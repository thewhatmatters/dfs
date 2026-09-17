"""Lineups.com RB/WR/TE snap-count refresh → nfl/data/snaps.csv.

Public pages only (no login). Identified User-Agent; cache under
nfl/data/lineups-snaps/. Grant: nfl/docs/data/lineups-authorization.md.

Some egress IPs get Cloudflare 403 on live urllib. Prefer cached `.json`
(SSR payload) when present; seed those from a network that can fetch.

Wednesday-style weekly refresh (after the prior week’s games land):

  python3 -m nfl.targets --refresh
  python3 -m nfl.snaps --refresh

Usage:
  python3 -m nfl.snaps --refresh
  python3 -m nfl.snaps --cache-only
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from nfl.lineups import LineupsError, extract_ssr_payload, fetch_metric_payload
from nfl.names import match_key
from nfl.players import Player
from nfl.projections import week1_score
from nfl.teams import UnmappedTeam, lookup_odds

SKILL_POS = frozenset({"RB", "WR", "TE"})

RB_URL = "https://www.lineups.com/nfl/snap-counts/running-back-rb-snap-counts/"
WR_URL = "https://www.lineups.com/nfl/snap-counts/wide-receiver-wr-snap-counts/"
TE_URL = "https://www.lineups.com/nfl/snap-counts/tight-end-te-snap-counts/"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "lineups-snaps"
DEFAULT_OUT = Path(__file__).resolve().parent / "data" / "snaps.csv"
SOURCE = "lineups"
METRIC = "snaps"
CHOKE = "SNAPS_LINEUPS"
CSV_FIELDS = (
    "player",
    "team",
    "position",
    "week",
    "snaps",
    "snap_share",
    "snaps_avg",
    "snaps_total",
    "team_snap_pct",
    "source",
    "asof",
)


class SnapsError(LineupsError):
    """Fatal Lineups snaps ingest."""


@dataclass(frozen=True)
class SnapWeekRow:
    player: str
    team: str
    position: str
    week: int
    snaps: int
    snap_share: float
    snaps_avg: float
    snaps_total: int
    team_snap_pct: float | None
    source: str
    asof: str


def extract_snaps_ssr(html: str) -> dict[str, Any]:
    """Parse embedded Lineups SSR JSON for metric=snaps."""
    try:
        return extract_ssr_payload(html, metric=METRIC, choke=CHOKE)
    except LineupsError as e:
        raise SnapsError(e.choke, str(e)) from e


def fetch_position_payload(position: str, url: str, *, refresh: bool) -> dict[str, Any]:
    try:
        return fetch_metric_payload(
            position,
            url,
            cache_dir=CACHE_DIR,
            metric=METRIC,
            choke=CHOKE,
            refresh=refresh,
        )
    except LineupsError as e:
        raise SnapsError(e.choke, str(e)) from e


def _pct(raw: Any) -> float:
    try:
        return float(raw) / 100.0
    except (TypeError, ValueError):
        return 0.0


def rows_from_payload(
    payload: dict[str, Any],
    *,
    asof: str,
    expect_pos: str | None = None,
) -> list[SnapWeekRow]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SnapsError(CHOKE, "SSR data missing")
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise SnapsError(CHOKE, "SSR rows empty")

    out: list[SnapWeekRow] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        team_name = str(item.get("team") or "").strip()
        pos = str(item.get("position") or expect_pos or "").strip().upper()
        if not name or not team_name or not pos:
            continue
        if expect_pos and pos != expect_pos:
            continue
        ref = lookup_odds(team_name)
        if ref is None:
            raise SnapsError(
                "SNAPS_JOIN",
                f"unmapped Lineups team {team_name!r} for {name!r}; "
                "add Odds full name to nfl/teams.py",
            )
        weeks = item.get("weeks") or []
        weeks_pct = item.get("weeksPct") or []
        if not isinstance(weeks, list):
            raise SnapsError(CHOKE, f"bad weeks for {name!r}")
        if not isinstance(weeks_pct, list):
            weeks_pct = []
        try:
            total = int(item.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        try:
            average = float(item.get("average") or 0)
        except (TypeError, ValueError):
            average = 0.0
        team_pct_raw = item.get("teamSnapPct")
        team_snap_pct: float | None
        if team_pct_raw is None or team_pct_raw == "":
            team_snap_pct = None
        else:
            team_snap_pct = _pct(team_pct_raw)

        for i, val in enumerate(weeks):
            if val is None:
                continue
            try:
                snaps = int(val)
            except (TypeError, ValueError):
                continue
            share_raw = weeks_pct[i] if i < len(weeks_pct) else None
            share = 0.0 if share_raw is None else _pct(share_raw)
            out.append(
                SnapWeekRow(
                    player=name,
                    team=ref.fd,
                    position=pos,
                    week=i + 1,
                    snaps=snaps,
                    snap_share=share,
                    snaps_avg=average,
                    snaps_total=total,
                    team_snap_pct=team_snap_pct,
                    source=SOURCE,
                    asof=asof,
                )
            )
    return out


def load_snaps_csv(path: Path) -> list[SnapWeekRow]:
    """Parse `snaps.csv` written by `write_snaps_csv` / `--refresh`."""
    p = Path(path)
    if not p.is_file():
        raise SnapsError("SNAPS_CSV", f"missing snaps CSV {p}")
    with p.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SnapsError("SNAPS_CSV", f"empty snaps CSV {p}")
        required = {"player", "team", "position", "week", "snaps", "snap_share"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise SnapsError("SNAPS_CSV", f"{p} missing columns {sorted(missing)}")
        out: list[SnapWeekRow] = []
        for row in reader:
            name = (row.get("player") or "").strip()
            team = (row.get("team") or "").strip().upper()
            pos = (row.get("position") or "").strip().upper()
            if not name or not team or not pos:
                continue
            try:
                week = int(row.get("week") or 0)
            except (TypeError, ValueError):
                continue
            if week < 1:
                continue
            try:
                snaps = int(float(row.get("snaps") or 0))
            except (TypeError, ValueError):
                snaps = 0
            try:
                share = float(row.get("snap_share") or 0)
            except (TypeError, ValueError):
                share = 0.0
            try:
                avg = float(row.get("snaps_avg") or 0)
            except (TypeError, ValueError):
                avg = 0.0
            try:
                total = int(float(row.get("snaps_total") or 0))
            except (TypeError, ValueError):
                total = 0
            raw_tm = (row.get("team_snap_pct") or "").strip()
            team_snap_pct: float | None
            if not raw_tm:
                team_snap_pct = None
            else:
                try:
                    team_snap_pct = float(raw_tm)
                except (TypeError, ValueError):
                    team_snap_pct = None
            out.append(
                SnapWeekRow(
                    player=name,
                    team=team,
                    position=pos,
                    week=week,
                    snaps=snaps,
                    snap_share=share,
                    snaps_avg=avg,
                    snaps_total=total,
                    team_snap_pct=team_snap_pct,
                    source=(row.get("source") or SOURCE).strip() or SOURCE,
                    asof=(row.get("asof") or "").strip(),
                )
            )
    if not out:
        raise SnapsError("SNAPS_CSV", f"no player-week rows in {p}")
    return out


def latest_week(rows: list[SnapWeekRow]) -> int | None:
    weeks = [r.week for r in rows if r.week >= 1]
    return max(weeks) if weeks else None


def rows_for_week(rows: list[SnapWeekRow], week: int) -> list[SnapWeekRow]:
    return [r for r in rows if r.week == week]


def _row_key(row: SnapWeekRow) -> tuple[str, str]:
    return (row.team.upper(), match_key(row.player))


def _player_key(pl: Player) -> tuple[str, str]:
    return ((pl.team or "").upper(), match_key(pl.name))


def snaps_index(rows: list[SnapWeekRow]) -> dict[tuple[str, str], SnapWeekRow]:
    """(team, match_key) → row. First row wins."""
    out: dict[tuple[str, str], SnapWeekRow] = {}
    for r in rows:
        key = _row_key(r)
        if key not in out:
            out[key] = r
    return out


def attach_snaps(
    players: list[Player],
    rows: list[SnapWeekRow],
    *,
    week: int | None = None,
) -> tuple[list[Player], dict[str, Any]]:
    """Join RB/WR/TE pool players to one Lineups snap week.

    Name join is `match_key` (Jr/Sr/II stripped). No invented aliases.
    Missing week / no hit → snap fields empty; week1_score uses targets
    only (or 1.0). Snap share tilts **RB** usage only — WR/TE snaps are
    stored for later and do not stack on target_share.
    """
    chosen = week if week is not None else latest_week(rows)
    week_rows = rows_for_week(rows, chosen) if chosen is not None else []
    idx = snaps_index(week_rows)
    used: set[tuple[str, str]] = set()
    out: list[Player] = []
    unmatched_slate: list[dict[str, str]] = []
    joined = 0
    for pl in players:
        pos = (pl.position or "").upper()
        if pos not in SKILL_POS:
            out.append(pl)
            continue
        key = _player_key(pl)
        hit = idx.get(key)
        if hit is None:
            unmatched_slate.append(
                {"player": pl.name, "team": pl.team, "position": pl.position}
            )
            obj = week1_score(
                pl.implied_total or 0.0,
                depth_rank=pl.depth_rank,
                position=pl.position,
                prop_fd=pl.prop_fd,
                implied_opp=pl.implied_opp,
                target_share=pl.target_share,
                snap_share=None,
            )
            out.append(
                replace(
                    pl,
                    snap_share=None,
                    snaps=None,
                    snaps_week=chosen,
                    snaps_status="unmatched",
                    objective=obj,
                )
            )
            continue
        used.add(key)
        joined += 1
        obj = week1_score(
            pl.implied_total or 0.0,
            depth_rank=pl.depth_rank,
            position=pl.position,
            prop_fd=pl.prop_fd,
            implied_opp=pl.implied_opp,
            target_share=pl.target_share,
            snap_share=hit.snap_share,
        )
        out.append(
            replace(
                pl,
                snap_share=hit.snap_share,
                snaps=hit.snaps,
                snaps_week=chosen,
                snaps_status="joined",
                objective=obj,
            )
        )
    slate_teams = {(p.team or "").upper() for p in players}
    unmatched_lineups: list[dict[str, Any]] = []
    seen_lu: set[tuple[str, str]] = set()
    for r in week_rows:
        key = _row_key(r)
        if key in used or key in seen_lu:
            continue
        if r.team.upper() not in slate_teams:
            continue
        seen_lu.add(key)
        unmatched_lineups.append(
            {
                "player": r.player,
                "team": r.team,
                "position": r.position,
                "week": r.week,
            }
        )
    slate_skill = sum(1 for p in players if (p.position or "").upper() in SKILL_POS)
    stats: dict[str, Any] = {
        "week": chosen,
        "joined": joined,
        "slate_rb_wr_te": slate_skill,
        "week_rows": len(week_rows),
        "unmatched_lineups": unmatched_lineups,
        "unmatched_slate_rb_wr_te": unmatched_slate,
        "skipped": False,
    }
    return out, stats


def print_snaps_gaps(stats: dict[str, Any]) -> None:
    """Stderr join summary + both unmatched lists."""
    if stats.get("skipped"):
        print("snaps skipped", file=sys.stderr)
        return
    week = stats.get("week")
    week_s = "—" if week is None else str(week)
    unmatched_lu = list(stats.get("unmatched_lineups") or [])
    unmatched_sl = list(stats.get("unmatched_slate_rb_wr_te") or [])
    print(
        f"snaps week {week_s}  joined {stats.get('joined', 0)} / "
        f"{stats.get('slate_rb_wr_te', 0)} slate RB/WR/TE  "
        f"unmatched_lineups {len(unmatched_lu)}  "
        f"unmatched_slate_rb_wr_te {len(unmatched_sl)}",
        file=sys.stderr,
    )
    if unmatched_lu:
        print(f"unmatched Lineups snaps ({len(unmatched_lu)}):", file=sys.stderr)
        for row in unmatched_lu:
            print(
                f"  {row.get('player')} ({row.get('team')} {row.get('position')})",
                file=sys.stderr,
            )
    if unmatched_sl:
        print(f"unmatched slate RB/WR/TE snaps ({len(unmatched_sl)}):", file=sys.stderr)
        for row in unmatched_sl:
            print(
                f"  {row.get('player')} ({row.get('team')} {row.get('position')})",
                file=sys.stderr,
            )


def _fmt_share(val: float | None) -> str:
    if not val:
        return "0"
    return f"{val:.4f}".rstrip("0").rstrip(".")


def write_snaps_csv(rows: list[SnapWeekRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda r: (r.team, r.position, r.player, r.week))
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in ordered:
            w.writerow(
                {
                    "player": r.player,
                    "team": r.team,
                    "position": r.position,
                    "week": r.week,
                    "snaps": r.snaps,
                    "snap_share": _fmt_share(r.snap_share),
                    "snaps_avg": r.snaps_avg,
                    "snaps_total": r.snaps_total,
                    "team_snap_pct": (
                        "" if r.team_snap_pct is None else _fmt_share(r.team_snap_pct)
                    ),
                    "source": r.source,
                    "asof": r.asof,
                }
            )


def refresh_snaps(
    *,
    refresh: bool = True,
    out: Path = DEFAULT_OUT,
) -> dict[str, Any]:
    asof = date.today().isoformat()
    rb_payload = fetch_position_payload("RB", RB_URL, refresh=refresh)
    wr_payload = fetch_position_payload("WR", WR_URL, refresh=refresh)
    te_payload = fetch_position_payload("TE", TE_URL, refresh=refresh)
    rb_rows = rows_from_payload(rb_payload, asof=asof, expect_pos="RB")
    wr_rows = rows_from_payload(wr_payload, asof=asof, expect_pos="WR")
    te_rows = rows_from_payload(te_payload, asof=asof, expect_pos="TE")
    rows = rb_rows + wr_rows + te_rows
    if not rows:
        raise SnapsError(CHOKE, "no player-week rows after parse")
    write_snaps_csv(rows, out)
    return {
        "out": str(out),
        "asof": asof,
        "rows": len(rows),
        "rb_players": len({r.player for r in rb_rows}),
        "wr_players": len({r.player for r in wr_rows}),
        "te_players": len({r.player for r in te_rows}),
        "rb_rows": len(rb_rows),
        "wr_rows": len(wr_rows),
        "te_rows": len(te_rows),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="re-fetch Lineups RB/WR/TE snap pages (writes cache + CSV)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"output CSV (default: {DEFAULT_OUT})",
    )
    ap.add_argument(
        "--cache-only",
        action="store_true",
        help="rebuild CSV from today's cache without HTTP",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.refresh and not args.cache_only:
        print(
            "choke SNAPS_LINEUPS: pass --refresh (or --cache-only)",
            file=sys.stderr,
        )
        return 2
    try:
        stats = refresh_snaps(
            refresh=bool(args.refresh) and not args.cache_only,
            out=args.out,
        )
    except SnapsError as e:
        print(f"choke {e.choke}: {e}", file=sys.stderr)
        return 1
    except UnmappedTeam as e:
        print(f"choke SNAPS_JOIN: {e}", file=sys.stderr)
        return 1
    print(
        f"snaps: {stats['rows']} rows "
        f"({stats['rb_players']} RB / {stats['wr_players']} WR / "
        f"{stats['te_players']} TE players) "
        f"→ {stats['out']} (asof {stats['asof']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
