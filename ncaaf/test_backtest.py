"""Overlap vs a hindsight perfect card."""

from __future__ import annotations

import unittest

from ncaaf.backtest import compare


class BacktestOverlapTest(unittest.TestCase):
    def test_one_hit_is_not_improved_until_more(self):
        perfect = {
            "contest": "133865",
            "players": [
                {"name": "Jeremiah Smith", "team": "OSU"},
                {"name": "Nate Sheppard", "team": "DUKE"},
            ],
            "baseline_lock": {
                "players": [{"name": "Jeremiah Smith", "team": "OSU"}]
            },
        }
        solved = {
            "lineup": {
                "picker": [
                    {"name": "Jeremiah Smith", "team": "OSU"},
                    {"name": "Nate Sheppard", "team": "DUKE"},
                ],
                "salary": 20000,
            }
        }
        r = compare(solved, perfect)
        self.assertEqual(r["overlap"], 2)
        self.assertEqual(r["baseline_overlap"], 1)
        self.assertTrue(r["improved"])
        self.assertIn("Nate Sheppard", r["hits"])

    def test_generational_suffix(self):
        perfect = {"players": [{"name": "JC French IV", "team": "CIN"}]}
        solved = {"lineup": {"picker": [{"name": "JC French", "team": "CIN"}]}}
        r = compare(solved, perfect)
        self.assertEqual(r["overlap"], 1)


if __name__ == "__main__":
    unittest.main()
