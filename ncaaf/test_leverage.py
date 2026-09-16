"""GPP leverage seats: +14 WR1 and +21 dog QB, not name-fitting."""

from __future__ import annotations

import unittest
from dataclasses import replace

from ncaaf.leverage import dog_wr1, huge_dog_qb
from ncaaf.players import Player
from ncaaf.rules import FANDUEL_NCAAF
from ncaaf.solver import solve_ilp


def _p(pid, pos, team, salary, obj, *, spread=0.0, depth=1, opp="X"):
    return Player(
        pid=pid,
        name=pid,
        position=pos,
        salary=salary,
        team=team,
        opponent=opp,
        game="A@B",
        fppg=None,
        injury="",
        roster_position="",
        objective=obj,
        spread=spread,
        depth_rank=depth,
    )


def _pool():
    # Favorite SuperFLEX + grind WRs win unconstrained mean.
    return [
        _p("qb_fav", "QB", "ORE", 10500, 20.0, spread=-23.5, opp="OKST"),
        _p("qb_dog", "QB", "OKST", 8500, 10.0, spread=23.5, opp="ORE"),
        _p("qb_mid", "QB", "CAL", 9100, 16.0, spread=3.5, opp="SYR"),
        _p("rb1", "RB", "DUKE", 10600, 14.0, spread=6.0),
        _p("rb2", "RB", "ORE", 6400, 13.0, spread=-23.5, depth=2),
        _p("wr_grind", "WR", "GT", 7200, 8.2, spread=11.75),
        _p("wr_mid", "WR", "CAL", 7200, 7.3, spread=3.5),
        _p("wr_star", "WR", "ILL", 8600, 7.7, spread=-6.0),
        _p("wr_dog", "WR", "ASU", 6700, 4.8, spread=14.5),
        _p("wr_dog_d2", "WR", "ORST", 6900, 3.1, spread=25.5, depth=2),
    ]


class DogClassifiersTest(unittest.TestCase):
    def test_wr1_plus_14_is_dog_wr1_d2_is_not(self):
        wr1 = _p("wr_dog", "WR", "ASU", 6700, 4.8, spread=14.5)
        d2 = _p("wr_dog_d2", "WR", "ORST", 6900, 3.1, spread=25.5, depth=2)
        te = _p("te_dog", "TE", "ASU", 5000, 2.0, spread=14.5)
        self.assertTrue(dog_wr1(wr1))
        self.assertFalse(dog_wr1(d2))
        self.assertTrue(dog_wr1(te))

    def test_sit_band_dog_qb_only(self):
        dog = _p("qb_dog", "QB", "OKST", 8500, 10.0, spread=23.5)
        mid = _p("qb_mid", "QB", "CAL", 9100, 16.0, spread=3.5)
        self.assertTrue(huge_dog_qb(dog))
        self.assertFalse(huge_dog_qb(mid))


class LeverageIlpTest(unittest.TestCase):
    def test_off_picks_favorite_qb_and_skips_cheap_dog_wr(self):
        lu = solve_ilp(_pool(), replace(FANDUEL_NCAAF, salary_floor=0), leverage=False)
        names = {p.pid for p in lu.slots.values()}
        self.assertIn("qb_fav", names)
        self.assertNotIn("qb_dog", names)
        self.assertNotIn("wr_dog", names)

    def test_on_forces_dog_qb_and_wr1_not_d2(self):
        lu = solve_ilp(_pool(), replace(FANDUEL_NCAAF, salary_floor=0), leverage=True)
        names = {p.pid for p in lu.slots.values()}
        self.assertIn("qb_dog", names)
        self.assertIn("wr_dog", names)
        self.assertNotIn("wr_dog_d2", names)
        self.assertTrue(any("leverage:" in n for n in lu.notes))

    def test_no_dogs_in_pool_is_a_noop(self):
        pool = [
            p
            for p in _pool()
            if p.pid not in {"qb_dog", "wr_dog", "wr_dog_d2"}
        ]
        lu = solve_ilp(pool, replace(FANDUEL_NCAAF, salary_floor=0), leverage=True)
        names = {p.pid for p in lu.slots.values()}
        self.assertIn("qb_fav", names)
        self.assertNotIn("wr_dog", names)


if __name__ == "__main__":
    unittest.main()
