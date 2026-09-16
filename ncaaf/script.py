"""Run/pass distribution and closing-script multipliers for the ILP objective.

Closing script uses the team's spread (negative = favorite) we already ingest.
That term applies even when 2026 CFBD advanced rates are missing — it is the
closing number, not a live score and not last season's mix.

When `pass_rate` exists: mix × spread, then second-half sit. Favorites lean
run late; dogs throw. Opponent rush vs pass **PPA allowed** splits that
tilt (worse vs pass → WR/QB up, RB down) without restating overall D
(already in implied total). Props tilt the implied base ±20% afterward.
"""

from __future__ import annotations

FBS_PASS = 0.55  # typical FBS pass-play share
PASS_POS = frozenset({"QB", "WR", "TE"})
SPREAD_COEF = 0.012
# Mix × spread only. Sit may push WR/TE below this; see OUT_*.
CLAMP_LO = 0.55
CLAMP_HI = 1.45
OUT_LO = 0.35
OUT_HI = 1.65

# |spread| at which notes mention favorite/dog (two scores). Still tilts.
BLOWOUT = 14.0
# Huge favorite: second-half sit. −14 stays mention-only. Dogs never sit.
# Playbook: ncaaf/docs/data/game-script.md — tweak these after a finished slate.
SIT_RUN = 21.0
# Sit factor reaches its floor here so −40 still moves vs −28.
SIT_FULL = 40.0

# (at SIT_RUN, at SIT_FULL). WR/TE starters recede most; QB modest.
# Blowout RBs: featured back sits some; d2/d3 get clock/garbage (slate 133865).
SIT_WR1 = (0.85, 0.62)
SIT_WR = (0.92, 0.78)
SIT_QB = (0.94, 0.85)
SIT_RB1 = (0.92, 0.75)
BOOST_RB23 = (1.12, 1.28)

# Opponent D quality split. Higher PPA allowed = worse D on that play type.
# Use pass−rush gap only (level is in the spread). Cap the gap: n=1 boxes.
DEF_PPA_COEF = 0.40
DEF_PPA_GAP_CAP = 0.35


def script_mult(
    position: str,
    *,
    pass_rate: float | None,
    opp_pass_rate: float | None = None,
    team_spread: float | None = None,
    depth_rank: int | None = None,
    opp_pass_ppa: float | None = None,
    opp_rush_ppa: float | None = None,
) -> float:
    """>1 boosts pass catchers/QB; <1 boosts RBs.

    Spread-only when `pass_rate` is None (week 1). Mix × spread when rates
    exist. Opponent pass−rush PPA gap splits rush vs pass without
    restating overall D. Second-half sit (spread ≤ −SIT_RUN) haircuts
    WR/TE/QB and RB1 on favorites; RB d2/d3 get a clock/garbage boost.
    Grind favorites (−3 to −14) only get the spread 2−tilt (RB1 clock-chew).
    See ncaaf/docs/data/game-script.md.
    """
    tilt = _mix_spread_tilt(
        pass_rate,
        opp_pass_rate,
        team_spread,
        opp_pass_ppa=opp_pass_ppa,
        opp_rush_ppa=opp_rush_ppa,
    )
    pos = (position or "").upper()
    blowout_rb1 = (
        pos == "RB"
        and depth_rank == 1
        and team_spread is not None
        and float(team_spread) <= -SIT_RUN
    )
    if pos in PASS_POS or blowout_rb1:
        # Featured RB on a huge favorite recedes with the lead (not clock-chew).
        m = tilt
    else:
        m = 2.0 - tilt
    m = _clamp(m, CLAMP_LO, CLAMP_HI)
    m *= _sit_factor(pos, team_spread, depth_rank)
    return _clamp(m, OUT_LO, OUT_HI)


def _mix_spread_tilt(
    pass_rate: float | None,
    opp_pass_rate: float | None,
    team_spread: float | None,
    opp_pass_ppa: float | None = None,
    opp_rush_ppa: float | None = None,
) -> float:
    if pass_rate is None:
        tilt = 1.0 if team_spread is None else 1.0 + SPREAD_COEF * float(team_spread)
    else:
        tilt = 1.0 + 0.7 * (float(pass_rate) - FBS_PASS)
        if opp_pass_rate is not None:
            tilt *= 1.0 + 0.35 * (float(opp_pass_rate) - FBS_PASS)
        if team_spread is not None:
            tilt *= 1.0 + SPREAD_COEF * float(team_spread)
    return tilt * _def_ppa_factor(opp_pass_ppa, opp_rush_ppa)


def _def_ppa_factor(
    opp_pass_ppa: float | None,
    opp_rush_ppa: float | None,
) -> float:
    """>1 when opponent D is leakier vs pass than run. 1.0 if missing."""
    if opp_pass_ppa is None or opp_rush_ppa is None:
        return 1.0
    gap = float(opp_pass_ppa) - float(opp_rush_ppa)
    gap = max(-DEF_PPA_GAP_CAP, min(DEF_PPA_GAP_CAP, gap))
    return 1.0 + DEF_PPA_COEF * gap


def _sit_factor(
    position: str,
    team_spread: float | None,
    depth_rank: int | None,
) -> float:
    if team_spread is None or float(team_spread) > -SIT_RUN:
        return 1.0
    span = SIT_FULL - SIT_RUN
    t = min(1.0, (-float(team_spread) - SIT_RUN) / span)
    if position in {"WR", "TE"}:
        lo, hi = SIT_WR1 if depth_rank == 1 else SIT_WR
        return lo + (hi - lo) * t
    if position == "QB":
        lo, hi = SIT_QB
        return lo + (hi - lo) * t
    if position == "RB":
        if depth_rank == 1:
            lo, hi = SIT_RB1
            return lo + (hi - lo) * t
        if depth_rank in {2, 3}:
            lo, hi = BOOST_RB23
            return lo + (hi - lo) * t
        return 1.0
    return 1.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
