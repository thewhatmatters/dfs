"""Placeholder efficiency: expected FanDuel points given opportunities.

Layer 4 replaces ``PlaceholderEfficiency``. That replacement should draw
residual yards and lumpy TD counts, and may shift medians onto prop
lines (``player.prop_pass_yds`` and the other ``prop_*`` fields). This
placeholder is the conditional expectation and does not consume ``rng``.

``receiving_line`` realizes a target allocation once. ``points`` scores
a catcher from that line, and scores the QB by summing the same lines
(rostered catchers plus the other bucket). Callers must pass ``rng``
even though this class ignores it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

from nfl.players import Player
from nfl.rules import FANDUEL_NFL

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


def _bonuses(pass_yd: float, rush_yd: float, rec_yd: float) -> float:
    sc = FANDUEL_NFL.scoring
    pts = 0.0
    if pass_yd >= 300.0:
        pts += sc["bonus_pass_yd_300"]
    if rush_yd >= 100.0:
        pts += sc["bonus_rush_yd_100"]
    if rec_yd >= 100.0:
        pts += sc["bonus_rec_yd_100"]
    return pts


class PlaceholderEfficiency:
    """E[FD points | opportunities]. Layer 4 owns noise and prop calibration."""

    def receiving_line(
        self,
        rng: random.Random,
        position: str,
        targets: float,
    ) -> ReceivingLine:
        del rng  # layer 4 draws residual yards and lumpy TDs here
        return expected_receiving_line(position, targets)

    def points(
        self,
        rng: random.Random,
        player: Player,
        opportunities: OpportunityCount,
    ) -> float:
        del rng  # reserved for layer-4 residuals and TD draws
        pos = (player.position or "WR").upper()
        sc = FANDUEL_NFL.scoring
        if pos == "QB":
            att = max(0.0, opportunities.pass_attempts)
            rushes = max(0.0, opportunities.rushes)
            if opportunities.team_receiving is None:
                pass_yd = att * PASS_YPA
                pass_td = att * PASS_TD_RATE
            else:
                # Same objects that scored the catchers, plus the other bucket.
                pass_yd = sum(line.rec_yd for line in opportunities.team_receiving)
                pass_td = sum(line.rec_td for line in opportunities.team_receiving)
            rush_yd = rushes * QB_YPC
            pts = (
                pass_yd * sc["pass_yd"]
                + pass_td * sc["pass_td"]
                + att * INT_RATE * sc["int"]
                + rush_yd * sc["rush_yd"]
                + rushes * QB_RUSH_TD_RATE * sc["rush_td"]
            )
            return max(0.0, pts + _bonuses(pass_yd, rush_yd, 0.0))
        if opportunities.receiving is None:
            line = expected_receiving_line(pos, opportunities.targets)
        else:
            line = opportunities.receiving
        rushes = max(0.0, opportunities.rushes)
        ypc = YARDS_PER_RUSH.get(pos, 4.4)
        rush_yd = rushes * ypc
        pts = (
            line.rec_yd * sc["rec_yd"]
            + line.receptions * sc["rec"]
            + line.rec_td * sc["rec_td"]
            + rush_yd * sc["rush_yd"]
            + rushes * RUSH_TD_RATE * sc["rush_td"]
        )
        return max(0.0, pts + _bonuses(0.0, rush_yd, line.rec_yd))
