"""Stdlib box tables for CLI stderr."""

from __future__ import annotations

import unittest

from cli_table import (
    GREEN,
    RED,
    format_picker_sources,
    format_picker_table,
    format_table,
    json_stdout_enabled,
)


class FormatTableTest(unittest.TestCase):
    def test_box_chars(self):
        text = format_table(["A", "B"], [["1", "2"]], box=True)
        for ch in "┌┬┐│─┼└┘":
            self.assertIn(ch, text)

    def test_ascii_fallback(self):
        text = format_table(["A", "B"], [["1", "2"]], box=False)
        self.assertIn("+", text)
        self.assertIn("|", text)
        self.assertNotIn("┌", text)


class PickerTableTest(unittest.TestCase):
    def test_picker_headers_slot_and_proj(self):
        lu = {
            "picker": [
                {
                    "slot": "QB",
                    "name": "Joe Burrow",
                    "position": "QB",
                    "team": "CIN",
                    "opponent": "TB",
                    "game": "CIN@TB",
                    "implied_total": 27.0,
                    "implied_opp": 23.5,
                    "salary": 8200,
                    "fppg": 18.2,
                    "projection": 21.4,
                    "floor": 17.2,
                    "ceiling": 28.6,
                    "starter": True,
                    "note": "Odds props (FanDuel): 267.5 pass yds, 2.5 pass TDs, 6.5 rush yds.",
                    "prop_pass_yds": 267.5,
                    "prop_pass_tds": 2.5,
                    "prop_rush_yds": 6.5,
                    "prop_status": "props",
                    "depth_rank": 1,
                    "depth_source": "ourlads",
                },
                {
                    "slot": "WR",
                    "name": "Amon-Ra St. Brown",
                    "position": "WR",
                    "team": "DET",
                    "opponent": "CHI",
                    "game": "DET@CHI",
                    "implied_total": 24.5,
                    "implied_opp": 21.0,
                    "salary": 7800,
                    "fppg": 14.0,
                    "projection": 16.2,
                    "floor": 11.0,
                    "ceiling": 24.0,
                    "starter": True,
                    "depth_rank": 1,
                    "depth_source": "ourlads",
                    "target_share": 0.28,
                    "snap_share": 0.91,
                },
                {
                    "slot": "DEF",
                    "name": "Titans",
                    "position": "D",
                    "team": "TEN",
                    "opponent": "NYJ",
                    "game": "NYJ@TEN",
                    "implied_total": 21.0,
                    "implied_opp": 22.5,
                    "salary": 4000,
                    "fppg": 5.1,
                    "projection": 8.0,
                    "floor": 4.0,
                    "ceiling": 14.0,
                    "note": "DST PA proxy: opp implied 22.5 → dst_pa_21_27 (1 pts) + 3.0 sack/TO prior.",
                    "implied_opp": 22.5,
                },
            ],
            "salary": 59900,
            "lineup_proj": 93.3,
            "lineup_floor": 72.9,
            "lineup_ceiling": 129.3,
            "cash_line": 150.0,
            "ceiling_minus_cash": -20.7,
        }
        text = format_picker_table(lu, box=True, color=False)
        self.assertIn("┌", text)
        self.assertIn("Slot", text)
        self.assertIn("Proj", text)
        self.assertIn("FPPG", text)
        self.assertIn("18.2", text)
        self.assertIn("Props", text)
        self.assertIn("Sources", text)
        self.assertIn("ourlads/gs-props", text)
        self.assertIn("ourlads/lineups-tgt/lineups-snap", text)
        self.assertIn("vegas-dst", text)
        self.assertIn("Joe Burrow (*)", text)
        self.assertIn("Opp", text)
        self.assertIn("CIN (A) 27", text)
        self.assertIn("TB (H) 24", text)
        self.assertIn("TEN (H) 21", text)
        self.assertIn("NYJ (A) 23", text)
        self.assertNotIn("Imp", text)
        self.assertNotIn("@TB", text)
        colored = format_picker_table(lu, box=True, color=True)
        self.assertIn(GREEN, colored)
        self.assertIn(RED, colored)
        self.assertIn("$8,200", text)
        self.assertIn("21.4 (2.6x)", text)
        self.assertIn("8.0 (2.0x)", text)
        self.assertIn("267.5 pass / 2.5 TD / 6.5 rush", text)
        self.assertIn("PA 22.5", text)
        self.assertIn("Lineup", text)
        self.assertIn("cash 150", text)
        self.assertNotIn("Odds props", text)
        ascii_text = format_picker_table(lu, box=False, color=False)
        self.assertIn("+", ascii_text)
        self.assertIn("Props", ascii_text)
        self.assertIn("Sources", ascii_text)
        self.assertNotIn("┌", ascii_text)

    def test_picker_sources_tags(self):
        self.assertEqual(
            format_picker_sources(
                {
                    "position": "WR",
                    "depth_rank": 1,
                    "depth_source": "ourlads",
                    "target_share": 0.24,
                }
            ),
            "ourlads/lineups-tgt",
        )
        self.assertEqual(
            format_picker_sources(
                {"position": "WR", "depth_rank": 2, "depth_source": "espn"}
            ),
            "espn",
        )
        self.assertEqual(
            format_picker_sources({"position": "QB", "prop_status": "props"}),
            "gs-props",
        )
        self.assertEqual(
            format_picker_sources(
                {
                    "position": "TE",
                    "depth_rank": 1,
                    "depth_source": "gangstash",
                    "target_share": 0.22,
                    "targets_source": "gangstash",
                    "snap_share": 0.81,
                    "snaps_source": "gangstash",
                    "prop_fd": 12.4,
                    "prop_book": "gangstash",
                }
            ),
            "gs-depth/gs-tgt/gs-snap/gs-props",
        )
        self.assertEqual(
            format_picker_sources({"position": "D", "implied_opp": 22.5}),
            "vegas-dst",
        )
        self.assertEqual(format_picker_sources({"position": "RB"}), "-")
        self.assertEqual(
            format_picker_sources({"position": "WR", "sources": "ourlads/lineups-tgt"}),
            "ourlads/lineups-tgt",
        )

    def test_json_stdout_flags(self):
        self.assertTrue(json_stdout_enabled(agent=True, json_flag=False))
        self.assertTrue(json_stdout_enabled(agent=False, json_flag=True))


if __name__ == "__main__":
    unittest.main()
