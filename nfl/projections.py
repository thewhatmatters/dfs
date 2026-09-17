"""Week-1 player scores and slate projection board.

Default: implied team total × depth prior × position share × usage factor.
A volume prop is a clamped ±20% tilt on that base — not a second currency.
WR/TE Lineups target_share is a ±20% usage tilt vs a depth-conditional
expected share. RB usage blends snap_share (rush role, 70%) with
target_share (receiving tilt, 30%), each clamped ±20% then blended —
not two stacked clamps, and not a replacement for Vegas implied totals.
WR/TE snaps are stored but not applied (would double-count targets).
DEF: implied_opp PA bucket + 3.0 sack/TO prior. No FPPG.
`--board` prints the point estimate; optional sim percentiles ride along.
"""

from __future__ import annotations

import sys
from dataclasses import replace

from nfl.lines import TeamLine
from nfl.players import Player
from nfl.rules import dst_projection

DEPTH_PRIOR = {1: 1.00, 2: 0.40, 3: 0.15}
UNLISTED_PRIOR = 0.05
POS_FD_SHARE = {"QB": 0.50, "RB": 0.28, "WR": 0.18, "TE": 0.12}
POS_ORDER = ("QB", "RB", "WR", "TE", "D")
PROP_FACTOR_LO = 0.80
PROP_FACTOR_HI = 1.20
USAGE_FACTOR_LO = 0.80
USAGE_FACTOR_HI = 1.20
# Depth-conditional expected target share. Unlisted uses the pos default.
EXPECTED_TARGET_SHARE = {
    ("WR", 1): 0.24,
    ("WR", 2): 0.16,
    ("WR", 3): 0.10,
    ("TE", 1): 0.18,
    ("TE", 2): 0.10,
    ("TE", 3): 0.06,
    ("RB", 1): 0.12,
    ("RB", 2): 0.07,
    ("RB", 3): 0.04,
}
UNLISTED_TARGET_SHARE = {"WR": 0.08, "TE": 0.06, "RB": 0.04}
# RB snap-share priors (rush role). WR/TE snaps are not scored.
EXPECTED_SNAP_SHARE = {
    ("RB", 1): 0.65,
    ("RB", 2): 0.30,
    ("RB", 3): 0.15,
}
UNLISTED_SNAP_SHARE = {"RB": 0.10}
RB_SNAP_WEIGHT = 0.70
RB_TARGET_WEIGHT = 0.30


def depth_prior(rank: int | None) -> float:
    if rank is None:
        return UNLISTED_PRIOR
    return DEPTH_PRIOR.get(int(rank), UNLISTED_PRIOR)


def prop_factor(base: float, prop_fd: float | None) -> float:
    """Books vote ±20% on the implied base. No line → 1.0."""
    if prop_fd is None or base <= 0:
        return 1.0
    return max(PROP_FACTOR_LO, min(PROP_FACTOR_HI, float(prop_fd) / base))


def expected_target_share(
    position: str = "WR",
    depth_rank: int | None = None,
) -> float:
    """Role-typical target share for WR/TE/RB. 0 for other positions."""
    pos = (position or "").upper()
    if pos not in UNLISTED_TARGET_SHARE:
        return 0.0
    if depth_rank is None:
        return UNLISTED_TARGET_SHARE[pos]
    return EXPECTED_TARGET_SHARE.get(
        (pos, int(depth_rank)), UNLISTED_TARGET_SHARE[pos]
    )


def expected_snap_share(
    position: str = "RB",
    depth_rank: int | None = None,
) -> float:
    """Role-typical snap share for an RB. 0 for other positions."""
    pos = (position or "").upper()
    if pos not in UNLISTED_SNAP_SHARE:
        return 0.0
    if depth_rank is None:
        return UNLISTED_SNAP_SHARE[pos]
    return EXPECTED_SNAP_SHARE.get(
        (pos, int(depth_rank)), UNLISTED_SNAP_SHARE[pos]
    )


def _clamp_usage(observed: float, expected: float) -> float:
    if expected <= 0:
        return 1.0
    return max(
        USAGE_FACTOR_LO,
        min(USAGE_FACTOR_HI, float(observed) / expected),
    )


def _share_factor(
    observed: float | None,
    expected: float,
) -> float | None:
    if observed is None:
        return None
    return _clamp_usage(observed, expected)


def usage_factor(
    target_share: float | None,
    position: str = "WR",
    depth_rank: int | None = None,
    snap_share: float | None = None,
) -> float:
    """Usage tilt vs depth-conditional expected share, clamped ±20%.

    WR/TE: target_share only (snap_share ignored — would double-count).
    RB: snaps primary (70%) + targets receiving tilt (30%). One missing
    signal uses the other at full weight. Neither → 1.0.
    """
    pos = (position or "").upper()
    tgt = _share_factor(target_share, expected_target_share(pos, depth_rank))
    snap = _share_factor(snap_share, expected_snap_share(pos, depth_rank))
    if pos in {"WR", "TE"}:
        return 1.0 if tgt is None else tgt
    if pos != "RB":
        return 1.0
    if snap is None and tgt is None:
        return 1.0
    if snap is None:
        return tgt if tgt is not None else 1.0
    if tgt is None:
        return snap
    blended = RB_SNAP_WEIGHT * snap + RB_TARGET_WEIGHT * tgt
    return max(USAGE_FACTOR_LO, min(USAGE_FACTOR_HI, blended))


def implied_core(
    implied_total: float,
    depth_rank: int | None = None,
    position: str = "WR",
    target_share: float | None = None,
    snap_share: float | None = None,
) -> float:
    """Implied × depth × share × usage. No prop tilt. Not DST."""
    share = POS_FD_SHARE.get((position or "WR").upper(), 0.18)
    return (
        float(implied_total)
        * depth_prior(depth_rank)
        * share
        * usage_factor(
            target_share, position, depth_rank, snap_share=snap_share
        )
    )


def week1_score(
    implied_total: float,
    depth_rank: int | None = None,
    position: str = "WR",
    prop_fd: float | None = None,
    implied_opp: float | None = None,
    target_share: float | None = None,
    snap_share: float | None = None,
) -> float:
    """Implied core × optional prop tilt. One currency.

    DEF uses opponent implied total as expected points allowed.
    WR/TE `target_share` and RB `snap_share`/`target_share` are usage
    tilts on the role prior, not a new objective.
    """
    pos = (position or "WR").upper()
    if pos in {"D", "DEF"}:
        return dst_projection(implied_opp if implied_opp is not None else 0.0)
    base = implied_core(
        implied_total,
        depth_rank=depth_rank,
        position=position,
        target_share=target_share,
        snap_share=snap_share,
    )
    return base * prop_factor(base, prop_fd)


def score_player(pl: Player, depth_rank: int | None = None) -> float:
    rank = pl.depth_rank if depth_rank is None else depth_rank
    return week1_score(
        pl.implied_total or 0.0,
        depth_rank=rank,
        position=pl.position,
        prop_fd=pl.prop_fd,
        implied_opp=pl.implied_opp,
        target_share=pl.target_share,
        snap_share=pl.snap_share,
    )


def attach_team_lines(
    players: list[Player],
    by_team: dict[str, TeamLine],
) -> list[Player]:
    out: list[Player] = []
    for pl in players:
        line = by_team.get(pl.team)
        if line is None:
            continue
        implied = line.implied_for(pl.team)
        opp_implied = line.implied_for(pl.opponent) if pl.opponent else None
        patched = replace(
            pl,
            spread=line.spread_for(pl.team),
            total=line.total,
            implied_total=implied,
            implied_opp=opp_implied,
            moneyline=line.moneyline_for(pl.team),
            lines_provider=line.provider,
            lines_source=line.source,
            ownership=None,
            weather=None,
        )
        out.append(
            replace(
                patched,
                objective=week1_score(
                    implied,
                    depth_rank=patched.depth_rank,
                    position=patched.position,
                    prop_fd=patched.prop_fd,
                    implied_opp=opp_implied,
                    target_share=patched.target_share,
                    snap_share=patched.snap_share,
                ),
            )
        )
    return out


def depth_mark(rank: int | None) -> str:
    """Board depth cell: starter `!`, d2/d3, em dash if unlisted."""
    if rank == 1:
        return "!"
    if rank is None:
        return "—"
    return f"d{int(rank)}"


def board_source(player: Player) -> str:
    if (player.position or "").upper() in {"D", "DEF"}:
        return "model"
    return "props" if player.prop_fd is not None else "model"


def on_default_board(player: Player) -> bool:
    """Starters + d2, priced props, and every DST."""
    if (player.position or "").upper() in {"D", "DEF"}:
        return True
    return player.prop_fd is not None or player.depth_rank in (1, 2)


def board_row(player: Player, sim=None) -> dict:
    implied = player.implied_total
    if (player.position or "").upper() in {"D", "DEF"}:
        implied = player.implied_opp
    row = {
        "player": player.name,
        "pos": player.position,
        "team": player.team,
        "depth": depth_mark(player.depth_rank),
        "implied": None if implied is None else round(float(implied), 4),
        "source": board_source(player),
        "proj": round(float(player.projection), 4),
        "game": player.game,
    }
    if sim is not None:
        row.update(sim.to_dict())
    return row


def projection_board(
    players: list[Player],
    *,
    mode: str = "default",
    sim_by_pid: dict | None = None,
) -> list[dict]:
    """ILP-pool projection rows grouped later by game, then pos.

    `mode=default`: depth 1–2 plus anyone with `prop_fd`, plus DST.
    `mode=all`: full pool. `proj` is `week1_score` (player.projection).
    Optional `sim_by_pid` adds game-draw mean/p10/p50/p90 (not the ILP).
    """
    src = list(players)
    if (mode or "default").lower() != "all":
        src = [p for p in src if on_default_board(p)]
    pos_rank = {p: i for i, p in enumerate(POS_ORDER)}

    def sort_key(p: Player) -> tuple:
        rank = p.depth_rank if p.depth_rank is not None else 99
        return (
            p.game or "",
            pos_rank.get((p.position or "").upper(), 99),
            rank,
            (p.name or "").lower(),
        )

    by_pid = sim_by_pid or {}
    return [
        board_row(p, by_pid.get(p.pid))
        for p in sorted(src, key=sort_key)
    ]


def print_projection_board(
    rows: list[dict],
    *,
    mode: str = "default",
    sim_n: int | None = None,
) -> None:
    """Stderr table: game → pos → player / depth / implied / source / proj.

    With `--sim`, rows also print mean / p10 / p50 / p90. Game draws —
    teammates share the world.
    """
    label = "all" if (mode or "").lower() == "all" else "depth 1–2 + props + DST"
    if sim_n:
        print(
            f"board ({label}; game Monte Carlo — teammates share the draw)",
            file=sys.stderr,
        )
    else:
        print(f"board ({label})", file=sys.stderr)
    if not rows:
        return
    name_w = min(28, max(16, max(len(str(r.get("player") or "")) for r in rows)))
    if any(r.get("mean") is not None for r in rows):
        print(
            f"    {'player':<{name_w}} pos team  d   implied  src    proj"
            f"    mean     p10    p50    p90",
            file=sys.stderr,
        )
    prev_game: str | None = None
    prev_pos: str | None = None
    for r in rows:
        game = str(r.get("game") or "")
        if game != prev_game:
            print(game, file=sys.stderr)
            prev_game = game
            prev_pos = None
        pos = str(r.get("pos") or "")
        if pos != prev_pos:
            print(f"  {pos}", file=sys.stderr)
            prev_pos = pos
        implied = r.get("implied")
        implied_s = "—" if implied is None else f"{implied:g}"
        sim_s = ""
        if r.get("mean") is not None:
            sim_s = (
                f"  {float(r['mean']):6.2f}  {float(r['p10']):6.2f} "
                f"{float(r['p50']):6.2f} {float(r['p90']):6.2f}"
            )
        print(
            f"    {str(r.get('player') or ''):<{name_w}} {pos:<3} "
            f"{str(r.get('team') or ''):<5} {str(r.get('depth') or '—'):<3} "
            f"{implied_s:>7}  {str(r.get('source') or ''):<5}  "
            f"{float(r.get('proj') or 0):.2f}{sim_s}",
            file=sys.stderr,
        )
