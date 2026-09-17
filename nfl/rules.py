"""FanDuel NFL classic contest constraints.

Numbers and scoring live in nfl/docs/sites/fanduel-nfl.md. This module is the
code seam the optimizer and tests share — change the doc and this file together.

Optimizer: python3 -m nfl.optimize
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, FrozenSet, NamedTuple, Sequence


@dataclass(frozen=True)
class FanDuelNflClassic:
    site: str = "fanduel"
    sport: str = "nfl"
    salary_cap: int = 60_000
    # House rule (not FanDuel): spend at least this much. 0 disables.
    salary_floor: int = 58_000
    roster: tuple[tuple[str, int, FrozenSet[str]], ...] = (
        ("QB", 1, frozenset({"QB"})),
        ("RB", 2, frozenset({"RB"})),
        ("WR", 3, frozenset({"WR"})),
        ("TE", 1, frozenset({"TE"})),
        ("FLEX", 1, frozenset({"RB", "WR", "TE"})),
        ("DEF", 1, frozenset({"D"})),
    )
    min_teams: int = 3
    # FanDuel lobby max is 4. House default is 3; --max-per-team=4 restores lobby.
    max_per_team: int = 3
    # House construction (not FanDuel): no opponent DST vs the lineup QB
    # or a stud RB (salary >= stud_rb_min_salary). Cheap RB2 / handcuff
    # does not get the ban. FLEX stud RB counts (CSV Position RB).
    forbid_qb_opp_dst: bool = True
    forbid_stud_rb_opp_dst: bool = True
    stud_rb_min_salary: int = 7000
    # House construction (not FanDuel): require N opposing WR/TE/QB vs a
    # QB pass stack. 0 disables (default). RB and DST do not count.
    bring_back: int = 0
    # House construction (not FanDuel): 2+ WR/TE from a team (FLEX WR/TE
    # counts) requires that team's QB in the 9. One WR without QB is legal.
    # Two RBs without QB is legal. Hampton (RB) does not trigger.
    # --stack-qb=off disables.
    require_qb_with_two_pass_catchers: bool = True
    scoring: dict[str, float] = field(
        default_factory=lambda: {
            "pass_yd": 0.04,
            "pass_td": 4.0,
            "int": -1.0,
            "rush_yd": 0.1,
            "rush_td": 6.0,
            "rec_yd": 0.1,
            "rec": 0.5,
            "rec_td": 6.0,
            "fum_lost": -2.0,
            "own_fum_td": 6.0,
            "kr_td": 6.0,
            "pr_td": 6.0,
            "two_pt": 2.0,
            "two_pt_pass": 2.0,
            "bonus_pass_yd_300": 3.0,
            "bonus_rush_yd_100": 3.0,
            "bonus_rec_yd_100": 3.0,
            # DE/PA21-27 omitted on lobby tab; lock 0.
            "dst_pa_0": 10.0,
            "dst_pa_1_6": 7.0,
            "dst_pa_7_13": 4.0,
            "dst_pa_14_20": 1.0,
            "dst_pa_21_27": 0.0,
            "dst_pa_28_34": -1.0,
            "dst_pa_35_plus": -4.0,
            "dst_sack": 1.0,
            "dst_int": 2.0,
            "dst_fr": 2.0,
            "dst_safety": 2.0,
            "dst_blocked": 2.0,
            "dst_xpr": 2.0,
            "dst_rtd": 6.0,
            "dst_brtd": 6.0,
            "dst_frtd": 6.0,
        }
    )
    # 133104 export: Q / IR / NA (no O/D/P). IR and NA are out-equivalent.
    out_codes: FrozenSet[str] = frozenset({"IR", "NA"})
    questionable_codes: FrozenSet[str] = frozenset({"Q"})

    @property
    def slot_names(self) -> list[str]:
        names: list[str] = []
        for slot, count, _elig in self.roster:
            if count == 1:
                names.append(slot)
            else:
                names.extend(f"{slot}{i}" for i in range(1, count + 1))
        return names

    def __post_init__(self) -> None:
        if self.salary_floor < 0:
            raise ValueError("salary_floor must be >= 0")
        if self.salary_floor > self.salary_cap:
            raise ValueError("salary_floor cannot exceed salary_cap")
        if self.bring_back < 0:
            raise ValueError("bring_back must be >= 0")
        if self.max_per_team < 1 or self.max_per_team > FANDUEL_MAX_PER_TEAM:
            raise ValueError(f"max_per_team must be 1..{FANDUEL_MAX_PER_TEAM}")

    def eligible_positions(self, slot_family: str) -> FrozenSet[str]:
        for slot, _count, elig in self.roster:
            if slot == slot_family:
                return elig
        raise KeyError(slot_family)


# FanDuel lobby max-per-team. House default on FanDuelNflClassic is 3.
FANDUEL_MAX_PER_TEAM = 4
# ILP house premium: STACK_COEF * catcher.projection when QB + teammate WR/TE
# are both selected. Printed Proj stays week1_score. RB/DST get no premium.
STACK_COEF = 0.12

FANDUEL_NFL = FanDuelNflClassic()

# House cash line (not FanDuel). 2025-ish NFL classic cash ~150 FD points.
# Independent sim understates stacked GPP scores; reporting only, not an ILP cut.
HOUSE_CASH_LINE = 150.0
# FanDuel upload max is 250; we cap below that.
MAX_LINEUPS = 150
MIN_UNIQUE_DEFAULT = 2
# n>1: differ by 3 vs every locked 9 (n=1 ignores uniqueness).
MIN_UNIQUE_MULTI_DEFAULT = 3
# n>1 default cap on any one player's share of the set. 1.0 disables.
MAX_EXPOSURE_DEFAULT = 0.60
DIVERSITY_CHALK = "chalk"
DIVERSITY_COVERAGE = "coverage"
DIVERSITY_CHOICES = (DIVERSITY_CHALK, DIVERSITY_COVERAGE)


def max_player_appearances(max_exposure: float, n_lineups: int) -> int | None:
    """Hard cap on how many of `n_lineups` a player may appear in.

    ``max_exposure >= 1`` (or n=1) is uncapped. Otherwise
    ``floor(max_exposure * n_lineups)``, at least 1.
    """
    if n_lineups < 1:
        raise ValueError("n_lineups must be >= 1")
    if max_exposure <= 0 or max_exposure > 1:
        raise ValueError("max_exposure must be in (0, 1]")
    if n_lineups == 1 or max_exposure >= 1.0:
        return None
    return max(1, int(max_exposure * n_lineups + 1e-9))

# FanDuel classic picker / upload order (template: QB, RB, RB, WR, WR, WR, TE, FLEX, DEF).
# Internal keys stay unique (RB1/RB2/…) for the ILP.
FANDUEL_PICKER_ORDER: tuple[tuple[str, str], ...] = (
    ("QB", "QB"),
    ("RB1", "RB"),
    ("RB2", "RB"),
    ("WR1", "WR"),
    ("WR2", "WR"),
    ("WR3", "WR"),
    ("TE", "TE"),
    ("FLEX", "FLEX"),
    ("DEF", "DEF"),
)


class SkillSide(NamedTuple):
    """One skill player for house DST correlation.

    `position` is CSV Position (QB/RB/WR/TE), never the slot name — a FLEX
    RB is position=RB.
    """

    position: str
    opp: str
    salary: int = 0


def qb_opp_dst_illegal(qb_team: str, qb_opp: str, def_team: str) -> bool:
    """True when DEF is the lineup QB's opponent.

    House stacking rule, not FanDuel scoring. Trigger is the QB (qb_opp ==
    def_team). Same-team DST stays legal; qb_team is unused.
    """
    _ = qb_team
    return opp_dst_illegal(def_team=def_team, qb_opp=qb_opp)


def opp_dst_illegal(
    *,
    def_team: str,
    qb_opp: str | None = None,
    rbs: Sequence[tuple[str, int]] = (),
    skill: Sequence[SkillSide] = (),
    stud_rb_min_salary: int = 7000,
) -> bool:
    """True when DEF is the opponent of the lineup QB or a stud RB.

    Illegal if:
    1. QB's opponent == DEF team (any QB salary), or
    2. any RB with salary >= stud_rb_min_salary has opponent == DEF team.

    `rbs` is (opponent, salary) for players already known to be RBs.
    `skill` is mixed skill; only CSV Position RB is considered (FLEX is a
    slot — pass position=RB). Cheap RBs do not trigger. Bring-back WRs
    are ignored.
    """
    dst = (def_team or "").strip().upper()
    if not dst:
        return False
    opp = (qb_opp or "").strip().upper()
    if opp and opp == dst:
        return True
    sides: list[tuple[str, int]] = list(rbs)
    for side in skill:
        if (side.position or "").strip().upper() != "RB":
            continue
        sides.append((side.opp, side.salary))
    for rb_opp, salary in sides:
        if salary >= stud_rb_min_salary and (rb_opp or "").strip().upper() == dst:
            return True
    return False


PASS_STACK_POS: FrozenSet[str] = frozenset({"WR", "TE"})
BRING_BACK_POS: FrozenSet[str] = frozenset({"WR", "TE", "QB"})


def _attr_upper(obj: Any, name: str) -> str:
    return (getattr(obj, name, None) or "").strip().upper()


def pass_stack_players(slots: Mapping[str, Any]) -> list:
    """WR/TE on the lineup QB's team. FLEX WR/TE counts; FLEX RB does not."""
    qb = slots.get("QB")
    if qb is None:
        return []
    team = _attr_upper(qb, "team")
    if not team:
        return []
    out = []
    for p in slots.values():
        if p is qb:
            continue
        if _attr_upper(p, "position") in PASS_STACK_POS and _attr_upper(p, "team") == team:
            out.append(p)
    return out


def bring_back_players(slots: Mapping[str, Any]) -> list:
    """WR/TE/QB whose team == QB.opponent. RB and DST do not count."""
    qb = slots.get("QB")
    if qb is None:
        return []
    opp = _attr_upper(qb, "opponent")
    if not opp:
        return []
    out = []
    for p in slots.values():
        if _attr_upper(p, "position") in BRING_BACK_POS and _attr_upper(p, "team") == opp:
            out.append(p)
    return out


def bring_back_illegal(slots: Mapping[str, Any], n: int) -> bool:
    """True when N>=1, a pass stack exists, and bring-back count < N.

    No pass stack (solo QB, no teammate WR/TE) → never illegal, even at N>=1.
    """
    if n <= 0:
        return False
    if not pass_stack_players(slots):
        return False
    return len(bring_back_players(slots)) < n


def max_per_team_illegal(slots: Mapping[str, Any], n: int) -> bool:
    """True when any team has more than `n` players in the 9."""
    counts: dict[str, int] = {}
    for p in slots.values():
        team = _attr_upper(p, "team")
        if not team:
            continue
        counts[team] = counts.get(team, 0) + 1
    return any(c > n for c in counts.values())


def pass_catchers_by_team(slots: Mapping[str, Any]) -> dict[str, list]:
    """WR/TE in the 9, grouped by team. FLEX WR/TE counts; FLEX RB does not."""
    by_team: dict[str, list] = {}
    for p in slots.values():
        if _attr_upper(p, "position") not in PASS_STACK_POS:
            continue
        team = _attr_upper(p, "team")
        if not team:
            continue
        by_team.setdefault(team, []).append(p)
    return by_team


def stack_qb_illegal(slots: Mapping[str, Any], enabled: bool = True) -> bool:
    """True when 2+ WR/TE from a team and that team's QB is not in the 9.

    Incomplete lineups (QB slot empty) are not illegal yet. One WR without
    QB is legal. Two RBs without QB is legal. RB never counts as a catcher.
    """
    if not enabled:
        return False
    qb = slots.get("QB")
    if qb is None:
        return False
    qb_team = _attr_upper(qb, "team")
    for team, catchers in pass_catchers_by_team(slots).items():
        if len(catchers) >= 2 and team != qb_team:
            return True
    return False


def stack_premium_points(slots: Mapping[str, Any], coef: float | None = None) -> float:
    """ILP house premium: coef × each teammate WR/TE projection vs the lineup QB.

    Printed Proj stays week1_score. RB/DST do not get the premium.
    """
    c = STACK_COEF if coef is None else float(coef)
    if c == 0:
        return 0.0
    qb = slots.get("QB")
    if qb is None:
        return 0.0
    team = _attr_upper(qb, "team")
    if not team:
        return 0.0
    pts = 0.0
    for p in slots.values():
        if p is qb:
            continue
        if _attr_upper(p, "position") not in PASS_STACK_POS:
            continue
        if _attr_upper(p, "team") != team:
            continue
        pts += c * float(getattr(p, "projection", 0.0) or 0.0)
    return pts


# DST expected-points-allowed → FanDuel PA bucket. 21–27 is 0 (lobby omit).
DST_SACK_TO_PRIOR = 3.0


def dst_pa_key(points_allowed: float) -> str:
    """Map expected points allowed onto a `dst_pa_*` scoring key."""
    p = float(points_allowed)
    if p < 1:
        return "dst_pa_0"
    if p < 7:
        return "dst_pa_1_6"
    if p < 14:
        return "dst_pa_7_13"
    if p < 21:
        return "dst_pa_14_20"
    if p < 28:
        return "dst_pa_21_27"
    if p < 35:
        return "dst_pa_28_34"
    return "dst_pa_35_plus"


def dst_pa_points(points_allowed: float, scoring: dict[str, float] | None = None) -> float:
    sc = scoring if scoring is not None else FANDUEL_NFL.scoring
    return float(sc[dst_pa_key(points_allowed)])


def dst_projection(implied_opp: float, scoring: dict[str, float] | None = None) -> float:
    """PA bucket from opponent implied total + sack/turnover prior.

    `implied_opp` ≈ expected points the DST allows. Without the +3.0 prior,
    a 21–27 PA DST is a 1-pt stub (bucket is 0).
    """
    return dst_pa_points(implied_opp, scoring) + DST_SACK_TO_PRIOR
