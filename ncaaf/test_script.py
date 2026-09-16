"""Pass/rush script multipliers."""

from __future__ import annotations

import unittest

from ncaaf.projections import week1_score
from ncaaf.script import DEF_PPA_COEF, DEF_PPA_GAP_CAP, script_mult


class ScriptMultTest(unittest.TestCase):
    def test_none_is_identity(self):
        self.assertEqual(script_mult("QB", pass_rate=None), 1.0)
        self.assertEqual(script_mult("RB", pass_rate=None), 1.0)
        self.assertEqual(script_mult("WR", pass_rate=None, team_spread=None), 1.0)
        self.assertEqual(script_mult("WR", pass_rate=None, team_spread=0.0), 1.0)

    def test_spread_only_wr_starter_below_identity(self):
        wr = script_mult("WR", pass_rate=None, team_spread=-28.0, depth_rank=1)
        rb = script_mult("RB", pass_rate=None, team_spread=-28.0, depth_rank=1)
        qb = script_mult("QB", pass_rate=None, team_spread=-28.0, depth_rank=1)
        self.assertLess(wr, 1.0)
        self.assertLess(qb, 1.0)
        self.assertGreater(rb, wr)
        self.assertLess(rb, 1.0)
        wr_obj = week1_score(40.0, 8000, 1, "WR", team_spread=-28.0)
        wr_id = week1_score(40.0, 8000, 1, "WR", team_spread=0.0)
        rb_obj = week1_score(40.0, 8000, 1, "RB", team_spread=-28.0)
        self.assertLess(wr_obj, wr_id)
        self.assertGreater(rb_obj, wr_obj)

    def test_sit_scales_past_mix_clamp(self):
        wr28 = script_mult("WR", pass_rate=None, team_spread=-28.0, depth_rank=1)
        wr40 = script_mult("WR", pass_rate=None, team_spread=-40.0, depth_rank=1)
        self.assertLess(wr40, wr28)
        wr1 = script_mult("WR", pass_rate=None, team_spread=-28.0, depth_rank=1)
        wr2 = script_mult("WR", pass_rate=None, team_spread=-28.0, depth_rank=2)
        self.assertLess(wr1, wr2)

    def test_blowout_rb1_sits_rb23_boost(self):
        rb1 = script_mult("RB", pass_rate=None, team_spread=-28.0, depth_rank=1)
        rb2 = script_mult("RB", pass_rate=None, team_spread=-28.0, depth_rank=2)
        rb1_grind = script_mult(
            "RB", pass_rate=None, team_spread=-10.0, depth_rank=1
        )
        self.assertGreater(rb2, rb1)
        self.assertGreater(rb1_grind, rb1)

    def test_dog_does_not_sit(self):
        wr = script_mult("WR", pass_rate=None, team_spread=28.0, depth_rank=1)
        self.assertGreater(wr, 1.0)

    def test_pass_heavy_boosts_qb_not_rb(self):
        qb = script_mult("QB", pass_rate=0.70)
        rb = script_mult("RB", pass_rate=0.70)
        wr = script_mult("WR", pass_rate=0.70)
        self.assertGreater(qb, 1.0)
        self.assertGreater(wr, 1.0)
        self.assertLess(rb, 1.0)

    def test_dog_throws_more_than_favorite(self):
        dog = script_mult("QB", pass_rate=0.55, team_spread=14.0)
        fav = script_mult("QB", pass_rate=0.55, team_spread=-14.0)
        self.assertGreater(dog, fav)

    def test_objective_pass_heavy_qb_beats_same_team_rb(self):
        qb = week1_score(40.0, 10000, 1, "QB", pass_rate=0.70)
        rb = week1_score(40.0, 8000, 1, "RB", pass_rate=0.70)
        self.assertGreater(qb, rb)

    def test_leaky_pass_d_boosts_wr_not_rb(self):
        wr = script_mult(
            "WR",
            pass_rate=0.55,
            opp_pass_ppa=0.40,
            opp_rush_ppa=0.05,
        )
        rb = script_mult(
            "RB",
            pass_rate=0.55,
            opp_pass_ppa=0.40,
            opp_rush_ppa=0.05,
        )
        wr0 = script_mult("WR", pass_rate=0.55)
        self.assertGreater(wr, wr0)
        self.assertLess(rb, wr)

    def test_def_ppa_gap_is_capped(self):
        wide = script_mult(
            "QB",
            pass_rate=0.55,
            opp_pass_ppa=1.0,
            opp_rush_ppa=0.0,
        )
        cap = script_mult(
            "QB",
            pass_rate=0.55,
            opp_pass_ppa=DEF_PPA_GAP_CAP,
            opp_rush_ppa=0.0,
        )
        self.assertAlmostEqual(wide, cap)
        self.assertAlmostEqual(cap, 1.0 + DEF_PPA_COEF * DEF_PPA_GAP_CAP, places=6)

    def test_props_still_see_script(self):
        with_script = week1_score(40.0, 10000, 1, "QB", prop_fd=30.0, pass_rate=0.90)
        no_script = week1_score(40.0, 10000, 1, "QB", prop_fd=30.0, pass_rate=None)
        spread_only = week1_score(
            40.0, 10000, 1, "WR", prop_fd=12.0, team_spread=-28.0
        )
        no_spread = week1_score(40.0, 10000, 1, "WR", prop_fd=12.0, team_spread=None)
        self.assertNotAlmostEqual(with_script, no_script)
        self.assertNotAlmostEqual(spread_only, no_spread)


if __name__ == "__main__":
    unittest.main()
