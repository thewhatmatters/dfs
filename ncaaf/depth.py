"""Join OurLads depth rows to FanDuel Nickname+Team and attach depth_rank.

Rank 1 is a role prior (starter at that alignment), not 100% snaps.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from ncaaf.ourlads import (
    DEPTH_CSV,
    DepthRow,
    ingest_slate_depth,
    load_depth_csv,
    match_key,
)
from ncaaf.players import Player, load_fanduel_csv
from ncaaf.projections import score_player

# (fd_team, ourlads match_key) → FanDuel Nickname. Add when normalize misses.
NAME_OVERRIDES: dict[tuple[str, str], str] = {
    ("WMU", "ofa mataele"): "Lolo Mataele",
    ("ORST", "xayvion noland"): "Xavyion Noland",
}


def _override_lookup(team: str, ourlads_norm: str) -> str | None:
    nick = NAME_OVERRIDES.get((team, ourlads_norm))
    return nick.strip() if nick else None


def depth_index(
    rows: list[DepthRow],
) -> dict[tuple[str, str], tuple[int, DepthRow]]:
    """(team, match_key) → (best rank, row). Rank is min across WR alignments."""
    out: dict[tuple[str, str], tuple[int, DepthRow]] = {}
    for r in rows:
        key = (r.team, match_key(r.name))
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
) -> tuple[list[Player], dict]:
    """Set depth_rank; recompute week-1 objective if implied_total is present."""
    idx = depth_index(rows)
    out: list[Player] = []
    matched = 0
    for pl in players:
        key = (pl.team, match_key(pl.name))
        hit = idx.get(key)
        rank = hit[0] if hit else None
        if rank is not None:
            matched += 1
        nxt = replace(pl, depth_rank=rank)
        obj = nxt.objective
        if nxt.implied_total is not None:
            obj = score_player(nxt)
        out.append(replace(nxt, objective=obj))
    stats = {
        "depth_rows": len(rows),
        "players": len(players),
        "matched": matched,
        "unmatched_players": len(players) - matched,
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
        key = (r.team, match_key(r.name))
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
                key = (r.team, match_key(r.name))
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
            teams, refresh=args.refresh, out_csv=Path(args.out).expanduser()
        )
    except Exception as e:
        print(f"depth ingest failed: {e}", file=sys.stderr)
        return 1
    print(
        f"depth {len(rows)} rows  teams {len(teams)}  wrote {args.out}",
        file=sys.stderr,
    )
    print_depth_board(rows, players)
    leftover = unmatched_depth_names(players, rows)
    print(f"unmatched OurLads names: {len(leftover)}", file=sys.stderr)
    for r in leftover:
        print(f"  {r.team} {r.pos}{r.rank} {r.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
