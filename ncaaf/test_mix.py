"""CFBD mix parse + usage prior. No live HTTP."""

from __future__ import annotations

import unittest

from ncaaf.mix import attach_mix, ingest_mix, parse_team_mix, parse_usage
from ncaaf.players import Player
from ncaaf.projections import role_prior, week1_score


def _p(**kw) -> Player:
    fields = dict(
        pid="1",
        name="Cooper Perry",
        position="WR",
        salary=5700,
        team="CAL",
        opponent="SYR",
        game="CAL@SYR",
        fppg=None,
        injury="",
        roster_position="WR/Super FLEX",
        implied_total=26.5,
        spread=3.5,
        depth_rank=1,
    )
    fields.update(kw)
    return Player(**fields)


ADV = [
    {
        "team": "California",
        "offense": {"passingPlays": {"rate": 0.52}},
        "defense": {"passingPlays": {"rate": 0.45}},
    },
    {
        "team": "Syracuse",
        "offense": {"passingPlays": {"rate": 0.60}},
        "defense": {
            "passingPlays": {"rate": 0.48, "ppa": 0.40},
            "rushingPlays": {"ppa": 0.05},
        },
    },
    {"team": "Ghost", "offense": {}, "defense": {}},
]

USAGE = [
    {
        "name": "Cooper Perry",
        "team": "California",
        "position": "WR",
        "usage": {"pass": 0.087, "rush": 0.0},
    },
    {
        "name": "Ian Strong",
        "team": "California",
        "position": "WR",
        "usage": {"pass": 0.217, "rush": 0.0},
    },
]


class ParseMixTest(unittest.TestCase):
    def test_pass_rate_and_skip_empty(self):
        mix = parse_team_mix(ADV)
        self.assertAlmostEqual(mix["california"].pass_rate, 0.52)
        self.assertAlmostEqual(mix["syracuse"].opp_pass_rate, 0.48)
        self.assertAlmostEqual(mix["syracuse"].def_pass_ppa, 0.40)
        self.assertAlmostEqual(mix["syracuse"].def_rush_ppa, 0.05)
        self.assertNotIn("ghost", mix)

    def test_usage_match_key(self):
        u = parse_usage(USAGE)
        self.assertIn(("california", "cooper perry"), u)
        self.assertAlmostEqual(u[("california", "cooper perry")].catch, 0.087)

    def test_pre_2026_noop(self):
        teams, usage = ingest_mix(2025)
        self.assertEqual(teams, {})
        self.assertEqual(usage, {})


class UsagePriorTest(unittest.TestCase):
    def test_wr_usage_splits_equal_depth(self):
        perry = role_prior(1, "WR", target_share=0.087)
        strong = role_prior(1, "WR", target_share=0.217)
        depth = role_prior(1, "WR")
        self.assertAlmostEqual(depth, 1.0)
        self.assertLess(perry, strong)
        self.assertLess(perry, depth)

    def test_props_still_see_usage(self):
        a = week1_score(26.5, 5700, 1, "WR", prop_fd=6.1, target_share=0.087)
        b = week1_score(26.5, 5700, 1, "WR", prop_fd=6.1, target_share=0.50)
        self.assertLess(a, b)

    def test_attach_rescores(self):
        mix = parse_team_mix(ADV)
        usage = parse_usage(USAGE)
        perry = _p()
        strong = _p(pid="2", name="Ian Strong", salary=6400)
        out, stats = attach_mix([perry, strong], mix, usage)
        self.assertEqual(stats["usage_joined"], 2)
        self.assertEqual(stats["teams_with_mix"], 1)
        self.assertAlmostEqual(out[0].pass_rate, 0.52)
        self.assertAlmostEqual(out[0].opp_pass_rate, 0.48)
        self.assertAlmostEqual(out[0].opp_pass_ppa, 0.40)
        self.assertAlmostEqual(out[0].opp_rush_ppa, 0.05)
        self.assertLess(out[0].objective, out[1].objective)


if __name__ == "__main__":
    unittest.main()
