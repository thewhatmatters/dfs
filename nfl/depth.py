"""Join OurLads (default), ESPN, or gangstash depth to FanDuel Nickname+Team.

Default path is authorized OurLads HTML → nfl/data/depth.csv.
`--depth-source=espn` is an optional fallback (ESPN site.api often 403).
`--depth-source=gangstash` reads `dataset=depth_charts`, base offense
`pos_grp` `3WR 1TE`. `pos_rank` is the depth rank (WR2 = pos_abb WR,
pos_rank 2). Rank 1 is a role prior, not 100% snaps.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from nfl.gangstash import GangstashDataError, GangstashDataKeyMissing, GangstashTruncated
from nfl.gangstash_data import fetch_depth_charts, map_depth_slots
from nfl.http import HttpError, http_json
from nfl.names import match_key
from nfl.ourlads import (
    DEPTH_CSV,
    DepthError,
    DepthRow,
    ingest_slate_depth as ingest_ourlads_depth,
    match_key as ourlads_match_key,
    skill_pos,
)
from nfl.ourlads import _dedupe as dedupe_depth_rows
from nfl.players import Player, load_fanduel_csv
from nfl.teams import ALIASES, TeamRef, require_mapped

ESPN_DEPTH = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{id}/depthcharts"
)
ESPN_CACHE_DIR = Path(__file__).resolve().parent / "data" / "espn-depth"

# Offense alignment keys → FanDuel position. wr1/wr2/wr3 are X/Y/Z.
SKILL_POS = {
    "qb": "QB",
    "rb": "RB",
    "hb": "RB",
    "te": "TE",
    "wr": "WR",
    "wr1": "WR",
    "wr2": "WR",
    "wr3": "WR",
    "wrx": "WR",
    "wry": "WR",
    "wrz": "WR",
    "lwr": "WR",
    "rwr": "WR",
    "slwr": "WR",
    "slot": "WR",
}

# (fd_team, ourlads match_key) → FanDuel Nickname. Add when normalize misses.
NAME_OVERRIDES: dict[tuple[str, str], str] = {}


class EspnDepthError(DepthError):
    """Fatal ESPN depth ingest (optional --depth-source=espn)."""


class GangstashDepthError(DepthError):
    """Fatal gangstash depth ingest."""


class GangstashDepthKeyMissing(GangstashDepthError):
    """No GANGSTASH_API_KEY and no depth cache. Optimizer leaves depth unlisted."""


def _override_lookup(team: str, ourlads_norm: str) -> str | None:
    nick = NAME_OVERRIDES.get((team, ourlads_norm))
    return nick.strip() if nick else None


def _day_dir() -> Path:
    d = ESPN_CACHE_DIR / date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_team_depth(espn_id: str, *, refresh: bool = False) -> dict:
    path = _day_dir() / f"{espn_id}.json"
    if path.is_file() and path.stat().st_size > 2 and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    url = ESPN_DEPTH.format(id=espn_id)
    try:
        payload, _hdrs = http_json(url)
    except HttpError as e:
        raise EspnDepthError(f"ESPN depth team {espn_id}: {e}") from e
    if not isinstance(payload, dict):
        raise EspnDepthError(f"ESPN depth team {espn_id} did not return an object")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _offense_chart(charts: list[dict]) -> dict | None:
    best = None
    best_n = -1
    for ch in charts:
        if not isinstance(ch, dict):
            continue
        pos = ch.get("positions") or {}
        n = sum(1 for k in pos if str(k).lower() in SKILL_POS)
        if n > best_n:
            best, best_n = ch, n
    return best


def parse_team_depth(payload: dict, team: TeamRef) -> list[DepthRow]:
    charts = payload.get("depthchart") or payload.get("items") or []
    if not isinstance(charts, list):
        return []
    offense = _offense_chart(charts)
    if offense is None:
        return []
    rows: list[DepthRow] = []
    positions = offense.get("positions") or {}
    fetched_at = date.today().isoformat()
    src = ESPN_DEPTH.format(id=team.espn_id)
    for key, block in positions.items():
        pos = SKILL_POS.get(str(key).lower())
        if pos is None or not isinstance(block, dict):
            continue
        athletes = block.get("athletes") or []
        for i, ath in enumerate(athletes):
            if not isinstance(ath, dict):
                continue
            nested = ath.get("athlete") if isinstance(ath.get("athlete"), dict) else None
            src_ath = nested or ath
            name = str(src_ath.get("displayName") or src_ath.get("fullName") or "").strip()
            if not name:
                continue
            rows.append(
                DepthRow(
                    team=team.fd,
                    pos=pos,
                    rank=i + 1,
                    name=name,
                    source_url=src,
                    fetched_at=fetched_at,
                )
            )
    return rows


def ingest_espn_slate_depth(
    slate_teams: set[str],
    *,
    refresh: bool = False,
) -> list[DepthRow]:
    refs = require_mapped(slate_teams)
    rows: list[DepthRow] = []
    for fd in sorted(refs):
        ref = refs[fd]
        payload = fetch_team_depth(ref.espn_id, refresh=refresh)
        rows.extend(parse_team_depth(payload, ref))
    return rows


def _write_depth_csv(rows: list[DepthRow], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["team", "pos", "rank", "name", "source_url", "fetched_at"],
        )
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "team": r.team,
                    "pos": r.pos,
                    "rank": r.rank,
                    "name": r.name,
                    "source_url": r.source_url,
                    "fetched_at": r.fetched_at,
                }
            )


def ingest_gangstash_slate_depth(
    slate_teams: set[str],
    *,
    refresh: bool = False,
    out_csv: Path | None = None,
    cache_day: date | None = None,
) -> list[DepthRow]:
    """Map gangstash depth_charts onto slate FanDuel teams.

    Keeps `pos_grp` 3WR 1TE. Players who also appear in other groups are
    dropped with those rows, then the best `pos_rank` wins per player.
    Missing skill rows for a slate team is fatal. Non-skill `pos_abb` values
    are skipped. `out_csv` is written only when the caller passes a path so an
    optimize run does not replace the OurLads `depth.csv`.
    """
    canon = {
        ALIASES.get(t.strip().upper(), t.strip().upper())
        for t in slate_teams
        if t
    }
    try:
        raw, _meta = fetch_depth_charts(refresh=refresh, cache_day=cache_day)
    except GangstashDataKeyMissing as e:
        raise GangstashDepthKeyMissing(str(e)) from e
    except (GangstashTruncated, GangstashDataError) as e:
        raise GangstashDepthError(str(e)) from e

    fetched_at = datetime.now(timezone.utc).isoformat()
    src = "https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/data?dataset=depth_charts"
    try:
        slots = map_depth_slots(raw)
    except GangstashDataError as e:
        raise GangstashDepthError(str(e)) from e
    rows: list[DepthRow] = []
    seen: set[str] = set()
    for slot in slots:
        if slot.team_fd not in canon:
            continue
        pos = skill_pos(slot.position)
        if pos is None:
            continue
        seen.add(slot.team_fd)
        rows.append(
            DepthRow(
                team=slot.team_fd,
                pos=pos,
                rank=slot.rank,
                name=slot.player_name,
                source_url=src,
                fetched_at=fetched_at,
            )
        )
    missing = sorted(canon - seen)
    if missing:
        raise GangstashDepthError(
            "gangstash depth has no skill rows for " + ", ".join(missing)
        )
    rows = dedupe_depth_rows(rows)
    if out_csv is not None:
        _write_depth_csv(rows, out_csv)
    return rows


def ingest_slate_depth(
    slate_teams: set[str],
    *,
    refresh: bool = False,
    source: str = "ourlads",
    out_csv: Path | None = None,
) -> list[DepthRow]:
    """Default OurLads. `espn` and `gangstash` are optional sources."""
    src = (source or "ourlads").strip().lower()
    if src == "espn":
        return ingest_espn_slate_depth(slate_teams, refresh=refresh)
    if src == "gangstash":
        return ingest_gangstash_slate_depth(
            slate_teams, refresh=refresh, out_csv=out_csv
        )
    if src != "ourlads":
        raise DepthError(
            f"unknown --depth-source {source!r} (ourlads|espn|gangstash)"
        )
    return ingest_ourlads_depth(slate_teams, refresh=refresh, out_csv=out_csv)


def depth_index(
    rows: list[DepthRow],
) -> dict[tuple[str, str], tuple[int, DepthRow]]:
    """(team, match_key) → (best rank, row). Rank is min across WR alignments."""
    out: dict[tuple[str, str], tuple[int, DepthRow]] = {}
    for r in rows:
        key = (r.team, ourlads_match_key(r.name))
        prev = out.get(key)
        if prev is None or r.rank < prev[0]:
            out[key] = (r.rank, r)
        nick = _override_lookup(r.team, key[1])
        if nick:
            okey = (r.team, match_key(nick))
            prev = out.get(okey)
            if prev is None or r.rank < prev[0]:
                out[okey] = (r.rank, r)
    return out


def attach_depth_ranks(
    players: list[Player],
    rows: list[DepthRow],
    score_fn=None,
    *,
    source: str | None = None,
) -> tuple[list[Player], dict]:
    idx = depth_index(rows)
    out: list[Player] = []
    matched = 0
    for pl in players:
        if pl.position == "D":
            out.append(pl)
            continue
        hit = idx.get((pl.team, match_key(pl.name)))
        rank = hit[0] if hit else None
        if rank is not None:
            matched += 1
        obj = pl.objective
        if score_fn is not None:
            obj = score_fn(pl, rank)
        src = source if rank is not None else None
        out.append(replace(pl, depth_rank=rank, depth_source=src, objective=obj))
    stats = {
        "depth_rows": len(rows),
        "players": len(players),
        "matched": matched,
        "unmatched_players": sum(1 for p in players if p.position != "D") - matched,
    }
    return out, stats


def unmatched_depth_names(
    players: list[Player],
    rows: list[DepthRow],
) -> list[DepthRow]:
    fd_keys = {(p.team, match_key(p.name)) for p in players}
    leftover: list[DepthRow] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (r.team, ourlads_match_key(r.name))
        nick = _override_lookup(r.team, key[1])
        aliases = {key}
        if nick:
            aliases.add((r.team, match_key(nick)))
        if aliases & fd_keys:
            continue
        if key in seen:
            continue
        seen.add(key)
        leftover.append(r)
    return leftover


def print_depth_board(rows: list[DepthRow], players: list[Player] | None = None) -> None:
    fd_keys = {(p.team, match_key(p.name)) for p in players} if players else None
    by_team: dict[str, list[DepthRow]] = {}
    for r in rows:
        by_team.setdefault(r.team, []).append(r)
    for team in sorted(by_team):
        print(f"{team}", file=sys.stderr)
        for r in sorted(by_team[team], key=lambda x: (x.pos, x.rank, x.name)):
            mark = ""
            if fd_keys is not None:
                key = (r.team, ourlads_match_key(r.name))
                nick = _override_lookup(r.team, key[1])
                ok = key in fd_keys or (
                    nick is not None and (r.team, match_key(nick)) in fd_keys
                )
                mark = "  join" if ok else "  NOJOIN"
            print(
                f"  {r.pos:<3} {r.rank}  {r.name}{mark}",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="FanDuel players-list CSV")
    ap.add_argument("--out", default=str(DEPTH_CSV), help="depth.csv path")
    ap.add_argument("--refresh", action="store_true", help="bypass OurLads HTML cache")
    ap.add_argument(
        "--depth-source",
        choices=("ourlads", "espn", "gangstash"),
        default="ourlads",
        help="ourlads (default), espn fallback, or gangstash depth_charts",
    )
    ap.add_argument("--agent", action="store_true")
    args = ap.parse_args(argv)

    csv_path = Path(args.csv).expanduser()
    try:
        players = load_fanduel_csv(csv_path)
    except (OSError, ValueError) as e:
        print(f"load failed: {e}", file=sys.stderr)
        return 1
    teams = {p.team for p in players} | {p.opponent for p in players if p.opponent}
    try:
        rows = ingest_slate_depth(
            teams,
            refresh=args.refresh,
            source=args.depth_source,
            out_csv=Path(args.out).expanduser(),
        )
    except Exception as e:
        print(f"depth ingest failed: {e}", file=sys.stderr)
        return 1
    print(
        f"depth {len(rows)} rows  teams {len(teams)}  wrote {args.out}  "
        f"source {args.depth_source}",
        file=sys.stderr,
    )
    print_depth_board(rows, players)
    leftover = unmatched_depth_names(players, rows)
    print(f"unmatched depth names: {len(leftover)}", file=sys.stderr)
    for r in leftover:
        print(f"  {r.team} {r.pos}{r.rank} {r.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
