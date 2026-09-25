"""Layered game Monte Carlo for FanDuel-point draws.

One world per slate draw. Games are independent. Inside a game the layers
share that world:

1. Game — Vegas total + home spread. Sigma scales with team EPA variance
   when team stats are present; otherwise the fixed constants below.
2. Volume + script — team plays and a pass/rush split. Plays per game,
   when that column is present, is shrunk toward 63 and blended with the
   opponent's defensive plays faced and with seconds per play (league
   29.80, neutral 32.34). The pace leg is clamped to ±15%. Neutral pass
   rate (or pass rate minus PROE) shifts with the drawn
   margin: trailing teams pass more, leading teams run more. Passing yards and TDs are anchored
   to the Vegas implied total times that pass rate (a passing prop replaces
   the matching anchor). Team rush attempts come from team_stats plus the
   same script, then a yard budget tied to the implied total.
3. Opportunity — target shares (WR/TE/RB) and RB rush shares drawn jointly
   (Dirichlet) from games played so same-team catchers compete. Rush shares
   blend snap rate and carry share across every roster RB. The starter QB's
   passing yards and TDs are the sum of those same receiving lines after
   they are scaled to the pass anchor. Backups get no team passing volume.
   A rush-yard prop sets that RB's rush-yard mean. Missing history keeps
   the deterministic role share (team points × depth × position share × usage)
   except the passing QB. With no catcher history that QB is the pass-yard
   and pass-TD anchors (scaled by drawn team points / implied) plus a
   rush-yard floor. With catcher history the same passing floor is the
   implied-total anchor, and the rush count is his own carries or at least
   max(league rush share, that yard floor). Other QBs on that team score 0.
   O, D, IR, and NA do not keep the starter job, and they do not draw
   target or rush share. That share is renormalized onto active teammates.

Efficiency (yards per opportunity, TD rates) is ``PlaceholderEfficiency``
or ``DataEfficiency`` in ``nfl/sim_efficiency.py``. ``DataEfficiency`` is
layer 4 (shrunk player rates and a clamped opponent). It is not a
prop-line calibration. ``simulate_games`` still defaults to the
placeholder so empty inputs and existing callers keep the same draws.
The CLI flag is ``--sim-efficiency {placeholder,data}`` (default
``placeholder`` until data mode beats it). Data mode keeps team rush
attempts on the implied script and redistributes the rush-yard budget.
It does not also scale attempts.

Inputs are ``SimInputs`` (team stats, weekly targets, optional snaps).
The sim does not call Gangstash. Empty inputs reproduce the role-share
fallback and do not add RNG draws.

Not a play-by-play copula and not SaberSim.

`--board` stays the point estimate (implied×depth×share×usage, ±20% prop tilt).
Default ILP (`mean`) stays `week1_score` (`--projection-source board`).
`--projection-source sim` sets `mean` to each player's simulated mean.
`floor` / `ceiling` replace `Player.objective` with that player's sim p10 / p90.
Lineup Fl/Cl are the joint 9 (sum in the same world), not the sum of
player p10s.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import random
from dataclasses import dataclass, field, replace

from nfl.injuries import is_inactive
from nfl.names import match_key
from nfl.players import Player
from nfl.projections import (
    POS_FD_SHARE,
    depth_prior,
    expected_snap_share,
    prop_factor,
    score_player,
    usage_factor,
)
from nfl.rules import (
    DST_SACK_TO_PRIOR,
    FANDUEL_NFL,
    dst_pa_points,
    dst_projection,
    skill_fd_points,
)
from nfl.sim_efficiency import (
    INT_RATE,
    PASS_YPA,
    QB_RUSH_TD_RATE,
    QB_YPC,
    YARDS_PER_RUSH,
    EfficiencyModel,
    OpportunityCount,
    PlaceholderEfficiency,
    ReceivingLine,
    expected_receiving_line,
    clamp,
    sample_yards,
    shrink,
)
from nfl.sim_inputs import CarryWeek, SimInputs, SnapWeek, TargetWeek, TeamStat

DEFAULT_DRAWS = 10_000
DEFAULT_SEED = 1
ILP_OBJECTIVES = ("mean", "floor", "ceiling")
SIM_OBJECTIVES = frozenset({"floor", "ceiling"})

# Yards: 0.25 × line, sigma floor 10. TDs / receptions: 0.5.
# Used only by _props_draw (unit tests); pool path scales lines, no resample.
YARDS_SIGMA_FRAC = 0.25
YARDS_SIGMA_FLOOR = 10.0
COUNT_SIGMA = 0.5
# Model: team points ~ Normal(implied, 0.20 × implied), floor 3.
# Used for solo / unparsed games and simulate_player (single-player tests).
TEAM_SIGMA_FRAC = 0.20
TEAM_POINTS_FLOOR = 3.0
# DEF PA: same sigma; floor 0 (points allowed cannot go negative).
PA_FLOOR = 0.0
# Game draw: total and home spread. Tighter than CFB (0.15 / 16).
# These are the fallback when team EPA variance is missing, and the
# scale=1 point of the EPA dispersion map.
TOTAL_SIGMA_FRAC = 0.12
SPREAD_SIGMA = 10.0
# Per-play EPA sd that leaves total/spread sigma at the constants above.
EPA_SD_REF = 1.15
EPA_SCALE_MIN = 0.60
EPA_SCALE_MAX = 1.80
# Layer 2. Pass rates are fractions.
LEAGUE_PLAYS = 63.0
PLAYS_SIGMA = 4.0
PLAYS_FLOOR = 40.0
# Plays per game shrink toward 63. One game of 70 becomes (70 + 252) / 5.
PLAYS_PRIOR_GAMES = 4.0
# League offense pace. Seconds per play are play_seconds / timed_plays.
LEAGUE_SECONDS_PER_PLAY = 29.80
LEAGUE_NEUTRAL_SECONDS_PER_PLAY = 32.34
PACE_CLAMP = 0.15
LEAGUE_NEUTRAL_PASS_RATE = 0.57
# +1.2 percentage points of pass rate per point of deficit.
SCRIPT_PASS_PER_POINT = 0.012
PASS_RATE_MIN = 0.38
PASS_RATE_MAX = 0.78
DEFAULT_TARGETS_PER_ATTEMPT = 0.90
# Dirichlet concentration when a player has fewer than two weeks.
DEFAULT_SHARE_KAPPA = 10.0
KAPPA_MIN = 2.0
KAPPA_MAX = 80.0
# League-average share of team rushes for a starter QB. RBs split the rest.
# On the opportunity path the starter takes his own carry history when he
# has it, otherwise at least this share and the yard floor below.
QB_RUSH_SHARE = 0.08
# Implied 17.5 used to be team points × 0.50 (about 8.8, p90 about 11.4).
# The passing QB now adds this rush floor on every path: max(12, 0.8 ×
# implied) yards, or the rush-yard prop. The passing floor is the
# implied-total yard and TD anchor.
QB_RUSH_YARDS_FLOOR = 12.0
QB_RUSH_YARDS_PER_IMPLIED = 0.8
# Passing yards ≈ implied total × scripted pass rate × this.
# 22-pt team at a 0.57 pass rate → about 220 yards.
PASS_YARDS_PER_POINT = 17.5
# Team rush yards ≈ implied total × scripted rush rate × this.
# 22-pt team at a 0.43 rush rate → about 90 rush yards, then split.
RUSH_YARDS_PER_POINT = 9.5
# Passing TDs ≈ (implied / 7) × clamp(pass_rate × slope).
PASS_TD_SHARE_SLOPE = 1.08
PASS_TD_SHARE_MIN = 0.40
PASS_TD_SHARE_MAX = 0.82

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

BONUS_THRESHOLDS: tuple[tuple[str, str, float], ...] = (
    ("pass_yd", "bonus_pass_yd_300", 300.0),
    ("rush_yd", "bonus_rush_yd_100", 100.0),
    ("rec_yd", "bonus_rec_yd_100", 100.0),
)


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
        return {
            "mean": round(self.mean, 4),
            "p10": p10,
            "p50": round(self.p50, 4),
            "p90": p90,
            "p95": round(self.p95, 4),
            "p99": round(self.p99, 4),
            "floor": p10,
            "ceiling": p90,
        }


@dataclass(frozen=True)
class GameSim:
    """Aligned draws: index t is one slate world."""

    by_pid: dict[str, SimStats]
    draws: dict[str, tuple[float, ...]]
    # Teams whose catchers had weekly target history (Dirichlet shares).
    opportunity_teams: frozenset[str] = field(default_factory=frozenset)

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
        f"sim {int(n)} layered draws — game total+spread "
        "(EPA dispersion when team stats exist), volume/script, "
        "opportunity shares. Teammates share the world. Not a PBP copula."
    )


def has_volume_props(player: Player) -> bool:
    """True when any pass/rush/rec yard, reception, or pass-TD line is set."""
    return any(getattr(player, attr) is not None for attr in VOLUME_ATTRS)


def yardage_bonuses(drawn_yds: dict[str, float]) -> float:
    """+3 each if that draw's pass ≥ 300, rush ≥ 100, rec ≥ 100."""
    sc = FANDUEL_NFL.scoring
    pts = 0.0
    for key, bonus_key, thresh in BONUS_THRESHOLDS:
        if key in drawn_yds and drawn_yds[key] >= thresh:
            pts += sc[bonus_key]
    return pts


def volume_point(player: Player) -> float | None:
    """Point-estimate FD from attached volume lines (bonuses on the *line*)."""
    if not has_volume_props(player):
        return None
    sc = FANDUEL_NFL.scoring
    pts = 0.0
    drawn: dict[str, float] = {}
    for attr, key in YARD_FIELDS + COUNT_FIELDS:
        val = getattr(player, attr)
        if val is not None:
            pts += float(val) * sc[key]
            if key in {"pass_yd", "rush_yd", "rec_yd"}:
                drawn[key] = float(val)
    return pts + yardage_bonuses(drawn)


def model_point(player: Player) -> float:
    """Implied × depth × share × usage. DEF: PA bucket + sack/TO prior. No prop tilt."""
    pos = (player.position or "WR").upper()
    if pos in {"D", "DEF"}:
        return dst_projection(float(player.implied_opp or 0.0))
    implied = float(player.implied_total or 0.0)
    share = POS_FD_SHARE.get(pos, 0.18)
    return (
        implied
        * depth_prior(player.depth_rank)
        * share
        * usage_factor(
            player.target_share,
            player.position,
            player.depth_rank,
            snap_share=player.snap_share,
        )
    )


def simulate_player(
    player: Player,
    *,
    n: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
) -> SimStats:
    """n independent FD-point draws for one player.

    Unit-test / single-player path. Pool `--sim` is `simulate_games`.
    """
    if n <= 0:
        raise ValueError("n must be > 0")
    rng = random.Random(_player_seed(seed, player.pid))
    props = has_volume_props(player) and not _is_dst(player)
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
    inputs: SimInputs | None = None,
    efficiency: EfficiencyModel | None = None,
) -> GameSim:
    """n slate worlds. Players in a game share total+margin; games do not.

    ``inputs`` turns on EPA dispersion, scripted volume, and Dirichlet
    shares. ``None`` or an empty bundle keeps deterministic role shares
    and the fixed total/spread sigmas (same RNG steps as before).
    """
    if n <= 0:
        return GameSim(by_pid={}, draws={})
    bundle = inputs or SimInputs()
    index = _HistoryIndex(bundle)
    eff = efficiency or PlaceholderEfficiency()
    groups: dict[str, list[Player]] = {}
    for pl in players:
        groups.setdefault(_game_key(pl), []).append(pl)
    for key in groups:
        groups[key].sort(key=lambda p: p.pid)
    opportunity = frozenset(
        team
        for group in groups.values()
        for team in _teams_in(group)
        if _catchers(team, group, index)
    )
    rng = random.Random(int(seed))
    raw: dict[str, list[float]] = {p.pid: [] for p in players}
    for _ in range(n):
        for key in sorted(groups):
            group = groups[key]
            home_pts, away_pts, away, home = _draw_game(rng, group, index)
            if away is None or home is None:
                for pl in group:
                    team_pts, opp_pts = _solo_world(rng, pl)
                    raw[pl.pid].append(
                        _score_fallback(pl, team_pts, opp_pts, group, index)
                    )
                continue
            counts: dict[str, OpportunityCount] = {}
            for team in sorted(_teams_in(group)):
                if team not in opportunity:
                    continue
                margin = _team_margin(team, home_pts, away_pts, away, home)
                counts.update(
                    _draw_team_opportunities(
                        rng,
                        [p for p in group if (p.team or "").upper() == team],
                        margin,
                        index,
                        eff,
                    )
                )
            for pl in group:
                opp_count = counts.get(pl.pid)
                if opp_count is None:
                    team_pts, opp_pts = _player_world(
                        pl, home_pts, away_pts, away, home
                    )
                    raw[pl.pid].append(
                        _score_fallback(pl, team_pts, opp_pts, group, index)
                    )
                else:
                    raw[pl.pid].append(eff.points(rng, pl, opp_count))
    draws = {pid: tuple(xs) for pid, xs in raw.items()}
    layered = _layered_pids(groups, opportunity, index)
    by_pid: dict[str, SimStats] = {}
    for pl in players:
        xs = list(draws[pl.pid])
        if pl.pid in layered:
            src = "game"
        elif has_volume_props(pl) and not _is_dst(pl):
            src = "props"
        else:
            src = "model"
        by_pid[pl.pid] = _stats(xs, n=n, source=src)
    return GameSim(by_pid=by_pid, draws=draws, opportunity_teams=opportunity)


def apply_ilp_objective(
    players: list[Player],
    kind: str,
    *,
    sim_by_pid: dict[str, SimStats] | None = None,
    lean_pids: frozenset[str] | None = None,
    lean_mult: float = 1.0,
    projection_source: str = "board",
) -> list[Player]:
    """Set each player's ILP `objective`.

    `mean` + `board` (default): leave `week1_score`.
    `mean` + `sim`: that player's simulated mean. Missing sim stats keep
    the board score.
    `floor` / `ceiling`: sim p10 / p90 from the same draws, either source.
    Optional `lean_mult` for `lean_pids` applies only to floor/ceiling.
    Ceiling stays p90 (not NCAAF p99).
    """
    kind = (kind or "mean").lower()
    source = (projection_source or "board").lower()
    if source not in ("board", "sim"):
        raise ValueError(f"unknown projection source {projection_source!r}")
    if kind not in ILP_OBJECTIVES:
        raise ValueError(f"unknown objective {kind!r}")
    if kind == "mean" and source != "sim":
        return list(players)
    by_pid = sim_by_pid or {}
    leaned = lean_pids or frozenset()
    if kind == "floor":
        attr = "p10"
    elif kind == "ceiling":
        attr = "p90"
    else:
        attr = "mean"
    out: list[Player] = []
    for pl in players:
        st = by_pid.get(pl.pid)
        if st is None:
            out.append(pl)
            continue
        val = float(getattr(st, attr))
        if kind != "mean" and pl.pid in leaned:
            val *= lean_mult
        if val != pl.objective:
            out.append(replace(pl, objective=val))
        else:
            out.append(pl)
    return out


def _is_dst(player: Player) -> bool:
    return (player.position or "").upper() in {"D", "DEF"}


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
    rng: random.Random,
    group: list[Player],
    index: _HistoryIndex | None = None,
) -> tuple[float, float, str | None, str | None]:
    total_mu, spread_home_mu, away, home = _vegas(group)
    if away is None or home is None:
        # Unparsed tag: independent team totals. No RNG here.
        return (0.0, 0.0, None, None)
    home_var = away_var = None
    if index is not None and away and home:
        home_var = index.scoring_var(home, away)
        away_var = index.scoring_var(away, home)
    total_sigma, spread_sigma = game_sigmas(total_mu, home_var, away_var)
    total_d = _gauss_floor(
        rng,
        total_mu,
        total_sigma,
        2.0 * TEAM_POINTS_FLOOR,
    )
    if spread_sigma <= 0:
        spread_d = spread_home_mu
    else:
        spread_d = rng.gauss(spread_home_mu, spread_sigma)
    home_pts = max(TEAM_POINTS_FLOOR, (total_d - spread_d) / 2.0)
    away_pts = max(TEAM_POINTS_FLOOR, (total_d + spread_d) / 2.0)
    return home_pts, away_pts, away, home


def _player_world(
    player: Player,
    home_pts: float,
    away_pts: float,
    away: str | None,
    home: str | None,
) -> tuple[float, float]:
    team = (player.team or "").upper()
    if home and team == home:
        return home_pts, away_pts
    if away and team == away:
        return away_pts, home_pts
    implied = float(player.implied_total or 0.0)
    opp = float(player.implied_opp or 0.0)
    return implied, opp


def _solo_world(rng: random.Random, player: Player) -> tuple[float, float]:
    implied = float(player.implied_total or 0.0)
    implied_opp = float(player.implied_opp or 0.0)
    if _is_dst(player):
        opp_pts = _gauss_floor(
            rng,
            implied_opp,
            TEAM_SIGMA_FRAC * abs(implied_opp),
            PA_FLOOR,
        )
        return implied, opp_pts
    team_pts = _gauss_floor(
        rng,
        implied,
        TEAM_SIGMA_FRAC * abs(implied),
        TEAM_POINTS_FLOOR,
    )
    return team_pts, implied_opp


def starter_qb_rush_yards(player: Player) -> float:
    """Rush-yard floor for the passing QB. A rush prop replaces it."""
    if player.prop_rush_yds is not None:
        return max(0.0, float(player.prop_rush_yds))
    implied = float(player.implied_total or 0.0)
    return max(QB_RUSH_YARDS_FLOOR, implied * QB_RUSH_YARDS_PER_IMPLIED)


def starter_qb_rush_attempts(
    player: Player | None,
    team_rushes: float,
    index: _HistoryIndex,
) -> float:
    """Rush attempts for the passing QB on the opportunity path.

    Own carry history wins. Otherwise a snap share scales the league rush
    share. The result is at least that share and at least the yard floor
    (``starter_qb_rush_yards`` / yards per carry). Passing yards stay the
    scaled receiving lines.
    """
    league = max(0.0, float(team_rushes)) * QB_RUSH_SHARE
    if player is None:
        return league
    carries = index.mean_carries(player)
    if carries is not None and carries > 0:
        hist = float(carries)
    else:
        snap = index.snap_mean(player)
        if snap is not None and snap > 0:
            hist = league * min(1.0, float(snap))
        else:
            hist = league
    yards = starter_qb_rush_yards(player)
    floor_att = (yards / QB_YPC) if QB_YPC else 0.0
    return max(hist, floor_att)


def starter_qb_points(
    player: Player,
    team_pts: float,
    index: _HistoryIndex | None = None,
) -> float:
    """FanDuel points for the passing QB when catcher history is missing.

    Anchors use the neutral pass rate (not the drawn margin) and scale by
    ``team_pts / implied``, so the score stays linear in team points.
    """
    implied = float(player.implied_total or 0.0)
    scale = (float(team_pts) / implied) if implied > 0 else 1.0
    offense = None
    if index is not None:
        offense = index.offense((player.team or "").upper())
    rate = neutral_pass_rate_of(offense)
    yards = pass_yard_anchor(implied, rate, player.prop_pass_yds) * scale
    tds = pass_td_anchor(implied, rate, player.prop_pass_tds) * scale
    rush = starter_qb_rush_yards(player) * scale
    attempts = (yards / PASS_YPA) if PASS_YPA else 0.0
    rushes = (rush / QB_YPC) if QB_YPC else 0.0
    return max(
        0.0,
        skill_fd_points(
            pass_yd=yards,
            pass_td=tds,
            interceptions=attempts * INT_RATE,
            rush_yd=rush,
            rush_td=rushes * QB_RUSH_TD_RATE,
        ),
    )


def _score_fallback(
    player: Player,
    team_pts: float,
    opp_pts: float,
    group: list[Player],
    index: _HistoryIndex | None,
) -> float:
    """Role share, except the passing QB (anchors) and his backups (0).

    O, D, IR, and NA score 0. They are already out of the target and rush
    shares, so a fallback must not put a role-share stub back on them.
    """
    if is_inactive(player) and not _is_dst(player):
        return 0.0
    if (player.position or "").upper() != "QB":
        return _score_world(player, team_pts, opp_pts)
    team = (player.team or "").upper()
    mates = [p for p in group if (p.team or "").upper() == team] or [player]
    starter = passing_qb(mates)
    if starter is None:
        return _score_world(player, team_pts, opp_pts)
    if starter.pid != player.pid:
        return 0.0
    return starter_qb_points(player, team_pts, index)


def _score_world(player: Player, team_pts: float, opp_pts: float) -> float:
    if _is_dst(player):
        return dst_pa_points(opp_pts) + DST_SACK_TO_PRIOR
    share = POS_FD_SHARE.get((player.position or "WR").upper(), 0.18)
    pts = (
        float(team_pts)
        * depth_prior(player.depth_rank)
        * share
        * usage_factor(
            player.target_share,
            player.position,
            player.depth_rank,
            snap_share=player.snap_share,
        )
    )
    if has_volume_props(player):
        pts *= prop_factor(model_point(player), player.prop_fd)
        implied = float(player.implied_total or 0.0)
        scale = (float(team_pts) / implied) if implied > 0 else 1.0
        drawn: dict[str, float] = {}
        for attr, key in YARD_FIELDS:
            line = getattr(player, attr)
            if line is not None:
                drawn[key] = float(line) * scale
        pts += yardage_bonuses(drawn)
    return max(0.0, pts)


def _one_draw(rng: random.Random, player: Player, props: bool) -> float:
    if _is_dst(player):
        return _dst_draw(rng, player)
    implied = float(player.implied_total or 0.0)
    sigma = TEAM_SIGMA_FRAC * abs(implied)
    team_pts = _gauss_floor(rng, implied, sigma, TEAM_POINTS_FLOOR)
    share = POS_FD_SHARE.get((player.position or "WR").upper(), 0.18)
    pts = (
        team_pts
        * depth_prior(player.depth_rank)
        * share
        * usage_factor(
            player.target_share,
            player.position,
            player.depth_rank,
            snap_share=player.snap_share,
        )
    )
    if props:
        pts *= prop_factor(model_point(player), player.prop_fd)
    return pts


def _dst_draw(rng: random.Random, player: Player) -> float:
    implied_opp = float(player.implied_opp or 0.0)
    sigma = TEAM_SIGMA_FRAC * abs(implied_opp)
    pa = _gauss_floor(rng, implied_opp, sigma, PA_FLOOR)
    return dst_pa_points(pa) + DST_SACK_TO_PRIOR


def _props_draw(rng: random.Random, player: Player) -> float:
    sc = FANDUEL_NFL.scoring
    pts = 0.0
    drawn: dict[str, float] = {}
    for attr, key in YARD_FIELDS:
        line = getattr(player, attr)
        if line is None:
            continue
        mu = float(line)
        sigma = max(YARDS_SIGMA_FLOOR, YARDS_SIGMA_FRAC * abs(mu))
        yds = _gauss_floor(rng, mu, sigma, 0.0)
        drawn[key] = yds
        pts += yds * sc[key]
    for attr, key in COUNT_FIELDS:
        line = getattr(player, attr)
        if line is None:
            continue
        pts += _gauss_floor(rng, float(line), COUNT_SIGMA, 0.0) * sc[key]
    return pts + yardage_bonuses(drawn)


def dispersion_scale(var: float | None) -> float:
    """Map per-play EPA variance onto a multiplier for the fallback sigmas.

    ``None`` or 0 → 1 (today's constants). Otherwise
    ``sqrt(var) / EPA_SD_REF``, clamped.
    """
    if var is None or var <= 0:
        return 1.0
    scale = math.sqrt(var) / EPA_SD_REF
    return min(EPA_SCALE_MAX, max(EPA_SCALE_MIN, scale))


def game_sigmas(
    total_mu: float,
    home_var: float | None,
    away_var: float | None,
) -> tuple[float, float]:
    """``(total_sigma, spread_sigma)`` for one game.

    Home and away scoring variances are turned into scales and combined
    by RMS. Missing variances use scale 1.
    """
    home_s = dispersion_scale(home_var)
    away_s = dispersion_scale(away_var)
    scale = math.sqrt((home_s * home_s + away_s * away_s) / 2.0)
    return TOTAL_SIGMA_FRAC * abs(total_mu) * scale, SPREAD_SIGMA * scale


def neutral_pass_rate_of(stat: TeamStat | None) -> float:
    """Situation-neutral pass rate for layer 2.

    Prefer ``neutral_pass_rate`` and do not add PROE on top of it.
    Else ``pass_rate - proe`` (strip the tendency the script will reapply).
    Else ``pass_rate``, else league + PROE, else the league constant.
    """
    if stat is None:
        return LEAGUE_NEUTRAL_PASS_RATE
    if stat.neutral_pass_rate is not None:
        return _clamp_rate(stat.neutral_pass_rate)
    if stat.pass_rate is not None and stat.proe is not None:
        return _clamp_rate(stat.pass_rate - stat.proe)
    if stat.pass_rate is not None:
        return _clamp_rate(stat.pass_rate)
    if stat.proe is not None:
        return _clamp_rate(LEAGUE_NEUTRAL_PASS_RATE + stat.proe)
    return LEAGUE_NEUTRAL_PASS_RATE


def scripted_pass_rate(neutral: float, margin: float) -> float:
    """Trailing (negative margin) passes more. Leading runs more."""
    return _clamp_rate(neutral + SCRIPT_PASS_PER_POINT * (-float(margin)))


def scripted_rush_rate(stat: TeamStat | None, margin: float) -> float:
    """Team rush rate from the neutral split, shifted by the game script.

    ``rush_n / (pass_n + rush_n)`` is the team_stats base when both counts
    exist. The script then adds the same pass-rate move ``scripted_pass_rate``
    applies (a lead raises the rush rate). Otherwise the rate is
    ``1 - scripted pass rate``.
    """
    neutral = neutral_pass_rate_of(stat)
    pass_rate = scripted_pass_rate(neutral, margin)
    rush_rate = 1.0 - pass_rate
    if stat is not None and stat.pass_n and stat.rush_n:
        total = int(stat.pass_n) + int(stat.rush_n)
        if total > 0:
            base = int(stat.rush_n) / total
            shifted = base + (neutral - pass_rate)
            rush_rate = min(1.0 - PASS_RATE_MIN, max(1.0 - PASS_RATE_MAX, shifted))
    return rush_rate


def _shrunk_plays(stat: TeamStat | None) -> float | None:
    if stat is None or stat.plays_per_game is None:
        return None
    games = float(stat.pace_games) if stat.pace_games else 1.0
    return shrink(float(stat.plays_per_game), games, LEAGUE_PLAYS, PLAYS_PRIOR_GAMES)


def _pace_plays(seconds: float | None, games: float, league_seconds: float) -> float | None:
    """Plays implied by pace. Faster than the league means more plays.

    The seconds rate is shrunk toward the league, then the play ratio is
    clamped to ±``PACE_CLAMP``.
    """
    if seconds is None or float(seconds) <= 0 or league_seconds <= 0:
        return None
    shrunk = shrink(float(seconds), games, league_seconds, PLAYS_PRIOR_GAMES)
    if shrunk <= 0:
        return None
    ratio = clamp(league_seconds / shrunk, 1.0 - PACE_CLAMP, 1.0 + PACE_CLAMP)
    if ratio == 1.0:
        return LEAGUE_PLAYS
    return LEAGUE_PLAYS * ratio


def _side_volume(stat: TeamStat | None) -> list[float]:
    """Shrunk plays per game, plus one pace leg from the seconds columns."""
    if stat is None:
        return []
    games = float(stat.pace_games) if stat.pace_games else 1.0
    parts: list[float] = []
    plays = _shrunk_plays(stat)
    if plays is not None:
        parts.append(plays)
    pace: list[float] = []
    neutral = _pace_plays(
        stat.neutral_seconds_per_play, games, LEAGUE_NEUTRAL_SECONDS_PER_PLAY
    )
    overall = _pace_plays(stat.seconds_per_play, games, LEAGUE_SECONDS_PER_PLAY)
    if neutral is not None:
        pace.append(neutral)
    if overall is not None:
        pace.append(overall)
    if pace:
        parts.append(sum(pace) / len(pace))
    return parts


def offense_plays_mu(stat: TeamStat | None, defense: TeamStat | None = None) -> float:
    """Plays per game.

    ``plays_per_game`` on the offense, blended with the opponent's defensive
    plays faced and with seconds per play on both sides. Each plays figure
    shrinks toward 63 with a 4-game prior. Pace is clamped to ±15% of 63.
    With none of those columns, a pass+rush count in the one-game range
    wins; otherwise 63.
    """
    parts = _side_volume(stat) + _side_volume(defense)
    if parts:
        return sum(parts) / len(parts)
    if stat is None or not stat.pass_n or not stat.rush_n:
        return LEAGUE_PLAYS
    total = int(stat.pass_n) + int(stat.rush_n)
    if 40 <= total <= 95:
        return float(total)
    return LEAGUE_PLAYS


def pass_yard_anchor(
    implied: float,
    pass_rate: float,
    prop: float | None = None,
) -> float:
    """Expected team passing yards. A passing-yard prop replaces the anchor."""
    if prop is not None:
        return max(0.0, float(prop))
    return max(0.0, float(implied)) * max(0.0, float(pass_rate)) * PASS_YARDS_PER_POINT


def pass_td_anchor(
    implied: float,
    pass_rate: float,
    prop: float | None = None,
) -> float:
    """Expected team passing TDs. A passing-TD prop replaces the anchor."""
    if prop is not None:
        return max(0.0, float(prop))
    share = min(
        PASS_TD_SHARE_MAX,
        max(PASS_TD_SHARE_MIN, float(pass_rate) * PASS_TD_SHARE_SLOPE),
    )
    return max(0.0, float(implied)) / 7.0 * share


def team_rush_attempts(plays: float, rush_rate: float, implied: float) -> float:
    """Rush attempts from the script, capped by the implied-total yard budget."""
    scripted = max(0.0, float(plays)) * max(0.0, float(rush_rate))
    ypc = YARDS_PER_RUSH.get("RB", 4.4)
    if float(implied) <= 0 or ypc <= 0:
        return scripted
    budget = float(implied) * max(0.0, float(rush_rate)) * RUSH_YARDS_PER_POINT / ypc
    return min(scripted, max(0.0, budget))


def mean_target_share(
    weeks: list[TargetWeek],
    snaps: list[SnapWeek] | None = None,
) -> float:
    """Mean share over games played. Zero-filled DNPs are dropped.

    A week counts when it has targets or a positive ``target_share``.
    With no snap row, a zero-target week is a DNP and is left out, so a
    player who missed a week is not diluted by that zero. A snap week
    with ``offense_pct > 0`` counts even at zero targets (he played).
    ``offense_pct == 0`` drops a zero-target week. No played weeks → 0,
    and the player stays on the depth role share.
    """
    shares = _played_shares(weeks, snaps)
    if not shares:
        return 0.0
    return sum(shares) / len(shares)


def share_kappa(series: list[list[float]]) -> float:
    """Method-of-moments Dirichlet concentration, median across players.

    ``Var(s) = μ(1-μ)/(κ+1)`` so ``κ = μ(1-μ)/Var - 1``. One week (no
    sample variance) uses ``DEFAULT_SHARE_KAPPA``.
    """
    found: list[float] = []
    for shares in series:
        if len(shares) < 2:
            continue
        mu = sum(shares) / len(shares)
        var = sum((x - mu) ** 2 for x in shares) / (len(shares) - 1)
        if var <= 1e-8 or mu <= 0.0 or mu >= 1.0:
            continue
        found.append(mu * (1.0 - mu) / var - 1.0)
    if not found:
        return DEFAULT_SHARE_KAPPA
    found.sort()
    mid = found[len(found) // 2]
    return min(KAPPA_MAX, max(KAPPA_MIN, mid))


def draw_simplex(
    rng: random.Random,
    means: list[float],
    kappa: float,
) -> list[float]:
    """Dirichlet draw via independent gammas. ``means`` should sum to 1."""
    if not means:
        return []
    total_mean = sum(means)
    if total_mean <= 0:
        even = 1.0 / len(means)
        base = [even for _ in means]
    else:
        base = [m / total_mean for m in means]
    alphas = [max(m, 1e-6) * max(kappa, KAPPA_MIN) for m in base]
    gammas = [rng.gammavariate(a, 1.0) for a in alphas]
    total = sum(gammas)
    if total <= 0:
        return base
    return [g / total for g in gammas]


def rush_share_means(
    players: list[Player],
    snap_means: dict[str, float | None],
) -> tuple[dict[str, float], bool]:
    """Normalized RB rush weights. Second value is True when any snaps exist.

    No snaps → depth role weights (deterministic). Snaps → ``offense_pct``
    for RBs that have it, depth weight for RBs that do not.
    """
    rbs = [p for p in players if (p.position or "").upper() == "RB"]
    if not rbs:
        return {}, False
    raw: dict[str, float] = {}
    any_snaps = False
    for pl in rbs:
        snap = snap_means.get(pl.pid)
        if snap is not None and snap > 0:
            any_snaps = True
            raw[pl.pid] = float(snap)
        else:
            raw[pl.pid] = _rush_role_weight(pl)
    total = sum(raw.values())
    if total <= 0:
        even = 1.0 / len(rbs)
        return {pl.pid: even for pl in rbs}, any_snaps
    return {pid: val / total for pid, val in raw.items()}, any_snaps


def blended_rush_shares(
    players: list[Player],
    snap_means: dict[str, float | None],
    carry_means: dict[str, float | None],
) -> tuple[dict[str, float], bool]:
    """RB rush shares from snap rate and carry rate. Sum to 1.

    Snaps and carries are normalized on their own, then combined with a
    geometric mean when a back has both. A back with only one signal keeps
    that signal. No snaps and no carries → the depth role weights, and the
    second value is False (no Dirichlet draw).
    """
    rbs = [p for p in players if (p.position or "").upper() == "RB"]
    snap_shares, any_snaps = rush_share_means(rbs, snap_means)
    carry_raw = {pl.pid: float(carry_means.get(pl.pid) or 0.0) for pl in rbs}
    any_carries = any(val > 0 for val in carry_raw.values())
    if not any_carries:
        return snap_shares, any_snaps
    carry_total = sum(carry_raw.values())
    carry_shares = {
        pid: (val / carry_total if carry_total > 0 else 0.0)
        for pid, val in carry_raw.items()
    }
    blended: dict[str, float] = {}
    for pl in rbs:
        snap = snap_shares.get(pl.pid, 0.0)
        carry = carry_shares.get(pl.pid, 0.0)
        if snap > 0 and carry > 0:
            blended[pl.pid] = math.sqrt(snap * carry)
        elif carry > 0:
            blended[pl.pid] = carry
        else:
            blended[pl.pid] = snap
    total = sum(blended.values())
    if total <= 0:
        return snap_shares, any_snaps or any_carries
    return {pid: val / total for pid, val in blended.items()}, True


def pearson(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((xs[i] - mx) ** 2 for i in range(n))
    dy = sum((ys[i] - my) ** 2 for i in range(n))
    den = math.sqrt(dx * dy)
    return 0.0 if den == 0 else num / den


def format_board_vs_sim(
    players: list[Player],
    game_sim: GameSim,
    *,
    projection_source: str = "board",
    limit: int = 8,
) -> str:
    """Largest |sim mean − week1 board| gaps.

    Board is ``score_player`` (week1_score), not the ILP objective, so the
    comparison still holds after ``--projection-source sim`` replaces it.
    """
    ranked: list[tuple[float, float, float, float, Player]] = []
    for pl in players:
        st = game_sim.by_pid.get(pl.pid)
        if st is None:
            continue
        board = float(score_player(pl))
        delta = float(st.mean) - board
        ranked.append((abs(delta), delta, board, float(st.mean), pl))
    ranked.sort(key=lambda row: (-row[0], row[4].pid))
    shown = ranked[: max(0, int(limit))]
    lines = [
        f"board vs sim (top {len(shown)} movers by |sim mean − board|)",
        "player  pos team     board     sim    delta",
    ]
    for _abs, delta, board, mean, pl in shown:
        lines.append(
            f"{pl.name}  {pl.position:<3} {(pl.team or ''):<5} "
            f"{board:7.2f} {mean:7.2f} {delta:+7.2f}"
        )
    if not shown:
        lines.append("(no sim rows)")
    source = (projection_source or "board").lower()
    if source == "sim":
        lines.append(
            "ILP mean uses sim mean; board stays week1_score for comparison"
        )
    else:
        lines.append(
            "ILP mean uses board (week1_score); sim mean is descriptive"
        )
    return "\n".join(lines)


def format_sim_diagnostic(
    players: list[Player],
    game_sim: GameSim,
    *,
    projection_source: str = "board",
) -> str:
    """Per-player p10/p50/p90, correlations, and board-vs-sim movers."""
    lines = ["player  pos team     p10    p50    p90"]
    ordered = sorted(
        players,
        key=lambda p: (
            (p.team or "").upper(),
            (p.position or ""),
            (p.name or "").lower(),
            p.pid,
        ),
    )
    for pl in ordered:
        st = game_sim.by_pid.get(pl.pid)
        if st is None:
            continue
        lines.append(
            f"{pl.name}  {pl.position:<3} {(pl.team or ''):<5} "
            f"{st.p10:7.2f} {st.p50:7.2f} {st.p90:7.2f}"
        )
    lines.append("")
    lines.append(format_correlation_summary(players, game_sim))
    lines.append("")
    lines.append(
        format_board_vs_sim(
            players, game_sim, projection_source=projection_source
        )
    )
    return "\n".join(lines)


def format_correlation_summary(
    players: list[Player],
    game_sim: GameSim,
    *,
    list_pairs: bool = True,
) -> str:
    """Same-team correlation block.

    All-pairs means include bench WRs. Starter means are depth-1 QB vs
    WR depth 1–3 and TE depth 1. Those starter signs are the check:
    QB–WR positive, WR–WR negative.
    """
    pairs = _same_team_pairs(players, game_sim)
    lines: list[str] = ["same-team correlations"]
    if list_pairs:
        for kind, a, b, r in pairs:
            lines.append(f"  {kind}  {a} / {b}  r={r:+.3f}")
    qb_wr = [r for kind, _a, _b, r in pairs if kind == "QB-WR"]
    wr_wr = [r for kind, _a, _b, r in pairs if kind == "WR-WR"]
    lines.append(
        "QB–WR mean r = "
        + (_fmt_mean(qb_wr))
        + f"  (n={len(qb_wr)})"
    )
    lines.append(
        "WR–WR mean r = "
        + (_fmt_mean(wr_wr))
        + f"  (n={len(wr_wr)})"
    )
    starter_pairs = _same_team_pairs(players, game_sim, starters_only=True)
    sqb_wr = [r for kind, _a, _b, r in starter_pairs if kind == "QB-WR"]
    swr_wr = [r for kind, _a, _b, r in starter_pairs if kind == "WR-WR"]
    sqb_te = [r for kind, _a, _b, r in starter_pairs if kind == "QB-TE"]
    lines.append(
        "QB–WR starters mean r = "
        + (_fmt_mean(sqb_wr))
        + f"  (n={len(sqb_wr)})"
    )
    lines.append(
        "WR–WR starters mean r = "
        + (_fmt_mean(swr_wr))
        + f"  (n={len(swr_wr)})"
    )
    lines.append(
        "QB–TE starters mean r = "
        + (_fmt_mean(sqb_te))
        + f"  (n={len(sqb_te)})"
    )
    lines.append("starters: depth-1 QB, WR depth 1–3, TE depth 1")
    if game_sim.opportunity_teams:
        teams = ", ".join(sorted(game_sim.opportunity_teams))
        lines.append(f"opportunity shares on: {teams}")
    else:
        lines.append(
            "opportunity shares off — no weekly target history; "
            "role shares deterministic"
        )
    return "\n".join(lines)


def _fmt_mean(xs: list[float]) -> str:
    if not xs:
        return "n/a"
    return f"{sum(xs) / len(xs):+.3f}"


def _same_team_pairs(
    players: list[Player],
    game_sim: GameSim,
    *,
    starters_only: bool = False,
) -> list[tuple[str, str, str, float]]:
    """QB–WR and WR–WR pairs. Starters: depth-1 QB, WR 1–3, TE 1 (QB–TE)."""
    by_team: dict[str, list[Player]] = {}
    for pl in players:
        if pl.pid not in game_sim.draws:
            continue
        by_team.setdefault((pl.team or "").upper(), []).append(pl)
    out: list[tuple[str, str, str, float]] = []
    for team in sorted(by_team):
        group = sorted(by_team[team], key=lambda p: p.pid)
        qbs = [p for p in group if (p.position or "").upper() == "QB"]
        wrs = [p for p in group if (p.position or "").upper() == "WR"]
        tes = [p for p in group if (p.position or "").upper() == "TE"]
        if starters_only:
            qbs = [p for p in qbs if p.depth_rank == 1]
            wrs = [p for p in wrs if p.depth_rank in (1, 2, 3)]
            tes = [p for p in tes if p.depth_rank == 1]
        else:
            tes = []
        for qb in qbs:
            for wr in wrs:
                r = pearson(
                    list(game_sim.draws[qb.pid]),
                    list(game_sim.draws[wr.pid]),
                )
                out.append(("QB-WR", qb.name or qb.pid, wr.name or wr.pid, r))
            for te in tes:
                r = pearson(
                    list(game_sim.draws[qb.pid]),
                    list(game_sim.draws[te.pid]),
                )
                out.append(("QB-TE", qb.name or qb.pid, te.name or te.pid, r))
        for i, a in enumerate(wrs):
            for b in wrs[i + 1 :]:
                r = pearson(
                    list(game_sim.draws[a.pid]),
                    list(game_sim.draws[b.pid]),
                )
                out.append(("WR-WR", a.name or a.pid, b.name or b.pid, r))
    return out


def _clamp_rate(rate: float) -> float:
    return min(PASS_RATE_MAX, max(PASS_RATE_MIN, float(rate)))


def _rush_role_weight(player: Player) -> float:
    snap = expected_snap_share("RB", player.depth_rank)
    return max(depth_prior(player.depth_rank) * max(snap, 0.05), 1e-3)


def _teams_in(group: list[Player]) -> set[str]:
    return {(p.team or "").upper() for p in group if (p.team or "").strip()}


def _team_margin(
    team: str,
    home_pts: float,
    away_pts: float,
    away: str | None,
    home: str | None,
) -> float:
    if home and team == home:
        return home_pts - away_pts
    if away and team == away:
        return away_pts - home_pts
    return 0.0


def _layered_pids(
    groups: dict[str, list[Player]],
    opportunity: frozenset[str],
    index: _HistoryIndex,
) -> set[str]:
    """Players scored by the efficiency model (QB + catchers with history)."""
    pids: set[str] = set()
    for group in groups.values():
        for team in _teams_in(group):
            if team not in opportunity:
                continue
            members = [p for p in group if (p.team or "").upper() == team]
            pids.update(p.pid for p in _catchers(team, members, index))
            pids.update(
                p.pid
                for p in members
                if (p.position or "").upper() in {"QB", "RB"}
            )
    return pids


def passing_qb(players: list[Player]) -> Player | None:
    """The one QB who receives the team's pass attempts.

    Prefer a lone depth-1. Several depth-1 QBs: the one with a passing
    prop (``prop_pass_yds`` or ``prop_pass_tds``), else higher salary,
    else pid. No depth-1: the QB with a passing prop, else the lowest
    depth rank. O, D, IR, and NA are skipped so the next healthy QB
    gets the attempts. Everyone else is a backup and scores ~0.
    """
    qbs = [p for p in players if (p.position or "").upper() == "QB" and not is_inactive(p)]
    if not qbs:
        return None
    depth1 = [p for p in qbs if p.depth_rank == 1]
    if len(depth1) == 1:
        return depth1[0]
    if len(depth1) > 1:
        return min(depth1, key=_qb_tie_break)
    with_prop = [p for p in qbs if _has_pass_prop(p)]
    if with_prop:
        return min(with_prop, key=_qb_tie_break)
    return min(qbs, key=_qb_depth_key)


def _has_pass_prop(player: Player) -> bool:
    return player.prop_pass_yds is not None or player.prop_pass_tds is not None


def _qb_tie_break(player: Player) -> tuple:
    return (
        0 if _has_pass_prop(player) else 1,
        -(player.salary or 0),
        player.pid,
    )


def _qb_depth_key(player: Player) -> tuple:
    rank = player.depth_rank if player.depth_rank is not None else 10**9
    return (rank, -(player.salary or 0), player.pid)


def _target_candidates(
    team: str,
    group: list[Player],
    index: _HistoryIndex,
) -> list[Player]:
    """WR/TE/RB with a positive target share, including inactive players."""
    out: list[Player] = []
    for pl in group:
        if (pl.team or "").upper() != team:
            continue
        if (pl.position or "").upper() not in {"WR", "TE", "RB"}:
            continue
        if mean_target_share(index.target_weeks(pl), index.snap_weeks(pl)) > 0:
            out.append(pl)
    return out


def _catchers(
    team: str,
    group: list[Player],
    index: _HistoryIndex,
) -> list[Player]:
    """Active catchers. O, D, IR, and NA do not take a target share."""
    return [pl for pl in _target_candidates(team, group, index) if not is_inactive(pl)]


def _fold_inactive_targets(
    active: list[float],
    inactive: list[float],
) -> tuple[list[float], float]:
    """Move inactive target share onto active catchers.

    ``other`` stays the residual of the original shares (rostered plus
    inactive), after the same >1 normalization. Inactive mass is not dumped
    into that unrostered bucket. No active catchers: the mass falls into
    ``other``.
    """
    all_sum = sum(active) + sum(inactive)
    if all_sum > 1.0:
        scale = 1.0 / all_sum
        active = [value * scale for value in active]
        inactive_mass = sum(inactive) * scale
        other = 0.0
    else:
        inactive_mass = sum(inactive)
        other = 1.0 - all_sum
    active_sum = sum(active)
    if inactive_mass > 0.0 and active_sum > 0.0:
        active = [
            value + inactive_mass * (value / active_sum) for value in active
        ]
    elif inactive_mass > 0.0:
        other += inactive_mass
    return active, other


def _snap_pct_by_week(snaps: list[SnapWeek] | None) -> dict[int, float]:
    out: dict[int, float] = {}
    if not snaps:
        return out
    for snap in snaps:
        if snap.offense_pct is None:
            continue
        val = float(snap.offense_pct)
        prev = out.get(snap.week)
        if prev is None or val > prev:
            out[snap.week] = val
    return out


def _week_played(week: TargetWeek, snap_pct: float | None) -> bool:
    has_volume = week.targets > 0 or (
        week.target_share is not None and week.target_share > 0
    )
    if has_volume:
        return True
    if snap_pct is None:
        return False
    return snap_pct > 0


def _share_value(week: TargetWeek) -> float:
    if week.target_share is not None:
        return float(week.target_share)
    if week.team_targets and week.team_targets > 0:
        return float(week.targets) / float(week.team_targets)
    return 0.0


def _played_shares(
    weeks: list[TargetWeek],
    snaps: list[SnapWeek] | None = None,
) -> list[float]:
    by_week = _snap_pct_by_week(snaps)
    shares: list[float] = []
    for week in weeks:
        if not _week_played(week, by_week.get(week.week)):
            continue
        shares.append(_share_value(week))
    return shares


def _weekly_shares(
    weeks: list[TargetWeek],
    snaps: list[SnapWeek] | None = None,
) -> list[float]:
    return _played_shares(weeks, snaps)


def _realize_receiving(
    eff: EfficiencyModel,
    rng: random.Random,
    position: str,
    targets: float,
    player: Player | None = None,
) -> ReceivingLine:
    fn = getattr(eff, "receiving_line", None)
    if fn is None:
        return expected_receiving_line(position, targets)
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "player" in params:
        return fn(rng, position, targets, player=player)
    return fn(rng, position, targets)


def _scale_receiving(
    lines: list[ReceivingLine],
    yards: float,
    tds: float,
) -> tuple[ReceivingLine, ...]:
    """Rescale realized lines so they sum to the team pass anchors."""
    if not lines:
        return ()
    raw_yd = sum(line.rec_yd for line in lines)
    raw_td = sum(line.rec_td for line in lines)
    raw_tgt = sum(max(0.0, line.targets) for line in lines)
    n = len(lines)
    scaled: list[ReceivingLine] = []
    for line in lines:
        if raw_yd > 1e-6:
            rec_yd = line.rec_yd / raw_yd * yards
        elif raw_tgt > 1e-6:
            rec_yd = line.targets / raw_tgt * yards
        else:
            rec_yd = yards / n
        if raw_td > 1e-6:
            rec_td = line.rec_td / raw_td * tds
        elif raw_tgt > 1e-6:
            rec_td = line.targets / raw_tgt * tds
        else:
            rec_td = tds / n
        scaled.append(
            ReceivingLine(
                targets=line.targets,
                receptions=line.receptions,
                rec_yd=rec_yd,
                rec_td=rec_td,
            )
        )
    return tuple(scaled)


def _rb_rush_attempts(
    rbs: list[Player],
    shares: dict[str, float],
    pool: float,
) -> dict[str, float]:
    """Split team RB rushes. A rush-yard prop takes ``prop / YPC`` first.

    Props that sum past the pool are scaled down so the team total holds.
    Everyone else splits what remains by rush share.
    """
    ypc = YARDS_PER_RUSH.get("RB", 4.4) or 4.4
    claimed: dict[str, float] = {}
    free: list[str] = []
    for pl in rbs:
        if pl.prop_rush_yds is not None:
            claimed[pl.pid] = max(0.0, float(pl.prop_rush_yds) / ypc)
        else:
            free.append(pl.pid)
    claim = sum(claimed.values())
    if claim >= pool and claim > 0:
        scale = pool / claim if pool > 0 else 0.0
        return {pid: val * scale for pid, val in claimed.items()}
    left = max(0.0, pool - claim)
    weight = sum(shares.get(pid, 0.0) for pid in free)
    out = dict(claimed)
    for pid in free:
        if weight > 0:
            out[pid] = left * shares.get(pid, 0.0) / weight
        elif free:
            out[pid] = left / len(free)
        else:
            out[pid] = 0.0
    return out


def _tilt_anchors(
    eff: EfficiencyModel,
    team: str,
    opponent: str,
    yard_anchor: float,
    rush_attempts: float,
    td_anchor: float,
) -> tuple[float, float, float]:
    """Pass-vs-rush tilt and a red-zone TD nudge. Placeholder has neither.

    A pass-anchor scale above 1 raises passing yards. Rush attempts stay
    on the implied script. Data mode puts the rush complement on the
    rush-yard budget (``allocate_rush``), so volume and efficiency do not
    both scale.
    """
    scale_fn = getattr(eff, "pass_anchor_scale", None)
    if scale_fn is not None:
        tilt = float(scale_fn(team, opponent or None))
        if tilt != 1.0:
            yard_anchor *= tilt
    td_fn = getattr(eff, "td_anchor_scale", None)
    if td_fn is not None:
        td_scale = float(td_fn(team, opponent or None))
        if td_scale != 1.0:
            td_anchor *= td_scale
    return yard_anchor, rush_attempts, td_anchor


def _team_implied(players: list[Player]) -> float:
    for pl in players:
        if pl.implied_total is not None and float(pl.implied_total) > 0:
            return float(pl.implied_total)
    return 0.0


def _draw_team_opportunities(
    rng: random.Random,
    team_players: list[Player],
    margin: float,
    index: _HistoryIndex,
    eff: EfficiencyModel,
) -> dict[str, OpportunityCount]:
    """Plays, script, anchors, joint shares. Empty if no target history.

    RNG order (stable): one plays gaussian, then target-share gammas in
    pid order (plus an "other" bucket), then rush-share gammas in pid
    order when some RB has snaps or carries, then receiving-line yards
    (catchers, then the other bucket), then one team pass-yard gaussian
    around the implied-total anchor (skipped when that anchor is 0).
    Only ``passing_qb`` gets pass attempts and QB rushes; other QBs are
    explicit zeros. Every roster RB gets a rush count so a bellcow does
    not absorb the backup's carries.
    """
    if not team_players:
        return {}
    team = (team_players[0].team or "").upper()
    catchers = _catchers(team, team_players, index)
    if not catchers:
        return {}
    offense = index.offense(team)
    opponent = ""
    for pl in team_players:
        if (pl.opponent or "").strip():
            opponent = (pl.opponent or "").upper()
            break
    defense = index.defense(opponent) if opponent else None
    plays = _gauss_floor(
        rng, offense_plays_mu(offense, defense), PLAYS_SIGMA, PLAYS_FLOOR
    )
    rush_rate = scripted_rush_rate(offense, margin)
    pass_rate = 1.0 - rush_rate
    implied = _team_implied(team_players)
    starter = passing_qb(team_players)
    yard_prop = starter.prop_pass_yds if starter is not None else None
    td_prop = starter.prop_pass_tds if starter is not None else None
    yard_anchor = pass_yard_anchor(implied, pass_rate, yard_prop)
    td_anchor = pass_td_anchor(implied, pass_rate, td_prop)
    pass_attempts = plays * pass_rate
    rush_attempts = team_rush_attempts(plays, rush_rate, implied)
    yard_anchor, rush_attempts, td_anchor = _tilt_anchors(
        eff, team, opponent, yard_anchor, rush_attempts, td_anchor
    )
    team_targets = pass_attempts * index.targets_per_attempt(team)

    means = [
        mean_target_share(index.target_weeks(pl), index.snap_weeks(pl))
        for pl in catchers
    ]
    inactive = [
        pl
        for pl in _target_candidates(team, team_players, index)
        if is_inactive(pl)
    ]
    inactive_means = [
        mean_target_share(index.target_weeks(pl), index.snap_weeks(pl))
        for pl in inactive
    ]
    series = [
        _weekly_shares(index.target_weeks(pl), index.snap_weeks(pl))
        for pl in catchers
    ]
    kappa = share_kappa(series)
    means, other = _fold_inactive_targets(means, inactive_means)
    simplex_means = list(means)
    if other > 1e-6:
        simplex_means.append(other)
    drawn = draw_simplex(rng, simplex_means, kappa)
    target_share = {pl.pid: drawn[i] for i, pl in enumerate(catchers)}
    other_share = drawn[-1] if other > 1e-6 else 0.0

    rbs = [
        pl
        for pl in team_players
        if (pl.position or "").upper() == "RB" and not is_inactive(pl)
    ]
    snap_means = {pl.pid: index.snap_mean(pl) for pl in rbs}
    carry_means = {pl.pid: index.mean_carries(pl) for pl in rbs}
    rush_means, any_rush_signal = blended_rush_shares(rbs, snap_means, carry_means)
    if any_rush_signal and rbs:
        ordered = [pl.pid for pl in rbs]
        rush_drawn = draw_simplex(
            rng,
            [rush_means.get(pid, 0.0) for pid in ordered],
            kappa,
        )
        rush_share = {pid: rush_drawn[i] for i, pid in enumerate(ordered)}
    else:
        rush_share = rush_means

    # Passing floor is yard_anchor / td_anchor (implied total × pass rate,
    # or the passing prop). Rush floor is the starter's own history or
    # the league share, and at least the yard floor.
    qb_rushes = starter_qb_rush_attempts(starter, rush_attempts, index)
    if rush_attempts > 0:
        qb_rushes = min(qb_rushes, rush_attempts)
    rb_pool = max(0.0, rush_attempts - qb_rushes)
    rb_rushes = _rb_rush_attempts(rbs, rush_share, rb_pool)
    rush_alloc = _rush_allocation(
        eff, team, opponent, starter, qb_rushes, rbs, rb_rushes
    )
    out: dict[str, OpportunityCount] = {}
    lines: list[ReceivingLine] = []
    raw_by_pid: dict[str, tuple[float, float, ReceivingLine]] = {}
    for pl in catchers:
        targets = team_targets * target_share[pl.pid]
        line = _realize_receiving(eff, rng, pl.position or "WR", targets, pl)
        lines.append(line)
        raw_by_pid[pl.pid] = (targets, rb_rushes.get(pl.pid, 0.0), line)
    other_targets = team_targets * other_share
    if other_targets > 0.0:
        lines.append(_realize_receiving(eff, rng, "WR", other_targets))
    if yard_anchor > 0:
        drawn_yards = sample_yards(rng, yard_anchor)
    else:
        drawn_yards = 0.0
    team_lines = _scale_receiving(lines, drawn_yards, td_anchor)
    cursor = 0
    for pl in catchers:
        targets, rushes, _raw = raw_by_pid[pl.pid]
        rush_yards, rush_tds = rush_alloc.get(pl.pid, (None, None))
        out[pl.pid] = OpportunityCount(
            targets=targets,
            rushes=rushes,
            receiving=team_lines[cursor],
            rush_yards=rush_yards,
            rush_tds=rush_tds,
        )
        cursor += 1
    for pl in rbs:
        if pl.pid in out:
            continue
        rush_yards, rush_tds = rush_alloc.get(pl.pid, (None, None))
        out[pl.pid] = OpportunityCount(
            rushes=rb_rushes.get(pl.pid, 0.0),
            rush_yards=rush_yards,
            rush_tds=rush_tds,
        )
    for pl in team_players:
        if (pl.position or "").upper() != "QB":
            continue
        if starter is not None and pl.pid == starter.pid:
            rush_yards, rush_tds = rush_alloc.get(pl.pid, (None, None))
            out[pl.pid] = OpportunityCount(
                pass_attempts=pass_attempts,
                rushes=qb_rushes,
                team_receiving=team_lines,
                rush_yards=rush_yards,
                rush_tds=rush_tds,
            )
        else:
            # In the dict so the backup is not scored off team points.
            out[pl.pid] = OpportunityCount()
    return out


def _rush_allocation(
    eff: EfficiencyModel,
    team: str,
    opponent: str,
    starter: Player | None,
    qb_rushes: float,
    rbs: list[Player],
    rb_rushes: dict[str, float],
) -> dict[str, tuple[float, float]]:
    """Data-mode rush yards and TDs. Placeholder has no allocator."""
    alloc = getattr(eff, "allocate_rush", None)
    if alloc is None:
        return {}
    specs: list[tuple[Player, float]] = []
    if starter is not None and qb_rushes > 0:
        specs.append((starter, qb_rushes))
    for pl in rbs:
        carries = rb_rushes.get(pl.pid, 0.0)
        if carries > 0:
            specs.append((pl, carries))
    if not specs:
        return {}
    tilt = 1.0
    scale_fn = getattr(eff, "pass_anchor_scale", None)
    if scale_fn is not None:
        tilt = float(scale_fn(team, opponent or None))
    return alloc(specs, team=team, opponent=opponent or None, pass_tilt=tilt)


class _HistoryIndex:
    """Join weekly rows onto the pool. Name key is match_key + team."""

    def __init__(self, inputs: SimInputs) -> None:
        self.inputs = inputs
        self._tgt_pid: dict[str, list[TargetWeek]] = {}
        self._tgt_name: dict[tuple[str, str], list[TargetWeek]] = {}
        self._snap_pid: dict[str, list[SnapWeek]] = {}
        self._snap_name: dict[tuple[str, str], list[SnapWeek]] = {}
        self._carry_pid: dict[str, list[CarryWeek]] = {}
        self._carry_name: dict[tuple[str, str], list[CarryWeek]] = {}
        for row in inputs.targets:
            if row.player_name:
                self._tgt_name.setdefault(
                    (row.team_fd, match_key(row.player_name)), []
                ).append(row)
            if row.player_id:
                self._tgt_pid.setdefault(row.player_id, []).append(row)
            if row.gsis_id:
                self._tgt_pid.setdefault(row.gsis_id, []).append(row)
        for row in inputs.snaps:
            if row.player_name:
                self._snap_name.setdefault(
                    (row.team_fd, match_key(row.player_name)), []
                ).append(row)
            if row.player_id:
                self._snap_pid.setdefault(row.player_id, []).append(row)
            if row.gsis_id:
                self._snap_pid.setdefault(row.gsis_id, []).append(row)
        for row in inputs.carries:
            if row.player_name:
                self._carry_name.setdefault(
                    (row.team_fd, match_key(row.player_name)), []
                ).append(row)
            if row.player_id:
                self._carry_pid.setdefault(row.player_id, []).append(row)
            if row.gsis_id:
                self._carry_pid.setdefault(row.gsis_id, []).append(row)

    def target_weeks(self, player: Player) -> list[TargetWeek]:
        hit = self._tgt_pid.get(player.pid)
        if hit:
            return hit
        return list(
            self._tgt_name.get(
                ((player.team or "").upper(), match_key(player.name)),
                [],
            )
        )

    def snap_weeks(self, player: Player) -> list[SnapWeek]:
        hit = self._snap_pid.get(player.pid)
        if hit:
            return hit
        return list(
            self._snap_name.get(
                ((player.team or "").upper(), match_key(player.name)),
                [],
            )
        )

    def carry_weeks(self, player: Player) -> list[CarryWeek]:
        hit = self._carry_pid.get(player.pid)
        if hit:
            return hit
        return list(
            self._carry_name.get(
                ((player.team or "").upper(), match_key(player.name)),
                [],
            )
        )

    def mean_carries(self, player: Player) -> float | None:
        weeks = self.carry_weeks(player)
        if not weeks:
            return None
        return sum(float(w.carries) for w in weeks) / len(weeks)

    def snap_mean(self, player: Player) -> float | None:
        vals = [
            float(w.offense_pct)
            for w in self.snap_weeks(player)
            if w.offense_pct is not None and w.offense_pct > 0
        ]
        if not vals:
            return None
        return sum(vals) / len(vals)

    def offense(self, team: str) -> TeamStat | None:
        rows = [
            r
            for r in self.inputs.team_stats
            if r.team_fd == team.upper() and r.is_offense
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: (-(r.n or 0), r.side))
        return rows[0]

    def defense(self, team: str) -> TeamStat | None:
        rows = [
            r
            for r in self.inputs.team_stats
            if r.team_fd == team.upper() and r.is_defense
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: (-(r.n or 0), r.side))
        return rows[0]

    def scoring_var(self, team: str, opponent: str | None) -> float | None:
        """Mean of this team's offensive EPA var and the opponent's defensive var."""
        parts: list[float] = []
        off = self.offense(team)
        if off is not None and off.epa_variance() is not None:
            parts.append(float(off.epa_variance()))
        if opponent:
            de = self.defense(opponent)
            if de is not None and de.epa_variance() is not None:
                parts.append(float(de.epa_variance()))
        if not parts:
            return None
        return sum(parts) / len(parts)

    def targets_per_attempt(self, team: str) -> float:
        by_week: dict[int, float] = {}
        for row in self.inputs.targets:
            if row.team_fd != team.upper():
                continue
            if not row.team_targets or not row.team_pass_attempts:
                continue
            if row.team_pass_attempts <= 0:
                continue
            by_week[row.week] = float(row.team_targets) / float(row.team_pass_attempts)
        if not by_week:
            return DEFAULT_TARGETS_PER_ATTEMPT
        return sum(by_week.values()) / len(by_week)


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
