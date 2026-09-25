"""RB/WR/TE target share for the usage tilt.

Default source is Lineups.com → nfl/data/targets.csv (`--targets-source=lineups`).
Public pages only (no login). Identified User-Agent; cache under
nfl/data/lineups-targets/. Grant: nfl/docs/data/lineups-authorization.md.

Some egress IPs get Cloudflare 403 on live urllib. Prefer cached `.json`
(SSR payload) when present; seed those from a network that can fetch.

Wednesday-style weekly refresh (after the prior week’s games land):

  python3 -m nfl.targets --refresh
  python3 -m nfl.snaps --refresh

`--targets-source=gangstash` reads `dataset=targets` and collapses a week
window with sum(targets)/sum(team_targets). The join still uses
`attach_targets` (unmatched target_share stays empty → usage 1.0).

Usage:
  python3 -m nfl.targets --refresh
  python3 -m nfl.targets --cache-only
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from nfl.gangstash_data import aggregate_target_window, fetch_targets
from nfl.lineups import (
    LineupsError,
    extract_ssr_payload as extract_lineups_ssr,
    fetch_metric_payload,
)
from nfl.names import match_key
from nfl.players import Player
from nfl.projections import week1_score
from nfl.teams import UnmappedTeam, lookup_odds

TARGET_POS = frozenset({"RB", "WR", "TE"})

RB_URL = "https://www.lineups.com/nfl/targets/running-back/"
WR_URL = "https://www.lineups.com/nfl/targets/wide-receiver/"
TE_URL = "https://www.lineups.com/nfl/targets/tight-end/"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "lineups-targets"
DEFAULT_OUT = Path(__file__).resolve().parent / "data" / "targets.csv"
SOURCE = "lineups"
METRIC = "targets"
CHOKE = "TARGETS_LINEUPS"
CSV_FIELDS = (
    "player",
    "team",
    "position",
    "week",
    "targets",
    "target_share",
    "targets_avg",
    "targets_total",
    "source",
    "asof",
)


class TargetsError(LineupsError):
    """Fatal Lineups targets ingest."""


@dataclass(frozen=True)
class TargetWeekRow:
    player: str
    team: str
    position: str
    week: int
    targets: int
    target_share: float
    targets_avg: float
    targets_total: int
    source: str
    asof: str


def extract_ssr_payload(html: str) -> dict[str, Any]:
    """Parse embedded Lineups SSR JSON for metric=targets."""
    try:
        return extract_lineups_ssr(html, metric=METRIC, choke=CHOKE)
    except LineupsError as e:
        raise TargetsError(e.choke, str(e)) from e


def fetch_position_payload(position: str, url: str, *, refresh: bool) -> dict[str, Any]:
    """Return SSR targets JSON for RB/WR/TE.

    Prefer cached `.json`. Else parse cached `.html`. Else live GET (writes
    both). Cloudflare 403 falls back to existing `.json` when present.
    """
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
        raise TargetsError(e.choke, str(e)) from e


def rows_from_payload(
    payload: dict[str, Any],
    *,
    asof: str,
    expect_pos: str | None = None,
) -> list[TargetWeekRow]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TargetsError("TARGETS_LINEUPS", "SSR data missing")
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise TargetsError("TARGETS_LINEUPS", "SSR rows empty")

    out: list[TargetWeekRow] = []
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
            raise TargetsError(
                "TARGETS_JOIN",
                f"unmapped Lineups team {team_name!r} for {name!r}; "
                "add Odds full name to nfl/teams.py",
            )
        weeks = item.get("weeks") or []
        weeks_pct = item.get("weeksPct") or []
        if not isinstance(weeks, list):
            raise TargetsError("TARGETS_LINEUPS", f"bad weeks for {name!r}")
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

        for i, val in enumerate(weeks):
            if val is None:
                continue
            try:
                targets = int(val)
            except (TypeError, ValueError):
                continue
            share_raw = weeks_pct[i] if i < len(weeks_pct) else None
            if share_raw is None:
                share = 0.0
            else:
                try:
                    share = float(share_raw) / 100.0
                except (TypeError, ValueError):
                    share = 0.0
            out.append(
                TargetWeekRow(
                    player=name,
                    team=ref.fd,
                    position=pos,
                    week=i + 1,
                    targets=targets,
                    target_share=share,
                    targets_avg=average,
                    targets_total=total,
                    source=SOURCE,
                    asof=asof,
                )
            )
    return out


def load_targets_csv(path: Path) -> list[TargetWeekRow]:
    """Parse `targets.csv` written by `write_targets_csv` / `--refresh`."""
    p = Path(path)
    if not p.is_file():
        raise TargetsError("TARGETS_CSV", f"missing targets CSV {p}")
    with p.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise TargetsError("TARGETS_CSV", f"empty targets CSV {p}")
        required = {"player", "team", "position", "week", "targets", "target_share"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise TargetsError(
                "TARGETS_CSV",
                f"{p} missing columns {sorted(missing)}",
            )
        out: list[TargetWeekRow] = []
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
                targets = int(float(row.get("targets") or 0))
            except (TypeError, ValueError):
                targets = 0
            try:
                share = float(row.get("target_share") or 0)
            except (TypeError, ValueError):
                share = 0.0
            try:
                avg = float(row.get("targets_avg") or 0)
            except (TypeError, ValueError):
                avg = 0.0
            try:
                total = int(float(row.get("targets_total") or 0))
            except (TypeError, ValueError):
                total = 0
            out.append(
                TargetWeekRow(
                    player=name,
                    team=team,
                    position=pos,
                    week=week,
                    targets=targets,
                    target_share=share,
                    targets_avg=avg,
                    targets_total=total,
                    source=(row.get("source") or SOURCE).strip() or SOURCE,
                    asof=(row.get("asof") or "").strip(),
                )
            )
    if not out:
        raise TargetsError("TARGETS_CSV", f"no player-week rows in {p}")
    return out


def latest_week(rows: list[TargetWeekRow]) -> int | None:
    weeks = [r.week for r in rows if r.week >= 1]
    return max(weeks) if weeks else None


def rows_for_week(rows: list[TargetWeekRow], week: int) -> list[TargetWeekRow]:
    return [r for r in rows if r.week == week]


def _row_key(row: TargetWeekRow) -> tuple[str, str]:
    return (row.team.upper(), match_key(row.player))


def _player_key(pl: Player) -> tuple[str, str]:
    return ((pl.team or "").upper(), match_key(pl.name))


def targets_index(
    rows: list[TargetWeekRow],
) -> dict[tuple[str, str], TargetWeekRow]:
    """(team, match_key) → row. First row wins."""
    out: dict[tuple[str, str], TargetWeekRow] = {}
    for r in rows:
        key = _row_key(r)
        prev = out.get(key)
        if prev is None:
            out[key] = r
    return out


def attach_targets(
    players: list[Player],
    rows: list[TargetWeekRow],
    *,
    week: int | None = None,
) -> tuple[list[Player], dict[str, Any]]:
    """Join RB/WR/TE pool players to one Lineups week.

    Name join is `match_key` (Jr/Sr/II stripped). No invented aliases.
    Missing week / no hit → target_share None (usage uses snaps or 1.0).
    """
    chosen = week if week is not None else latest_week(rows)
    week_rows = rows_for_week(rows, chosen) if chosen is not None else []
    idx = targets_index(week_rows)
    used: set[tuple[str, str]] = set()
    out: list[Player] = []
    unmatched_slate: list[dict[str, str]] = []
    joined = 0
    for pl in players:
        pos = (pl.position or "").upper()
        if pos not in TARGET_POS:
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
                target_share=None,
                snap_share=pl.snap_share,
            )
            out.append(
                replace(
                    pl,
                    target_share=None,
                    targets=None,
                    targets_week=chosen,
                    targets_status="unmatched",
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
            target_share=hit.target_share,
            snap_share=pl.snap_share,
        )
        out.append(
            replace(
                pl,
                target_share=hit.target_share,
                targets=hit.targets,
                targets_week=chosen,
                targets_status="joined",
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
    slate_skill = sum(1 for p in players if (p.position or "").upper() in TARGET_POS)
    stats: dict[str, Any] = {
        "week": chosen,
        "joined": joined,
        "slate_rb_wr_te": slate_skill,
        "slate_wr_te": slate_skill,
        "week_rows": len(week_rows),
        "unmatched_lineups": unmatched_lineups,
        "unmatched_slate_rb_wr_te": unmatched_slate,
        "unmatched_slate_wr_te": unmatched_slate,
        "skipped": False,
    }
    return out, stats


def print_targets_gaps(stats: dict[str, Any]) -> None:
    """Stderr join summary + both unmatched lists."""
    if stats.get("skipped"):
        print("targets skipped", file=sys.stderr)
        return
    week = stats.get("week")
    week_s = "—" if week is None else str(week)
    unmatched_lu = list(stats.get("unmatched_lineups") or [])
    unmatched_sl = list(
        stats.get("unmatched_slate_rb_wr_te") or stats.get("unmatched_slate_wr_te") or []
    )
    print(
        f"targets week {week_s}  joined {stats.get('joined', 0)} / "
        f"{stats.get('slate_rb_wr_te', stats.get('slate_wr_te', 0))} slate RB/WR/TE  "
        f"unmatched_lineups {len(unmatched_lu)}  "
        f"unmatched_slate_rb_wr_te {len(unmatched_sl)}",
        file=sys.stderr,
    )
    feed = "gangstash" if stats.get("source") == "gangstash" else "Lineups"
    if unmatched_lu:
        print(f"unmatched {feed} ({len(unmatched_lu)}):", file=sys.stderr)
        for row in unmatched_lu:
            print(
                f"  {row.get('player')} ({row.get('team')} {row.get('position')})",
                file=sys.stderr,
            )
    if unmatched_sl:
        print(f"unmatched slate RB/WR/TE ({len(unmatched_sl)}):", file=sys.stderr)
        for row in unmatched_sl:
            print(
                f"  {row.get('player')} ({row.get('team')} {row.get('position')})",
                file=sys.stderr,
            )


def write_targets_csv(rows: list[TargetWeekRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda r: (r.team, r.position, r.player, r.week))
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in ordered:
            share_s = (
                f"{r.target_share:.4f}".rstrip("0").rstrip(".")
                if r.target_share
                else "0"
            )
            w.writerow(
                {
                    "player": r.player,
                    "team": r.team,
                    "position": r.position,
                    "week": r.week,
                    "targets": r.targets,
                    "target_share": share_s,
                    "targets_avg": r.targets_avg,
                    "targets_total": r.targets_total,
                    "source": r.source,
                    "asof": r.asof,
                }
            )


def load_optimizer_targets(
    *,
    source: str,
    csv_path: Path,
    week: int | None,
    weeks: list[int] | None,
    season: int,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[TargetWeekRow], dict[str, Any]]:
    """Lineups CSV, or a gangstash window collapsed to one target_share per player."""
    src = (source or "lineups").strip().lower()
    if src == "lineups":
        return load_targets_csv(csv_path), {"source": "lineups", "join_week": week}
    if src != "gangstash":
        raise TargetsError(
            "TARGETS_SOURCE",
            f"unknown --targets-source {source!r} (lineups|gangstash)",
        )
    window = list(weeks) if weeks else ([week] if week else None)
    raw, meta = fetch_targets(
        season=season,
        weeks=window,
        refresh=refresh,
        cache_day=cache_day,
    )
    normalized = aggregate_target_window(raw, weeks=window)
    asof = (cache_day or date.today()).isoformat()
    rows: list[TargetWeekRow] = []
    for item in normalized:
        rows.append(
            TargetWeekRow(
                player=item["player_name"],
                team=item["team_fd"],
                position=item["position"],
                week=int(item["week"]),
                targets=int(item["targets"]),
                target_share=float(item["target_share"]),
                targets_avg=float(item["targets_avg"]),
                targets_total=int(item["targets_total"]),
                source="gangstash",
                asof=asof,
            )
        )
    join_week = rows[0].week if rows else None
    return rows, {
        **meta,
        "source": "gangstash",
        "join_week": join_week,
        "weeks": window,
        "season": season,
    }


def refresh_targets(
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
        raise TargetsError("TARGETS_LINEUPS", "no player-week rows after parse")
    write_targets_csv(rows, out)
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
        help="re-fetch Lineups RB/WR/TE target pages (writes cache + CSV)",
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
            "choke TARGETS_LINEUPS: pass --refresh (or --cache-only)",
            file=sys.stderr,
        )
        return 2
    try:
        stats = refresh_targets(
            refresh=bool(args.refresh) and not args.cache_only,
            out=args.out,
        )
    except TargetsError as e:
        print(f"choke {e.choke}: {e}", file=sys.stderr)
        return 1
    except UnmappedTeam as e:
        print(f"choke TARGETS_JOIN: {e}", file=sys.stderr)
        return 1
    print(
        f"targets: {stats['rows']} rows "
        f"({stats['rb_players']} RB / {stats['wr_players']} WR / "
        f"{stats['te_players']} TE players) "
        f"→ {stats['out']} (asof {stats['asof']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
