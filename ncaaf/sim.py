"""Structural game Monte Carlo for FanDuel-point draws.

One world per slate draw: each game gets a Vegas total + home-spread
realization; both teams' points come from that pair. Every player in that
game is scored from the same team points and realized margin (script sit
uses the realized spread, not the close). Teammates and bring-backs share
the world. Different games are independent.

Not a play-by-play copula and not SaberSim. No 2025 PBP.

`--board` stays the point estimate (implied×depth×share×script, ±20% prop
tilt). Default ILP (`mean`) stays `week1_score` — not sim p50. `floor` /
`ceiling` replace `Player.objective` with that player's sim p10 / p99
(then optional interview lean). p90 cannot see ~8% dog-upset worlds;
ceiling is the GPP tail. Lineup Fl/Cl are the joint 7 (sum in the
same world), not the sum of player p10s.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, replace

from ncaaf.players import Player
from ncaaf.projections import POS_FD_SHARE, prop_factor, role_prior
from ncaaf.rules import FANDUEL_NCAAF
from ncaaf.script import script_mult

DEFAULT_DRAWS = 10_000
DEFAULT_SEED = 1
ILP_OBJECTIVES = ("mean", "floor", "ceiling")
SIM_OBJECTIVES = frozenset({"floor", "ceiling"})

# Model: team points ~ Normal(implied, 0.20 × implied), floor 3.
# Used for solo / unparsed games and simulate_player (single-player tests).
TEAM_SIGMA_FRAC = 0.20
TEAM_POINTS_FLOOR = 3.0
# Game draw: total and home spread. CFB close-error is wider than NFL ~13.
TOTAL_SIGMA_FRAC = 0.15
SPREAD_SIGMA = 16.0

YARD_FIELDS: tuple[tuple[str, str], ...] = (
    ("prop_pass_yds", "pass_yd"),
    ("prop_rush_yds", "rush_yd"),
    ("prop_rec_yds", "rec_yd"),
)
COUNT_FIELDS: tuple[tuple[str, str], ...] = (
    ("prop_pass_tds", "pass_td"),
    ("prop_receptions", "rec"),
)
VOLUME_ATTRS: tuple[str, ...] = tuple(a for a, _k in YARD_FIELDS + COUNT_FIELDS)


@dataclass(frozen=True)
class SimStats:
    mean: float
    p10: float
    p50: float
    p90: float
    n: int
    source: str  # "props" | "model" | "game"
    p95: float = 0.0
    p99: float = 0.0

    def to_dict(self) -> dict[str, float]:
        p10 = round(self.p10, 4)
        p90 = round(self.p90, 4)
        p99 = round(self.p99, 4)
        return {
            "mean": round(self.mean, 4),
            "p10": p10,
            "p50": round(self.p50, 4),
            "p90": p90,
            "p95": round(self.p95, 4),
            "p99": p99,
            "floor": p10,
            "ceiling": p99,
        }


@dataclass(frozen=True)
class GameSim:
    """Aligned draws: index t is one slate world."""

    by_pid: dict[str, SimStats]
    draws: dict[str, tuple[float, ...]]

    def lineup_stats(self, pids: list[str]) -> SimStats | None:
        cols = [self.draws[p] for p in pids if p in self.draws]
        if not cols:
            return None
        n = min(len(c) for c in cols)
        if n <= 0:
            return None
        totals = [sum(c[t] for c in cols) for t in range(n)]
        return _stats(totals, n=n, source="game")


def sim_header(n: int) -> str:
    return (
        f"sim {int(n)} game draws — one world per game (Vegas total+spread); "
        "teammates share it. Not a PBP copula."
    )


def has_volume_props(player: Player) -> bool:
    """True when any pass/rush/rec yard, reception, or pass-TD line is set."""
    return any(getattr(player, attr) is not None for attr in VOLUME_ATTRS)


def volume_point(player: Player) -> float | None:
    """Point-estimate FD from attached volume lines (no leftover, no bonuses)."""
    if not has_volume_props(player):
        return None
    sc = FANDUEL_NCAAF.scoring
    pts = 0.0
    for attr, key in YARD_FIELDS + COUNT_FIELDS:
        val = getattr(player, attr)
        if val is not None:
            pts += float(val) * sc[key]
    return pts


def model_point(player: Player) -> float:
    """Implied × depth × share × script. No leftover — sim mean target."""
    implied = float(player.implied_total or 0.0)
    share = POS_FD_SHARE.get((player.position or "WR").upper(), 0.22)
    mix = 1.0
    if player.script_applied:
        mix = script_mult(
            player.position,
            pass_rate=player.pass_rate,
            opp_pass_rate=player.opp_pass_rate,
            team_spread=player.spread,
            depth_rank=player.depth_rank,
            opp_pass_ppa=player.opp_pass_ppa,
            opp_rush_ppa=player.opp_rush_ppa,
        )
    prior = role_prior(
        player.depth_rank,
        player.position,
        player.rush_share,
        player.target_share,
    )
    return implied * prior * share * mix


def simulate_player(
    player: Player,
    *,
    n: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
) -> SimStats:
    """n independent FD-point draws for one player (closing script).

    Unit-test / single-player path. Pool `--sim` is `simulate_games`.
    """
    if n <= 0:
        raise ValueError("n must be > 0")
    rng = random.Random(_player_seed(seed, player.pid))
    props = has_volume_props(player)
    draws = [_one_draw(rng, player, props) for _ in range(n)]
    return _stats(draws, n=n, source="props" if props else "model")


def simulate_pool(
    players: list[Player],
    *,
    n: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
) -> dict[str, SimStats]:
    """pid → SimStats from the structural game draw. Pool order does not matter."""
    return simulate_games(players, n=n, seed=seed).by_pid


def simulate_games(
    players: list[Player],
    *,
    n: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
) -> GameSim:
    """n slate worlds. Players in a game share total+margin; games do not."""
    if n <= 0:
        return GameSim(by_pid={}, draws={})
    groups: dict[str, list[Player]] = {}
    for pl in players:
        groups.setdefault(_game_key(pl), []).append(pl)
    for key in groups:
        groups[key].sort(key=lambda p: p.pid)
    rng = random.Random(int(seed))
    raw: dict[str, list[float]] = {p.pid: [] for p in players}
    for _ in range(n):
        for key in sorted(groups):
            group = groups[key]
            home_pts, away_pts, away, home = _draw_game(rng, group)
            for pl in group:
                if away is None or home is None:
                    implied = float(pl.implied_total or 0.0)
                    team_pts = _gauss_floor(
                        rng,
                        implied,
                        TEAM_SIGMA_FRAC * abs(implied),
                        TEAM_POINTS_FLOOR,
                    )
                    realized = pl.spread
                else:
                    team_pts, realized = _player_world(
                        pl, home_pts, away_pts, away, home
                    )
                raw[pl.pid].append(_score_world(pl, team_pts, realized))
    draws = {pid: tuple(xs) for pid, xs in raw.items()}
    by_pid: dict[str, SimStats] = {}
    for pl in players:
        xs = list(draws[pl.pid])
        src = "props" if has_volume_props(pl) else "model"
        by_pid[pl.pid] = _stats(xs, n=n, source=src)
    return GameSim(by_pid=by_pid, draws=draws)


def apply_ilp_objective(
    players: list[Player],
    kind: str,
    *,
    sim_by_pid: dict[str, SimStats] | None = None,
    lean_pids: frozenset[str] | None = None,
    lean_mult: float = 1.0,
) -> list[Player]:
    """Set each player's ILP `objective`.

    `mean`: leave `week1_score` (and any interview lean already on it).
    `floor` / `ceiling`: sim p10 / p99. Re-apply `lean_mult` for `lean_pids`
    (interview already scaled `week1_score`; do not double-apply on mean).
    Ceiling is p99 because p90 never sees ~8% dog-upset worlds.
    """
    kind = (kind or "mean").lower()
    if kind == "mean":
        return list(players)
    if kind not in SIM_OBJECTIVES:
        raise ValueError(f"unknown objective {kind!r}")
    by_pid = sim_by_pid or {}
    leaned = lean_pids or frozenset()
    attr = "p10" if kind == "floor" else "p99"
    out: list[Player] = []
    for pl in players:
        st = by_pid.get(pl.pid)
        if st is None:
            out.append(pl)
            continue
        val = float(getattr(st, attr))
        if pl.pid in leaned:
            val *= lean_mult
        if val != pl.objective:
            out.append(replace(pl, objective=val))
        else:
            out.append(pl)
    return out


def _player_seed(seed: int, pid: str) -> int:
    digest = hashlib.blake2b(
        f"{int(seed)}\0{pid}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "little")


def _gauss_floor(rng: random.Random, mu: float, sigma: float, floor: float) -> float:
    if sigma <= 0:
        return max(floor, mu)
    return max(floor, rng.gauss(mu, sigma))


def _game_key(player: Player) -> str:
    game = (player.game or "").strip()
    return game if game else f"solo:{player.pid}"


def _split_game(game: str) -> tuple[str | None, str | None]:
    if "@" not in (game or ""):
        return None, None
    away, home = game.split("@", 1)
    a = away.strip().upper()
    h = home.strip().upper()
    return (a or None, h or None)


def _vegas(group: list[Player]) -> tuple[float, float, str | None, str | None]:
    """total_mu, spread_home_mu, away abbrev, home abbrev."""
    away, home = _split_game(group[0].game or "")
    total = next((float(p.total) for p in group if p.total is not None), None)
    home_spread = None
    away_spread = None
    home_impl = None
    away_impl = None
    for p in group:
        team = (p.team or "").upper()
        if home and team == home:
            if p.spread is not None:
                home_spread = float(p.spread)
            if p.implied_total is not None:
                home_impl = float(p.implied_total)
        elif away and team == away:
            if p.spread is not None:
                away_spread = float(p.spread)
            if p.implied_total is not None:
                away_impl = float(p.implied_total)
        if p.implied_opp is not None:
            opp = (p.opponent or "").upper()
            if home and opp == home and home_impl is None:
                home_impl = float(p.implied_opp)
            if away and opp == away and away_impl is None:
                away_impl = float(p.implied_opp)
    if home_spread is None and away_spread is not None:
        home_spread = -float(away_spread)
    if home_spread is None:
        home_spread = 0.0
    if total is None:
        if home_impl is not None and away_impl is not None:
            total = home_impl + away_impl
        elif home_impl is not None:
            total = 2.0 * home_impl + float(home_spread)
        elif away_impl is not None:
            total = 2.0 * away_impl - float(home_spread)
        else:
            p0 = group[0]
            total = float(p0.implied_total or 0.0) + float(p0.implied_opp or 0.0)
            if total <= 0:
                total = float(p0.implied_total or 0.0)
    return float(total), float(home_spread), away, home


def _draw_game(
    rng: random.Random, group: list[Player]
) -> tuple[float, float, str | None, str | None]:
    total_mu, spread_home_mu, away, home = _vegas(group)
    if away is None or home is None:
        # Unparsed tag: independent team totals, closing spread on each player.
        return (0.0, 0.0, None, None)
    total_d = _gauss_floor(
        rng,
        total_mu,
        TOTAL_SIGMA_FRAC * abs(total_mu),
        2.0 * TEAM_POINTS_FLOOR,
    )
    spread_d = rng.gauss(spread_home_mu, SPREAD_SIGMA)
    home_pts = max(TEAM_POINTS_FLOOR, (total_d - spread_d) / 2.0)
    away_pts = max(TEAM_POINTS_FLOOR, (total_d + spread_d) / 2.0)
    return home_pts, away_pts, away, home


def _player_world(
    player: Player,
    home_pts: float,
    away_pts: float,
    away: str | None,
    home: str | None,
) -> tuple[float, float | None]:
    team = (player.team or "").upper()
    if home and team == home:
        return home_pts, away_pts - home_pts
    if away and team == away:
        return away_pts, home_pts - away_pts
    implied = float(player.implied_total or 0.0)
    return implied, player.spread


def _score_world(
    player: Player, team_pts: float, realized_spread: float | None
) -> float:
    share = POS_FD_SHARE.get((player.position or "WR").upper(), 0.22)
    mix = 1.0
    if player.script_applied:
        mix = script_mult(
            player.position,
            pass_rate=player.pass_rate,
            opp_pass_rate=player.opp_pass_rate,
            team_spread=realized_spread,
            depth_rank=player.depth_rank,
            opp_pass_ppa=player.opp_pass_ppa,
            opp_rush_ppa=player.opp_rush_ppa,
        )
    prior = role_prior(
        player.depth_rank,
        player.position,
        player.rush_share,
        player.target_share,
    )
    pts = float(team_pts) * prior * share * mix
    if has_volume_props(player):
        pts *= prop_factor(model_point(player), player.prop_fd)
    return max(0.0, pts)


def _one_draw(rng: random.Random, player: Player, props: bool) -> float:
    implied = float(player.implied_total or 0.0)
    sigma = TEAM_SIGMA_FRAC * abs(implied)
    team_pts = _gauss_floor(rng, implied, sigma, TEAM_POINTS_FLOOR)
    share = POS_FD_SHARE.get((player.position or "WR").upper(), 0.22)
    mix = 1.0
    if player.script_applied:
        mix = script_mult(
            player.position,
            pass_rate=player.pass_rate,
            opp_pass_rate=player.opp_pass_rate,
            team_spread=player.spread,
            depth_rank=player.depth_rank,
            opp_pass_ppa=player.opp_pass_ppa,
            opp_rush_ppa=player.opp_rush_ppa,
        )
    prior = role_prior(
        player.depth_rank,
        player.position,
        player.rush_share,
        player.target_share,
    )
    pts = team_pts * prior * share * mix
    if props:
        pts *= prop_factor(model_point(player), player.prop_fd)
    return pts


def _percentile(sorted_xs: list[float], p: float) -> float:
    n = len(sorted_xs)
    if n == 1:
        return sorted_xs[0]
    idx = p * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    w = idx - lo
    return sorted_xs[lo] * (1.0 - w) + sorted_xs[hi] * w


def _stats(draws: list[float], *, n: int, source: str) -> SimStats:
    draws.sort()
    return SimStats(
        mean=sum(draws) / n,
        p10=_percentile(draws, 0.10),
        p50=_percentile(draws, 0.50),
        p90=_percentile(draws, 0.90),
        n=n,
        source=source,
        p95=_percentile(draws, 0.95),
        p99=_percentile(draws, 0.99),
    )
