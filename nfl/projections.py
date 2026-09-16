"""Week-1 player scores and slate projection board.

Default: implied team total × depth prior × position share.
A volume prop is a clamped ±20% tilt on that base — not a second currency.
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


def depth_prior(rank: int | None) -> float:
    if rank is None:
        return UNLISTED_PRIOR
    return DEPTH_PRIOR.get(int(rank), UNLISTED_PRIOR)


def prop_factor(base: float, prop_fd: float | None) -> float:
    """Books vote ±20% on the implied base. No line → 1.0."""
    if prop_fd is None or base <= 0:
        return 1.0
    return max(PROP_FACTOR_LO, min(PROP_FACTOR_HI, float(prop_fd) / base))


def implied_core(
    implied_total: float,
    depth_rank: int | None = None,
    position: str = "WR",
) -> float:
    """Implied × depth × share. No prop tilt. Not DST."""
    share = POS_FD_SHARE.get((position or "WR").upper(), 0.18)
    return float(implied_total) * depth_prior(depth_rank) * share


def week1_score(
    implied_total: float,
    depth_rank: int | None = None,
    position: str = "WR",
    prop_fd: float | None = None,
    implied_opp: float | None = None,
) -> float:
    """Implied core × optional prop tilt. One currency.

    DEF uses opponent implied total as expected points allowed.
    """
    pos = (position or "WR").upper()
    if pos in {"D", "DEF"}:
        return dst_projection(implied_opp if implied_opp is not None else 0.0)
    base = implied_core(implied_total, depth_rank=depth_rank, position=position)
    return base * prop_factor(base, prop_fd)


def score_player(pl: Player, depth_rank: int | None = None) -> float:
    rank = pl.depth_rank if depth_rank is None else depth_rank
    return week1_score(
        pl.implied_total or 0.0,
        depth_rank=rank,
        position=pl.position,
        prop_fd=pl.prop_fd,
        implied_opp=pl.implied_opp,
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
