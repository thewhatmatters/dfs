"""Salary floor is a house constraint (FanDuel only enforces the cap)."""

from __future__ import annotations

import unittest
from dataclasses import replace

from ncaaf.players import Player
from ncaaf.rules import FANDUEL_NCAAF, FanDuelNcaafClassic
from ncaaf.solver import solve_ilp


def _p(pid, pos, team, salary, obj=10.0):
    return Player(
        pid=pid,
        name=pid,
        position=pos,
        salary=salary,
        team=team,
        opponent="X",
        game="A@B",
        fppg=None,
        injury="",
        roster_position="",
        objective=obj,
    )


class SalaryFloorTest(unittest.TestCase):
    def test_floor_cannot_exceed_cap(self):
        with self.assertRaises(ValueError):
            FanDuelNcaafClassic(salary_floor=70_000)

    def test_ilp_meets_floor(self):
        # Cheap legal 7-man set is $28k; with a floor the solver must spend up.
        pool = [
            _p("qb_cheap", "QB", "T1", 4_000),
            _p("qb_dear", "QB", "T1", 12_000),
            _p("rb1", "RB", "T2", 4_000),
            _p("rb2", "RB", "T2", 4_000),
            _p("wr1", "WR", "T3", 4_000),
            _p("wr2", "WR", "T3", 4_000),
            _p("wr3", "WR", "T3", 4_000),
            _p("wr_flex", "WR", "T2", 4_000),
            _p("wr4", "WR", "T1", 12_000),
            _p("rb3", "RB", "T3", 9_000),
        ]
        rules = replace(FANDUEL_NCAAF, salary_floor=40_000, salary_cap=60_000)
        floored = solve_ilp(pool, rules)
        self.assertGreaterEqual(floored.salary, 40_000)
        self.assertLessEqual(floored.salary, 60_000)
        self.assertEqual(floored.slots["SUPERFLEX"].position, "QB")

    def test_superflex_any_can_be_wr(self):
        pool = [
            _p("qb1", "QB", "T1", 8_000, obj=50),
            _p("rb1", "RB", "T2", 8_000, obj=50),
            _p("rb2", "RB", "T2", 8_000, obj=50),
            _p("wr1", "WR", "T3", 8_000, obj=50),
            _p("wr2", "WR", "T3", 8_000, obj=50),
            _p("wr3", "WR", "T3", 8_000, obj=50),
            _p("wr_star", "WR", "T1", 9_000, obj=99),
        ]
        rules = replace(
            FANDUEL_NCAAF,
            salary_floor=0,
            superflex_eligible=frozenset({"QB", "RB", "WR", "TE"}),
        )
        lu = solve_ilp(pool, rules)
        self.assertIn("wr_star", {p.pid for p in lu.slots.values()})
        self.assertEqual(lu.slots["SUPERFLEX"].position, "WR")
        labels = [row["slot"] for row in lu.to_dict()["picker"]]
        self.assertEqual(
            labels, ["QB", "RB", "RB", "WR", "WR", "WR", "Super FLEX"]
        )


if __name__ == "__main__":
    unittest.main()
