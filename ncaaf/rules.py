"""FanDuel NCAAF classic contest constraints.

Numbers and scoring live in ncaaf/docs/sites/fanduel-ncaaf.md. This module is the
code seam the optimizer and tests share — change the doc and this file together.
"""

from dataclasses import dataclass, field
from typing import FrozenSet


@dataclass(frozen=True)
class FanDuelNcaafClassic:
    site: str = "fanduel"
    sport: str = "ncaaf"
    salary_cap: int = 60_000
    # House rule (not FanDuel): spend at least this much. 0 disables.
    salary_floor: int = 58_000
    roster: tuple[tuple[str, int, FrozenSet[str]], ...] = (
        ("QB", 1, frozenset({"QB"})),
        ("RB", 2, frozenset({"RB"})),
        ("WR", 3, frozenset({"WR", "TE"})),
        ("SUPERFLEX", 1, frozenset({"QB", "RB", "WR", "TE"})),
    )
    # House default: SuperFLEX is a second QB (FanDuel allows any skill).
    superflex_eligible: FrozenSet[str] = frozenset({"QB"})
    min_teams: int = 3
    max_per_team: int = 4
    # CFB official table has no yardage bonuses (unlike FanDuel NFL).
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
        }
    )
    out_codes: FrozenSet[str] = frozenset({"O"})
    questionable_codes: FrozenSet[str] = frozenset({"Q", "D"})

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
        legal = next(e for s, _c, e in self.roster if s == "SUPERFLEX")
        extra = self.superflex_eligible - legal
        if extra:
            raise ValueError(f"superflex_eligible not FanDuel-legal: {sorted(extra)}")

    def eligible_positions(self, slot_family: str) -> FrozenSet[str]:
        if slot_family == "SUPERFLEX":
            return self.superflex_eligible
        for slot, _count, elig in self.roster:
            if slot == slot_family:
                return elig
        raise KeyError(slot_family)


FANDUEL_NCAAF = FanDuelNcaafClassic()

# FanDuel classic picker order (lobby: Select QB, RB, RB, WR, WR, WR, Super FLEX).
# Internal keys stay unique (RB1/RB2/…) for the ILP.
FANDUEL_PICKER_ORDER: tuple[tuple[str, str], ...] = (
    ("QB", "QB"),
    ("RB1", "RB"),
    ("RB2", "RB"),
    ("WR1", "WR"),
    ("WR2", "WR"),
    ("WR3", "WR"),
    ("SUPERFLEX", "Super FLEX"),
)
