"""Week-1 player scores and later-model hooks.

Default: implied team total × role prior × position share × script.
A volume prop is a clamped ±20% tilt on that base — not a second currency.
Role prior is CFBD usage (WR/TE pass share, RB rush share) when present,
else OurLads depth. Ownership and weather stay None.
"""

from __future__ import annotations

import sys
from dataclasses import replace

from ncaaf.lines import TeamLine
from ncaaf.players import Player
from ncaaf.script import script_mult

# Bounded so a 0.5-point implied-total gap always outranks salary.
VALUE_EPS = 0.04
FLOOR_SALARY = 4_000
# Role prior, not snaps. Rank 1 starter at that alignment; unlisted tiny.
DEPTH_PRIOR = {1: 1.00, 2: 0.40, 3: 0.15}
UNLISTED_PRIOR = 0.05
# Scoring-identity share of implied team points → FD-point scale (not snap %).
POS_FD_SHARE = {"QB": 0.50, "RB": 0.30, "WR": 0.22, "TE": 0.12}
POS_ORDER = ("QB", "RB", "WR", "TE")
# Typical starter share of team pass (WR/TE) or rush (RB) plays. QB stays depth.
USAGE_REF = {"RB": 0.40, "WR": 0.22, "TE": 0.12}
USAGE_LO = 0.05
USAGE_HI = 1.65
PROP_FACTOR_LO = 0.80
PROP_FACTOR_HI = 1.20


def depth_prior(rank: int | None) -> float:
    if rank is None:
        return UNLISTED_PRIOR
    return DEPTH_PRIOR.get(int(rank), UNLISTED_PRIOR)


def usage_raw(
    position: str,
    rush_share: float | None,
    target_share: float | None,
) -> float | None:
    pos = (position or "WR").upper()
    if pos == "RB":
        return rush_share
    if pos in {"WR", "TE"}:
        return target_share
    return None


def role_prior(
    depth_rank: int | None,
    position: str = "WR",
    rush_share: float | None = None,
    target_share: float | None = None,
) -> float:
    """Usage / typical starter, else OurLads depth prior."""
    raw = usage_raw(position, rush_share, target_share)
    ref = USAGE_REF.get((position or "WR").upper())
    if raw is None or ref is None or ref <= 0:
        return depth_prior(depth_rank)
    return max(USAGE_LO, min(USAGE_HI, float(raw) / ref))


def prop_factor(base: float, prop_fd: float | None) -> float:
    """Books vote ±20% on the implied base. No line → 1.0."""
    if prop_fd is None or base <= 0:
        return 1.0
    return max(PROP_FACTOR_LO, min(PROP_FACTOR_HI, float(prop_fd) / base))


def implied_core(
    implied_total: float,
    depth_rank: int | None = None,
    position: str = "WR",
    pass_rate: float | None = None,
    opp_pass_rate: float | None = None,
    team_spread: float | None = None,
    apply_script: bool = True,
    rush_share: float | None = None,
    target_share: float | None = None,
    opp_pass_ppa: float | None = None,
    opp_rush_ppa: float | None = None,
) -> float:
    """Implied × role × share × script. No leftover, no prop tilt."""
    share = POS_FD_SHARE.get((position or "WR").upper(), 0.22)
    mix = 1.0
    if apply_script:
        mix = script_mult(
            position,
            pass_rate=pass_rate,
            opp_pass_rate=opp_pass_rate,
            team_spread=team_spread,
            depth_rank=depth_rank,
            opp_pass_ppa=opp_pass_ppa,
            opp_rush_ppa=opp_rush_ppa,
        )
    prior = role_prior(depth_rank, position, rush_share, target_share)
    return float(implied_total) * prior * share * mix


def week1_score(
    implied_total: float,
    salary: int,
    depth_rank: int | None = None,
    position: str = "WR",
    prop_fd: float | None = None,
    pass_rate: float | None = None,
    opp_pass_rate: float | None = None,
    team_spread: float | None = None,
    apply_script: bool = True,
    rush_share: float | None = None,
    target_share: float | None = None,
    opp_pass_ppa: float | None = None,
    opp_rush_ppa: float | None = None,
) -> float:
    """Implied core × optional prop tilt + leftover. One currency."""
    leftover = VALUE_EPS * (FLOOR_SALARY / max(int(salary), 1))
    base = implied_core(
        implied_total,
        depth_rank=depth_rank,
        position=position,
        pass_rate=pass_rate,
        opp_pass_rate=opp_pass_rate,
        team_spread=team_spread,
        apply_script=apply_script,
        rush_share=rush_share,
        target_share=target_share,
        opp_pass_ppa=opp_pass_ppa,
        opp_rush_ppa=opp_rush_ppa,
    )
    return base * prop_factor(base, prop_fd) + leftover


def score_player(player: Player, *, apply_script: bool | None = None) -> float:
    """week1_score from a Player's attached fields."""
    script = player.script_applied if apply_script is None else apply_script
    return week1_score(
        player.implied_total or 0.0,
        player.salary,
        depth_rank=player.depth_rank,
        position=player.position,
        prop_fd=player.prop_fd,
        pass_rate=player.pass_rate,
        opp_pass_rate=player.opp_pass_rate,
        team_spread=player.spread,
        apply_script=script,
        rush_share=player.rush_share,
        target_share=player.target_share,
        opp_pass_ppa=player.opp_pass_ppa,
        opp_rush_ppa=player.opp_rush_ppa,
    )


def attach_team_lines(
    players: list[Player],
    by_team: dict[str, TeamLine],
) -> list[Player]:
    """Copy spread/total/implied onto each player and set the ILP objective."""
    out: list[Player] = []
    for pl in players:
        line = by_team.get(pl.team)
        if line is None:
            continue
        implied = line.implied_for(pl.team)
        opp_implied = line.implied_for(pl.opponent)
        out.append(
            replace(
                pl,
                spread=line.spread_for(pl.team),
                total=line.total,
                implied_total=implied,
                implied_opp=opp_implied,
                moneyline=line.moneyline_for(pl.team),
                lines_provider=line.provider,
                lines_source=line.source,
                objective=week1_score(
                    implied,
                    pl.salary,
                    pl.depth_rank,
                    pl.position,
                    pass_rate=pl.pass_rate,
                    opp_pass_rate=pl.opp_pass_rate,
                    team_spread=line.spread_for(pl.team),
                    rush_share=pl.rush_share,
                    target_share=pl.target_share,
                    opp_pass_ppa=pl.opp_pass_ppa,
                    opp_rush_ppa=pl.opp_rush_ppa,
                ),
                ownership=None,
                weather=None,
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
    return "props" if player.prop_fd is not None else "model"


def on_default_board(player: Player) -> bool:
    """Starters + d2, plus anyone the books priced (`prop_fd`)."""
    return player.prop_fd is not None or player.depth_rank in (1, 2)


def board_row(player: Player, sim=None) -> dict:
    implied = player.implied_total
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

    `mode=default`: depth 1–2 plus anyone with `prop_fd`.
    `mode=all`: full pool. `proj` is `week1_score` (player.projection).
    Optional `sim_by_pid` adds game-sim mean/p10/p50/p90/p99 (not the ILP).
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

    With `--sim`, rows also print mean / p10 / p50 / p90 / p99. Game draws —
    teammates share the world.
    """
    label = "all" if (mode or "").lower() == "all" else "depth 1–2 + props"
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
    show_sim = any(r.get("mean") is not None for r in rows)
    if show_sim:
        print(
            f"    {'player':<{name_w}} pos team  d   implied  src    proj"
            f"    mean     p10    p50    p90    p99",
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
                f"{float(r['p50']):6.2f} {float(r['p90']):6.2f} "
                f"{float(r.get('p99') or 0):6.2f}"
            )
        print(
            f"    {str(r.get('player') or ''):<{name_w}} {pos:<3} "
            f"{str(r.get('team') or ''):<5} {str(r.get('depth') or '—'):<3} "
            f"{implied_s:>7}  {str(r.get('source') or ''):<5}  "
            f"{float(r.get('proj') or 0):.2f}{sim_s}",
            file=sys.stderr,
        )
