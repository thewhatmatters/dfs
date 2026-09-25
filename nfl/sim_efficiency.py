"""Placeholder efficiency: expected FanDuel points given opportunities.

Layer 4 replaces ``PlaceholderEfficiency``. That replacement should draw
residual yards and lumpy TD counts, and may shift medians onto prop
lines (``player.prop_pass_yds`` and the other ``prop_*`` fields). This
placeholder is the conditional expectation and does not consume ``rng``.

``receiving_line`` realizes a target allocation once, including a yards
draw. ``points`` scores that realized game with FanDuel rates. A 300/100
bonus is +3 only when that draw's yards clear the line. The QB's passing
yards are the sum of the same receiving lines.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

from nfl.players import Player
from nfl.rules import skill_fd_points

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
