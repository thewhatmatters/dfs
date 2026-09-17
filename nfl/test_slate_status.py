"""Slate coverage report + --slate-status flag (no network)."""

from __future__ import annotations

import unittest

from nfl.optimize import parse_args
from nfl.slate_status import build_slate_status, format_slate_status


class ParseSlateStatusTest(unittest.TestCase):
    def test_flag_defaults_to_only(self):
        self.assertIsNone(parse_args(["--csv", "x.csv"]).slate_status)
        self.assertEqual(
            parse_args(["--csv", "x.csv", "--slate-status"]).slate_status,
            "only",
        )
        self.assertEqual(
            parse_args(
                ["--csv", "x.csv", "--slate-status=with-solve"]
            ).slate_status,
            "with-solve",
        )


class BuildSlateStatusTest(unittest.TestCase):
    def test_counts_and_gaps(self):
        pool = [
            {"position": "QB"},
            {"position": "RB"},
            {"position": "WR"},
            {"position": "TE"},
            {"position": "D"},
        ]
        status = build_slate_status(
            raw_n=10,
            after_ir_n=8,
            after_inj_n=7,
            pool=pool,
            depth_source="ourlads",
            depth={"skipped": False, "matched": 3, "source": "ourlads"},
            targets={"skipped": False, "joined": 2, "slate_rb_wr_te": 3, "week": 2},
            snaps={"skipped": False, "joined": 2, "slate_rb_wr_te": 3, "week": 2},
            props={
                "skipped": False,
                "players_with_props": 1,
                "credits_remaining": 80,
            },
            injuries={"skipped": False, "dropped": 1, "unmatched": 2},
        )
        self.assertEqual(status["pool"]["csv"], 10)
        self.assertEqual(status["pool"]["after_ir_na"], 8)
        self.assertEqual(status["pool"]["after_injuries"], 7)
        self.assertEqual(status["depth"]["matched"], 3)
        self.assertEqual(status["depth"]["eligible"], 4)
        self.assertEqual(status["targets"]["joined"], 2)
        self.assertEqual(status["targets"]["label"], "RB+WR+TE")
        self.assertEqual(status["snaps"]["joined"], 2)
        self.assertEqual(status["props"]["joined"], 1)
        self.assertEqual(status["injuries"]["dropped"], 1)
        self.assertEqual(status["gaps"], [])
        text = format_slate_status(status)
        self.assertIn("slate status", text)
        self.assertIn("10 csv → 8 after IR/NA → 7 after injuries", text)
        self.assertIn("depth  ourlads  3 / 4 skill", text)
        self.assertIn("targets  2 / 3 RB+WR+TE  week 2", text)
        self.assertIn("snaps  2 / 3 RB+WR+TE  week 2", text)
        self.assertIn("props  1 / 4 skill  credits_left 80", text)
        self.assertIn("injuries  dropped 1  unmatched 2", text)
        self.assertIn("gaps  none", text)

    def test_skipped_and_key_gaps(self):
        status = build_slate_status(
            raw_n=5,
            after_ir_n=5,
            after_inj_n=5,
            pool=[{"position": "WR"}],
            flags={
                "skip_depth": True,
                "skip_targets": True,
                "skip_snaps": True,
                "skip_props": True,
                "skip_injuries": True,
            },
            props={"skipped": True, "reason": "PROPS_ODDS_KEY"},
            targets={"skipped": True, "choke": "TARGETS_CSV"},
            snaps={"skipped": True},
        )
        text = format_slate_status(status)
        self.assertIn("depth  ourlads  skipped", text)
        self.assertIn("targets  skipped (TARGETS_CSV)", text)
        self.assertIn("snaps  skipped", text)
        self.assertIn("props  skipped (no Odds key)", text)
        self.assertIn("injuries  skipped", text)
        self.assertIn("targets TARGETS_CSV", text)
        self.assertIn("props skipped (no Odds key)", "\n".join(status["gaps"]))


if __name__ == "__main__":
    unittest.main()
