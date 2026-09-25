"""Efficiency: expected FanDuel points given opportunities.

``PlaceholderEfficiency`` uses league-average rates. ``DataEfficiency``
replaces those rates with shrunk player history (layer 4). It is not a
prop-line calibration. Passing-yard and passing-TD props are already the
team anchor in ``nfl.sim`` (the lines are scaled to them). A rush-yard
prop is that RB's attempt count (``prop / yards per carry``).

``receiving_line`` realizes a target allocation once, including a yards
draw. The sim then rescales those lines so the team sums to the pass
anchor. ``points`` scores that realized game with FanDuel rates. A 300/100
bonus is +3 only when that draw's yards clear the line. The QB's passing
yards are the sum of the same receiving lines.

Data-mode constants (prior opportunity counts):

- player targets 25, carries 20, pass attempts 40
- team-position targets 60, carries 40, pass attempts 80
- league targets 200, carries 150, pass attempts 250
  (empirical league rate, shrunk toward the placeholder constants)
- opponent shrinkage 100 plays; multiplier clamped to ±15%
- 1.0 EPA/play above the league is +0.50 on that multiplier, before the clamp
- red-zone TD multiplier clamped to ±15%
- pass-vs-rush yardage tilt clamped to ±8% (Vegas stays the scoring center)

A player with no history keeps the position prior. Weeks at or after
``before_week`` are dropped. Optional columns (air yards, red-zone
targets, goal-line carries, yards allowed, sack rate) change a rate only
when the column is present.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

from nfl.names import match_key
from nfl.players import Player
from nfl.rules import skill_fd_points
from nfl.sim_inputs import PlayerWeek, SimInputs, TeamStat

# League-average rates. Not fit to a slate and not tilted to props.
PASS_YPA = 7.1
PASS_TD_RATE = 0.045
INT_RATE = 0.022
QB_YPC = 4.8
QB_RUSH_TD_RATE = 0.035
CATCH_RATE = {"WR": 0.64, "TE": 0.68, "RB": 0.74}
YARDS_PER_REC = {"WR": 12.0, "TE": 10.5, "RB": 8.0}
REC_TD_PER_TARGET = {"WR": 0.048, "TE": 0.042, "RB": 0.028}
YARDS_PER_RUSH = {"RB": 4.4, "QB": QB_YPC, "WR": 5.5, "TE": 4.0}
RUSH_TD_RATE = 0.025
# Dispersion around the conditional-expectation yards so a 300/100 bonus
# is a per-game threshold, not a cliff on the mean. Not fit to a slate.
YARD_SIGMA_FRAC = 0.22
YARD_SIGMA_FLOOR = 12.0

# Empirical-Bayes prior counts. ``(n * obs + prior_n * prior) / (n + prior_n)``.
PRIOR_TARGETS = 25.0
PRIOR_CARRIES = 20.0
PRIOR_PASS_ATTEMPTS = 40.0
TEAM_PRIOR_TARGETS = 60.0
TEAM_PRIOR_CARRIES = 40.0
TEAM_PRIOR_PASS_ATTEMPTS = 80.0
LEAGUE_PRIOR_TARGETS = 200.0
LEAGUE_PRIOR_CARRIES = 150.0
LEAGUE_PRIOR_PASS_ATTEMPTS = 250.0
# Opponent defense. Weight ``n / (n + OPP_PRIOR_PLAYS)`` toward the league.
OPP_PRIOR_PLAYS = 100.0
OPP_CLAMP = 0.15
EPA_TO_MULT = 0.50
LEAGUE_PASS_EPA = 0.08
LEAGUE_RUSH_EPA = -0.02
LEAGUE_PASS_SUCCESS = 0.48
LEAGUE_RUSH_SUCCESS = 0.42
LEAGUE_RZ_TD = 0.55
RZ_PRIOR_PLAYS = 25.0
RZ_TD_CLAMP = 0.15
# Pass yard anchor moves at most this far. Rush attempts take the complement.
YARD_TILT_MAX = 0.08
YARD_TILT_PER_EPA = 0.12
# Optional columns. Skipped when the field is None.
AIR_YARDS_BLEND = 0.10
LEAGUE_AIR_YARDS_PER_TARGET = 8.0
RZ_TARGET_TD_BLEND = 0.10
LEAGUE_RZ_TARGET_SHARE = 0.12
GL_CARRY_TD_BLEND = 0.10
LEAGUE_GL_CARRY_SHARE = 0.08
LEAGUE_SACK_RATE = 0.07
SACK_TO_MULT = 0.50


@dataclass(frozen=True)
class ReceivingLine:
    """One realization of a target allocation. Catcher and QB share this object."""

    targets: float
    receptions: float
    rec_yd: float
    rec_td: float


@dataclass(frozen=True)
class OpportunityCount:
    """Volume allocated to one player in one world, before efficiency.

    ``receiving`` is that player's line. ``team_receiving`` is every line
    the QB's pass yards and TDs are summed from (rostered catchers plus
    the unrostered "other" bucket). ``None`` means this count is not on
    the shared-draw path (role-share fallback, or a backup QB).
    """

    pass_attempts: float = 0.0
    targets: float = 0.0
    rushes: float = 0.0
    receiving: ReceivingLine | None = None
    team_receiving: tuple[ReceivingLine, ...] | None = None


class EfficiencyModel(Protocol):
    def receiving_line(
        self,
        rng: random.Random,
        position: str,
        targets: float,
    ) -> ReceivingLine:
        """Realize targets once. The QB sum reuses this object."""

    def points(
        self,
        rng: random.Random,
        player: Player,
        opportunities: OpportunityCount,
    ) -> float:
        """FanDuel points for one player in one world."""


def expected_receiving_line(position: str, targets: float) -> ReceivingLine:
    """Conditional-expectation receiving line. No RNG."""
    pos = (position or "WR").upper()
    caught = CATCH_RATE.get(pos, 0.64)
    ypr = YARDS_PER_REC.get(pos, 11.0)
    t = max(0.0, float(targets))
    recs = t * caught
    return ReceivingLine(
        targets=t,
        receptions=recs,
        rec_yd=recs * ypr,
        rec_td=t * REC_TD_PER_TARGET.get(pos, 0.04),
    )


def sample_yards(rng: random.Random, mean: float) -> float:
    """One game's yards. Mean <= 0 stays 0 and does not touch the RNG."""
    mu = max(0.0, float(mean))
    if mu <= 0.0:
        return 0.0
    sigma = max(YARD_SIGMA_FLOOR, YARD_SIGMA_FRAC * mu)
    return max(0.0, rng.gauss(mu, sigma))


class PlaceholderEfficiency:
    """FanDuel points for one realized opportunity count.

    Yards are sampled around the conditional mean so a 300/100 bonus fires
    in some games and not others. TD and reception counts stay at their
    expectation (layer 4 draws those). Fumbles, two-point conversions, and
    return TDs are in ``skill_fd_points`` and are not drawn here.
    """

    def receiving_line(
        self,
        rng: random.Random,
        position: str,
        targets: float,
    ) -> ReceivingLine:
        line = expected_receiving_line(position, targets)
        return ReceivingLine(
            targets=line.targets,
            receptions=line.receptions,
            rec_yd=sample_yards(rng, line.rec_yd),
            rec_td=line.rec_td,
        )

    def points(
        self,
        rng: random.Random,
        player: Player,
        opportunities: OpportunityCount,
    ) -> float:
        pos = (player.position or "WR").upper()
        if pos == "QB":
            att = max(0.0, opportunities.pass_attempts)
            rushes = max(0.0, opportunities.rushes)
            if opportunities.team_receiving is None:
                pass_yd = sample_yards(rng, att * PASS_YPA)
                pass_td = att * PASS_TD_RATE
            else:
                # Same objects that scored the catchers, plus the other bucket.
                pass_yd = sum(line.rec_yd for line in opportunities.team_receiving)
                pass_td = sum(line.rec_td for line in opportunities.team_receiving)
            rush_yd = sample_yards(rng, rushes * QB_YPC)
            return max(
                0.0,
                skill_fd_points(
                    pass_yd=pass_yd,
                    pass_td=pass_td,
                    interceptions=att * INT_RATE,
                    rush_yd=rush_yd,
                    rush_td=rushes * QB_RUSH_TD_RATE,
                ),
            )
        if opportunities.receiving is None:
            line = self.receiving_line(rng, pos, opportunities.targets)
        else:
            line = opportunities.receiving
        rushes = max(0.0, opportunities.rushes)
        ypc = YARDS_PER_RUSH.get(pos, 4.4)
        rush_yd = sample_yards(rng, rushes * ypc)
        return max(
            0.0,
            skill_fd_points(
                rush_yd=rush_yd,
                rush_td=rushes * RUSH_TD_RATE,
                rec_yd=line.rec_yd,
                receptions=line.receptions,
                rec_td=line.rec_td,
            ),
        )


def shrink(observed: float, n: float, prior: float, prior_n: float) -> float:
    """Prior-count blend. ``n == 0`` returns ``prior``."""
    count = max(0.0, float(n))
    weight = max(0.0, float(prior_n))
    if count + weight <= 0:
        return float(prior)
    return (count * float(observed) + weight * float(prior)) / (count + weight)


def clamp(value: float, lo: float, hi: float) -> float:
    return min(float(hi), max(float(lo), float(value)))


def position_rates(position: str) -> dict[str, float]:
    """Placeholder constants for one position. The no-history prior."""
    pos = (position or "WR").upper()
    catch = CATCH_RATE.get(pos, 0.64)
    ypr = YARDS_PER_REC.get(pos, 11.0)
    return {
        "catch_rate": catch,
        "yards_per_target": catch * ypr,
        "rec_td_per_target": REC_TD_PER_TARGET.get(pos, 0.04),
        "yards_per_carry": YARDS_PER_RUSH.get(pos, 4.4),
        "rush_td_per_carry": QB_RUSH_TD_RATE if pos == "QB" else RUSH_TD_RATE,
        "yards_per_attempt": PASS_YPA,
        "pass_td_per_attempt": PASS_TD_RATE,
    }


def _blend_level(
    observed: float,
    n: float,
    team_observed: float,
    team_n: float,
    league_observed: float,
    league_n: float,
    constant: float,
    *,
    player_prior: float,
    team_prior: float,
    league_prior: float,
) -> float:
    """Player → team-position → league-position → documented constant."""
    league = shrink(league_observed, league_n, constant, league_prior)
    team = shrink(team_observed, team_n, league, team_prior)
    if n <= 0:
        return constant
    return shrink(observed, n, team, player_prior)


@dataclass(frozen=True)
class _Sums:
    targets: float = 0.0
    receptions: float = 0.0
    receiving_yards: float = 0.0
    receiving_tds: float = 0.0
    carries: float = 0.0
    rushing_yards: float = 0.0
    rushing_tds: float = 0.0
    pass_attempts: float = 0.0
    passing_yards: float = 0.0
    passing_tds: float = 0.0
    air_yards: float | None = None
    red_zone_targets: float | None = None
    goal_line_carries: float | None = None

    def add(self, week: PlayerWeek) -> "_Sums":
        air = self.air_yards
        if week.receiving_air_yards is not None:
            air = (air or 0.0) + float(week.receiving_air_yards)
        rz = self.red_zone_targets
        if week.red_zone_targets is not None:
            rz = (rz or 0.0) + float(week.red_zone_targets)
        gl = self.goal_line_carries
        if week.goal_line_carries is not None:
            gl = (gl or 0.0) + float(week.goal_line_carries)
        return _Sums(
            targets=self.targets + week.targets,
            receptions=self.receptions + week.receptions,
            receiving_yards=self.receiving_yards + week.receiving_yards,
            receiving_tds=self.receiving_tds + week.receiving_tds,
            carries=self.carries + week.carries,
            rushing_yards=self.rushing_yards + week.rushing_yards,
            rushing_tds=self.rushing_tds + week.rushing_tds,
            pass_attempts=self.pass_attempts + week.pass_attempts,
            passing_yards=self.passing_yards + week.passing_yards,
            passing_tds=self.passing_tds + week.passing_tds,
            air_yards=air,
            red_zone_targets=rz,
            goal_line_carries=gl,
        )


def _rate(total: float, n: float) -> float:
    if n <= 0:
        return 0.0
    return total / n


def _optional_factor(
    observed: float | None,
    n: float,
    league: float,
    blend: float,
) -> float:
    """1.0 when the column is missing. Otherwise a clamped blend."""
    if observed is None or n <= 0 or league <= 0:
        return 1.0
    gap = clamp(observed / n / league - 1.0, -1.0, 1.0)
    return 1.0 + blend * gap


@dataclass
class _Side:
    pass_epa: float | None = None
    rush_epa: float | None = None
    pass_success: float | None = None
    rush_success: float | None = None
    red_zone_td_rate: float | None = None
    n: float = 0.0
    pass_n: float = 0.0
    rush_n: float = 0.0
    rz_n: float = 0.0
    yards_per_carry_allowed: float | None = None
    yards_per_attempt_allowed: float | None = None
    sack_rate: float | None = None


def _weighted(rows: list[TeamStat]) -> _Side:
    """Play-weighted means. Early-down EPA wins over overall when present."""

    def _acc(pick, weight):
        num = 0.0
        den = 0.0
        for row in rows:
            val = pick(row)
            if val is None:
                continue
            w = weight(row)
            if w <= 0:
                w = 1.0
            num += float(val) * w
            den += w
        if den <= 0:
            return None, 0.0
        return num / den, den

    def _pass_epa(row: TeamStat) -> float | None:
        if row.early_down_pass_epa_per_play is not None:
            return row.early_down_pass_epa_per_play
        return row.pass_epa_per_play

    def _pass_n(row: TeamStat) -> float:
        if row.early_down_pass_epa_per_play is not None and row.early_down_pass_n:
            return float(row.early_down_pass_n)
        return float(row.pass_n or row.n or 0)

    def _rush_epa(row: TeamStat) -> float | None:
        if row.early_down_rush_epa_per_play is not None:
            return row.early_down_rush_epa_per_play
        return row.rush_epa_per_play

    def _rush_n(row: TeamStat) -> float:
        if row.early_down_rush_epa_per_play is not None and row.early_down_rush_n:
            return float(row.early_down_rush_n)
        return float(row.rush_n or row.n or 0)

    pass_epa, pass_n = _acc(_pass_epa, _pass_n)
    rush_epa, rush_n = _acc(_rush_epa, _rush_n)
    pass_success, _ = _acc(
        lambda row: row.early_down_pass_success_rate
        if row.early_down_pass_success_rate is not None
        else row.pass_success_rate,
        _pass_n,
    )
    rush_success, _ = _acc(
        lambda row: row.early_down_rush_success_rate
        if row.early_down_rush_success_rate is not None
        else row.rush_success_rate,
        _rush_n,
    )
    rz, rz_n = _acc(lambda row: row.red_zone_td_rate, lambda row: float(row.n or 0))
    ypc, _ = _acc(lambda row: row.yards_per_carry_allowed, _rush_n)
    ypa, _ = _acc(
        lambda row: row.yards_per_attempt_allowed or row.yards_per_dropback_allowed,
        _pass_n,
    )
    sack, _ = _acc(lambda row: row.sack_rate, _pass_n)
    return _Side(
        pass_epa=pass_epa,
        rush_epa=rush_epa,
        pass_success=pass_success,
        rush_success=rush_success,
        red_zone_td_rate=rz,
        n=max(pass_n, rush_n),
        pass_n=pass_n,
        rush_n=rush_n,
        rz_n=rz_n,
        yards_per_carry_allowed=ypc,
        yards_per_attempt_allowed=ypa,
        sack_rate=sack,
    )


def _epa_multiplier(
    allowed: float | None,
    league: float,
    n: float,
    success: float | None,
    league_success: float,
) -> float:
    """1.0 at the league. Clamped to ±``OPP_CLAMP``."""
    parts: list[float] = []
    if allowed is not None:
        shrunk = shrink(allowed, n, league, OPP_PRIOR_PLAYS)
        parts.append(1.0 + EPA_TO_MULT * (shrunk - league))
    if success is not None and league_success > 0:
        shrunk_s = shrink(success, n, league_success, OPP_PRIOR_PLAYS)
        parts.append(shrunk_s / league_success)
    if not parts:
        return 1.0
    return clamp(sum(parts) / len(parts), 1.0 - OPP_CLAMP, 1.0 + OPP_CLAMP)


class DataEfficiency:
    """Layer 4. Shrunk player rates, a clamped opponent, Vegas still the center.

    ``before_week`` drops that week and every later week. ``None`` keeps
    every row in the bundle (the optimizer's completed-week feed).
    """

    def __init__(
        self,
        inputs: SimInputs | None = None,
        *,
        before_week: int | None = None,
    ) -> None:
        self.before_week = before_week
        bundle = inputs or SimInputs()
        weeks = [
            row
            for row in bundle.player_weeks
            if _week_allowed(row.week, before_week)
        ]
        self._by_id: dict[str, _Sums] = {}
        self._by_name: dict[tuple[str, str], _Sums] = {}
        self._team_pos: dict[tuple[str, str], _Sums] = {}
        self._league_pos: dict[str, _Sums] = {}
        for row in weeks:
            self._add_player(row)
        self._sides = _index_sides(bundle, before_week)

    def rates_for(self, player: Player | None, position: str) -> dict[str, float]:
        """Shrunk rates. No history returns the position prior, unadjusted."""
        pos = (position or "WR").upper()
        base = position_rates(pos)
        player_sums = self._lookup(player) if player is not None else None
        if player_sums is None or (
            player_sums.targets <= 0
            and player_sums.carries <= 0
            and player_sums.pass_attempts <= 0
        ):
            return base
        team = (player.team or "").upper() if player is not None else ""
        team_sums = self._team_pos.get((team, pos), _Sums())
        league_sums = self._league_pos.get(pos, _Sums())
        out = dict(base)
        out["catch_rate"] = _blend_level(
            _rate(player_sums.receptions, player_sums.targets),
            player_sums.targets,
            _rate(team_sums.receptions, team_sums.targets),
            team_sums.targets,
            _rate(league_sums.receptions, league_sums.targets),
            league_sums.targets,
            base["catch_rate"],
            player_prior=PRIOR_TARGETS,
            team_prior=TEAM_PRIOR_TARGETS,
            league_prior=LEAGUE_PRIOR_TARGETS,
        )
        out["yards_per_target"] = _blend_level(
            _rate(player_sums.receiving_yards, player_sums.targets),
            player_sums.targets,
            _rate(team_sums.receiving_yards, team_sums.targets),
            team_sums.targets,
            _rate(league_sums.receiving_yards, league_sums.targets),
            league_sums.targets,
            base["yards_per_target"],
            player_prior=PRIOR_TARGETS,
            team_prior=TEAM_PRIOR_TARGETS,
            league_prior=LEAGUE_PRIOR_TARGETS,
        )
        out["yards_per_target"] *= _optional_factor(
            player_sums.air_yards,
            player_sums.targets,
            LEAGUE_AIR_YARDS_PER_TARGET,
            AIR_YARDS_BLEND,
        )
        out["rec_td_per_target"] = _blend_level(
            _rate(player_sums.receiving_tds, player_sums.targets),
            player_sums.targets,
            _rate(team_sums.receiving_tds, team_sums.targets),
            team_sums.targets,
            _rate(league_sums.receiving_tds, league_sums.targets),
            league_sums.targets,
            base["rec_td_per_target"],
            player_prior=PRIOR_TARGETS,
            team_prior=TEAM_PRIOR_TARGETS,
            league_prior=LEAGUE_PRIOR_TARGETS,
        )
        out["rec_td_per_target"] *= _optional_factor(
            player_sums.red_zone_targets,
            player_sums.targets,
            LEAGUE_RZ_TARGET_SHARE,
            RZ_TARGET_TD_BLEND,
        )
        out["yards_per_carry"] = _blend_level(
            _rate(player_sums.rushing_yards, player_sums.carries),
            player_sums.carries,
            _rate(team_sums.rushing_yards, team_sums.carries),
            team_sums.carries,
            _rate(league_sums.rushing_yards, league_sums.carries),
            league_sums.carries,
            base["yards_per_carry"],
            player_prior=PRIOR_CARRIES,
            team_prior=TEAM_PRIOR_CARRIES,
            league_prior=LEAGUE_PRIOR_CARRIES,
        )
        out["rush_td_per_carry"] = _blend_level(
            _rate(player_sums.rushing_tds, player_sums.carries),
            player_sums.carries,
            _rate(team_sums.rushing_tds, team_sums.carries),
            team_sums.carries,
            _rate(league_sums.rushing_tds, league_sums.carries),
            league_sums.carries,
            base["rush_td_per_carry"],
            player_prior=PRIOR_CARRIES,
            team_prior=TEAM_PRIOR_CARRIES,
            league_prior=LEAGUE_PRIOR_CARRIES,
        )
        out["rush_td_per_carry"] *= _optional_factor(
            player_sums.goal_line_carries,
            player_sums.carries,
            LEAGUE_GL_CARRY_SHARE,
            GL_CARRY_TD_BLEND,
        )
        out["yards_per_attempt"] = _blend_level(
            _rate(player_sums.passing_yards, player_sums.pass_attempts),
            player_sums.pass_attempts,
            _rate(team_sums.passing_yards, team_sums.pass_attempts),
            team_sums.pass_attempts,
            _rate(league_sums.passing_yards, league_sums.pass_attempts),
            league_sums.pass_attempts,
            base["yards_per_attempt"],
            player_prior=PRIOR_PASS_ATTEMPTS,
            team_prior=TEAM_PRIOR_PASS_ATTEMPTS,
            league_prior=LEAGUE_PRIOR_PASS_ATTEMPTS,
        )
        out["pass_td_per_attempt"] = _blend_level(
            _rate(player_sums.passing_tds, player_sums.pass_attempts),
            player_sums.pass_attempts,
            _rate(team_sums.passing_tds, team_sums.pass_attempts),
            team_sums.pass_attempts,
            _rate(league_sums.passing_tds, league_sums.pass_attempts),
            league_sums.pass_attempts,
            base["pass_td_per_attempt"],
            player_prior=PRIOR_PASS_ATTEMPTS,
            team_prior=TEAM_PRIOR_PASS_ATTEMPTS,
            league_prior=LEAGUE_PRIOR_PASS_ATTEMPTS,
        )
        return out

    def pass_multiplier(self, opponent: str | None) -> float:
        side = self._sides.get(((opponent or "").upper(), "defense"))
        if side is None:
            return 1.0
        mult = _epa_multiplier(
            side.pass_epa,
            LEAGUE_PASS_EPA,
            side.pass_n,
            side.pass_success,
            LEAGUE_PASS_SUCCESS,
        )
        if side.yards_per_attempt_allowed is not None and PASS_YPA > 0:
            ratio = side.yards_per_attempt_allowed / PASS_YPA
            mult = 0.5 * mult + 0.5 * ratio
        if side.sack_rate is not None:
            mult *= 1.0 - SACK_TO_MULT * (side.sack_rate - LEAGUE_SACK_RATE)
        return clamp(mult, 1.0 - OPP_CLAMP, 1.0 + OPP_CLAMP)

    def rush_multiplier(self, opponent: str | None) -> float:
        side = self._sides.get(((opponent or "").upper(), "defense"))
        if side is None:
            return 1.0
        mult = _epa_multiplier(
            side.rush_epa,
            LEAGUE_RUSH_EPA,
            side.rush_n,
            side.rush_success,
            LEAGUE_RUSH_SUCCESS,
        )
        if side.yards_per_carry_allowed is not None:
            league_ypc = YARDS_PER_RUSH.get("RB", 4.4)
            if league_ypc > 0:
                ratio = side.yards_per_carry_allowed / league_ypc
                mult = 0.5 * mult + 0.5 * ratio
        return clamp(mult, 1.0 - OPP_CLAMP, 1.0 + OPP_CLAMP)

    def td_multiplier(self, team: str | None, opponent: str | None) -> float:
        """Offense red-zone rate up, and a defense that allows more, both raise TDs."""
        off = self._sides.get(((team or "").upper(), "offense"))
        de = self._sides.get(((opponent or "").upper(), "defense"))
        if (off is None or off.red_zone_td_rate is None) and (
            de is None or de.red_zone_td_rate is None
        ):
            return 1.0
        off_rate = LEAGUE_RZ_TD
        off_n = 0.0
        if off is not None and off.red_zone_td_rate is not None:
            off_rate = off.red_zone_td_rate
            off_n = off.rz_n or off.n
        def_rate = LEAGUE_RZ_TD
        def_n = 0.0
        if de is not None and de.red_zone_td_rate is not None:
            def_rate = de.red_zone_td_rate
            def_n = de.rz_n or de.n
        off_s = shrink(off_rate, off_n, LEAGUE_RZ_TD, RZ_PRIOR_PLAYS)
        def_s = shrink(def_rate, def_n, LEAGUE_RZ_TD, RZ_PRIOR_PLAYS)
        gap = (off_s - LEAGUE_RZ_TD) + (def_s - LEAGUE_RZ_TD)
        mult = 1.0 + (gap / LEAGUE_RZ_TD if LEAGUE_RZ_TD else 0.0)
        return clamp(mult, 1.0 - RZ_TD_CLAMP, 1.0 + RZ_TD_CLAMP)

    def pass_anchor_scale(self, team: str, opponent: str | None) -> float:
        """Tilt pass yards inside the Vegas anchor. 1.0 when the rows are missing."""
        off = self._sides.get((team.upper(), "offense"))
        de = self._sides.get(((opponent or "").upper(), "defense"))
        if off is None and de is None:
            return 1.0
        off_pass = shrink(
            off.pass_epa if off and off.pass_epa is not None else LEAGUE_PASS_EPA,
            off.pass_n if off else 0.0,
            LEAGUE_PASS_EPA,
            OPP_PRIOR_PLAYS,
        )
        off_rush = shrink(
            off.rush_epa if off and off.rush_epa is not None else LEAGUE_RUSH_EPA,
            off.rush_n if off else 0.0,
            LEAGUE_RUSH_EPA,
            OPP_PRIOR_PLAYS,
        )
        def_pass = shrink(
            de.pass_epa if de and de.pass_epa is not None else LEAGUE_PASS_EPA,
            de.pass_n if de else 0.0,
            LEAGUE_PASS_EPA,
            OPP_PRIOR_PLAYS,
        )
        def_rush = shrink(
            de.rush_epa if de and de.rush_epa is not None else LEAGUE_RUSH_EPA,
            de.rush_n if de else 0.0,
            LEAGUE_RUSH_EPA,
            OPP_PRIOR_PLAYS,
        )
        off_gap = (off_pass - LEAGUE_PASS_EPA) - (off_rush - LEAGUE_RUSH_EPA)
        def_gap = (def_pass - LEAGUE_PASS_EPA) - (def_rush - LEAGUE_RUSH_EPA)
        raw = 1.0 + YARD_TILT_PER_EPA * (0.6 * off_gap + 0.4 * def_gap)
        return clamp(raw, 1.0 - YARD_TILT_MAX, 1.0 + YARD_TILT_MAX)

    def td_anchor_scale(self, team: str, opponent: str | None) -> float:
        return self.td_multiplier(team, opponent)

    def receiving_line(
        self,
        rng: random.Random,
        position: str,
        targets: float,
        player: Player | None = None,
    ) -> ReceivingLine:
        rates = self.rates_for(player, position)
        opponent = player.opponent if player is not None else None
        t = max(0.0, float(targets))
        catch = rates["catch_rate"]
        ypt = rates["yards_per_target"]
        td_rate = rates["rec_td_per_target"]
        pass_mult = self.pass_multiplier(opponent)
        prior = position_rates(position)
        # Same multiply order as the placeholder when nothing has moved,
        # so an empty history reproduces those draws bit for bit.
        if (
            pass_mult == 1.0
            and catch == prior["catch_rate"]
            and ypt == prior["yards_per_target"]
            and td_rate == prior["rec_td_per_target"]
        ):
            base = expected_receiving_line(position, t)
            return ReceivingLine(
                targets=base.targets,
                receptions=base.receptions,
                rec_yd=sample_yards(rng, base.rec_yd),
                rec_td=base.rec_td,
            )
        if pass_mult != 1.0:
            ypt *= pass_mult
        return ReceivingLine(
            targets=t,
            receptions=t * catch,
            rec_yd=sample_yards(rng, t * ypt),
            rec_td=t * td_rate,
        )

    def points(
        self,
        rng: random.Random,
        player: Player,
        opportunities: OpportunityCount,
    ) -> float:
        pos = (player.position or "WR").upper()
        rates = self.rates_for(player, pos)
        if pos == "QB":
            att = max(0.0, opportunities.pass_attempts)
            rushes = max(0.0, opportunities.rushes)
            if opportunities.team_receiving is None:
                ypa = rates["yards_per_attempt"]
                pass_mult = self.pass_multiplier(player.opponent)
                if pass_mult != 1.0:
                    ypa *= pass_mult
                pass_yd = sample_yards(rng, att * ypa)
                td_mult = self.td_multiplier(player.team, player.opponent)
                pass_td = att * rates["pass_td_per_attempt"]
                if td_mult != 1.0:
                    pass_td *= td_mult
            else:
                pass_yd = sum(line.rec_yd for line in opportunities.team_receiving)
                pass_td = sum(line.rec_td for line in opportunities.team_receiving)
            ypc = rates["yards_per_carry"]
            rush_mult = self.rush_multiplier(player.opponent)
            if rush_mult != 1.0:
                ypc *= rush_mult
            rush_yd = sample_yards(rng, rushes * ypc)
            rush_td = rushes * rates["rush_td_per_carry"]
            td_mult = self.td_multiplier(player.team, player.opponent)
            if td_mult != 1.0:
                rush_td *= td_mult
            return max(
                0.0,
                skill_fd_points(
                    pass_yd=pass_yd,
                    pass_td=pass_td,
                    interceptions=att * INT_RATE,
                    rush_yd=rush_yd,
                    rush_td=rush_td,
                ),
            )
        if opportunities.receiving is None:
            line = self.receiving_line(rng, pos, opportunities.targets, player)
        else:
            line = opportunities.receiving
        rushes = max(0.0, opportunities.rushes)
        ypc = rates["yards_per_carry"]
        rush_mult = self.rush_multiplier(player.opponent)
        if rush_mult != 1.0:
            ypc *= rush_mult
        rush_yd = sample_yards(rng, rushes * ypc)
        rush_td = rushes * rates["rush_td_per_carry"]
        td_mult = self.td_multiplier(player.team, player.opponent)
        if td_mult != 1.0:
            rush_td *= td_mult
        return max(
            0.0,
            skill_fd_points(
                rush_yd=rush_yd,
                rush_td=rush_td,
                rec_yd=line.rec_yd,
                receptions=line.receptions,
                rec_td=line.rec_td,
            ),
        )

    def _add_player(self, week: PlayerWeek) -> None:
        pos = (week.position or "WR").upper()
        team = week.team_fd.upper()
        self._team_pos[(team, pos)] = self._team_pos.get((team, pos), _Sums()).add(week)
        self._league_pos[pos] = self._league_pos.get(pos, _Sums()).add(week)
        name_key = (team, match_key(week.player_name))
        self._by_name[name_key] = self._by_name.get(name_key, _Sums()).add(week)
        for ident in (week.gsis_id, week.player_id):
            if ident:
                self._by_id[ident] = self._by_id.get(ident, _Sums()).add(week)

    def _lookup(self, player: Player) -> _Sums | None:
        if player.pid and player.pid in self._by_id:
            return self._by_id[player.pid]
        key = ((player.team or "").upper(), match_key(player.name))
        return self._by_name.get(key)


def _week_allowed(week: int | None, before_week: int | None) -> bool:
    if before_week is None:
        return True
    if week is None:
        return False
    return int(week) < int(before_week)


def _index_sides(
    inputs: SimInputs,
    before_week: int | None,
) -> dict[tuple[str, str], _Side]:
    """Weekly rows when a cutoff is set. Otherwise weekly, else the season board."""
    if before_week is not None:
        rows = [
            row
            for row in inputs.team_weeks
            if row.week is not None and int(row.week) < int(before_week)
        ]
    elif inputs.team_weeks:
        rows = list(inputs.team_weeks)
    else:
        rows = list(inputs.team_stats)
    grouped: dict[tuple[str, str], list[TeamStat]] = {}
    for row in rows:
        side = "defense" if row.is_defense else "offense"
        grouped.setdefault((row.team_fd.upper(), side), []).append(row)
    return {key: _weighted(group) for key, group in grouped.items()}


def build_efficiency(
    mode: str | None,
    inputs: SimInputs | None,
    *,
    before_week: int | None = None,
) -> EfficiencyModel:
    """``data`` (default) or ``placeholder``."""
    name = (mode or "data").strip().lower()
    if name == "placeholder":
        return PlaceholderEfficiency()
    if name != "data":
        raise ValueError(f"sim efficiency must be data or placeholder, got {mode!r}")
    return DataEfficiency(inputs, before_week=before_week)
