"""Slate coverage report + --slate-status flag (no network)."""

from __future__ import annotations

import unittest

from nfl.optimize import parse_args
from nfl.slate_status import (
    RELEVANT_SALARY_FLOOR,
    build_slate_status,
    format_slate_status,
    is_relevant,
)


def _pl(
    pos: str,
    salary: int,
    *,
    name: str = "",
    team: str = "KC",
    depth_rank: int | None = None,
    target_share: float | None = None,
    snap_share: float | None = None,
    prop_status: str | None = None,
    implied_opp: float | None = None,
) -> dict:
    return {
        "name": name or f"{pos}{salary}",
        "position": pos,
        "salary": salary,
        "team": team,
        "depth_rank": depth_rank,
        "target_share": target_share,
        "snap_share": snap_share,
        "prop_status": prop_status,
        "implied_opp": implied_opp,
    }


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


class RelevantBandFloorsTest(unittest.TestCase):
    def test_defaults_strictly_greater(self):
        self.assertEqual(RELEVANT_SALARY_FLOOR["QB"], 6500)
        self.assertEqual(RELEVANT_SALARY_FLOOR["WR"], 5000)
        self.assertEqual(RELEVANT_SALARY_FLOOR["RB"], 5000)
        self.assertEqual(RELEVANT_SALARY_FLOOR["TE"], 4500)
        self.assertEqual(RELEVANT_SALARY_FLOOR["D"], 3500)
        self.assertFalse(is_relevant({"position": "QB", "salary": 6500}))
        self.assertTrue(is_relevant({"position": "QB", "salary": 6501}))
        self.assertFalse(is_relevant({"position": "WR", "salary": 5000}))
        self.assertTrue(is_relevant({"position": "WR", "salary": 5001}))
        self.assertFalse(is_relevant({"position": "WR", "salary": 4500}))
        self.assertFalse(is_relevant({"position": "RB", "salary": 5000}))
        self.assertTrue(is_relevant({"position": "RB", "salary": 5001}))
        self.assertFalse(is_relevant({"position": "TE", "salary": 4500}))
        self.assertTrue(is_relevant({"position": "TE", "salary": 4501}))
        self.assertFalse(is_relevant({"position": "D", "salary": 3500}))
        self.assertTrue(is_relevant({"position": "DEF", "salary": 3501}))
        self.assertFalse(is_relevant({"position": "K", "salary": 9000}))


class BuildSlateStatusTest(unittest.TestCase):
    def test_counts_and_gaps(self):
        pool = [
            _pl(
                "QB",
                7000,
                name="Mahomes",
                depth_rank=1,
                prop_status="props",
            ),
            _pl(
                "RB",
                8000,
                name="Hunt",
                depth_rank=1,
                target_share=0.1,
                snap_share=0.6,
                prop_status="props",
            ),
            _pl(
                "WR",
                7500,
                name="Worthy",
                depth_rank=1,
                target_share=0.2,
                snap_share=0.8,
                prop_status="props",
            ),
            _pl(
                "TE",
                6000,
                name="Kelce",
                depth_rank=1,
                target_share=0.15,
                snap_share=0.7,
                prop_status="props",
            ),
            _pl("D", 4000, name="Chiefs", implied_opp=21.5),
        ]
        status = build_slate_status(
            raw_n=10,
            after_ir_n=8,
            after_inj_n=7,
            pool=pool,
            depth_source="ourlads",
            depth={"skipped": False, "source": "ourlads"},
            targets={"skipped": False, "week": 2},
            snaps={"skipped": False, "week": 2},
            props={
                "skipped": False,
                "credits_remaining": 80,
            },
            injuries={"skipped": False, "dropped": 1, "unmatched": 2},
        )
        self.assertEqual(status["pool"]["csv"], 10)
        self.assertEqual(status["pool"]["after_ir_na"], 8)
        self.assertEqual(status["pool"]["after_injuries"], 7)
        self.assertEqual(status["relevant"]["total"], 5)
        self.assertEqual(status["relevant"]["by_pos"]["QB"], 1)
        self.assertEqual(status["depth"]["matched"], 4)
        self.assertEqual(status["depth"]["eligible"], 4)
        self.assertEqual(status["depth"]["label"], "relevant skill")
        self.assertEqual(status["targets"]["matched"], 3)
        self.assertEqual(status["targets"]["label"], "relevant RB+WR+TE")
        self.assertEqual(status["snaps"]["matched"], 1)
        self.assertEqual(status["snaps"]["eligible"], 1)
        self.assertEqual(status["snaps"]["wr_te"]["matched"], 2)
        self.assertEqual(status["props"]["matched"], 4)
        self.assertEqual(status["dst"]["matched"], 1)
        self.assertEqual(status["dst"]["eligible"], 1)
        self.assertEqual(status["injuries"]["dropped"], 1)
        self.assertTrue(status["injuries"]["applied"])
        self.assertEqual(status["gaps"], [])
        text = format_slate_status(status)
        self.assertIn("slate status", text)
        self.assertIn("relevant  5 / 5  QB 1  RB 1  WR 1  TE 1  D 1", text)
        self.assertIn("10 csv → 8 after IR/NA → 7 after injuries", text)
        self.assertIn("depth  ourlads  4 / 4 relevant skill", text)
        self.assertIn("targets  3 / 3 relevant RB+WR+TE  week 2", text)
        self.assertIn("snaps  1 / 1 relevant RB  (WR/TE 2 / 2)  week 2", text)
        self.assertIn("props  4 / 4 relevant skill  credits_left 80", text)
        self.assertIn("dst  1 / 1 relevant DEF implied opp", text)
        self.assertIn("injuries  applied  dropped 1  unmatched 2", text)
        self.assertIn("gaps  none", text)
        self.assertIn("all pool  depth 4/4 skill", text)
        self.assertNotIn("missing depth", text)

    def test_salary_band_excludes_fillers(self):
        pool = [
            _pl("QB", 6600, name="StarterQB", depth_rank=1, prop_status="props"),
            _pl("QB", 6500, name="BackupQB", depth_rank=2, prop_status="props"),
            _pl(
                "WR",
                5001,
                name="CashWR",
                team="PHI",
                depth_rank=1,
                target_share=0.2,
                snap_share=0.8,
                prop_status="props",
            ),
            _pl(
                "WR",
                5000,
                name="FillerWR",
                team="PHI",
                depth_rank=3,
                target_share=0.05,
                snap_share=0.3,
                prop_status="props",
            ),
            _pl(
                "WR",
                4500,
                name="OldFloorWR",
                team="PHI",
                depth_rank=2,
                target_share=0.1,
            ),
            _pl(
                "RB",
                5001,
                name="CashRB",
                team="BUF",
                depth_rank=1,
                target_share=0.1,
                snap_share=0.7,
                prop_status="props",
            ),
            _pl(
                "RB",
                4900,
                name="FillerRB",
                team="BUF",
                snap_share=0.2,
            ),
            _pl(
                "TE",
                4501,
                name="CashTE",
                team="SF",
                depth_rank=1,
                target_share=0.12,
                snap_share=0.6,
                prop_status="props",
            ),
            _pl("TE", 4500, name="FillerTE", team="SF", target_share=0.04),
            _pl("D", 3600, name="CashDST", team="NYJ", implied_opp=17.0),
            _pl("D", 3500, name="FillerDST", team="NYJ", implied_opp=24.0),
        ]
        status = build_slate_status(
            raw_n=20,
            after_ir_n=15,
            after_inj_n=11,
            pool=pool,
            depth={"skipped": False, "source": "ourlads"},
            targets={"skipped": False},
            snaps={"skipped": False},
            props={"skipped": False},
            injuries={"skipped": False, "dropped": 0, "unmatched": 0},
        )
        self.assertEqual(status["pool"]["full"], 11)
        self.assertEqual(status["relevant"]["total"], 5)
        self.assertEqual(
            status["relevant"]["by_pos"],
            {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "D": 1},
        )
        self.assertEqual(status["depth"]["matched"], 4)
        self.assertEqual(status["depth"]["eligible"], 4)
        self.assertEqual(status["depth"]["all"]["matched"], 7)
        self.assertEqual(status["depth"]["all"]["eligible"], 9)
        self.assertEqual(status["targets"]["matched"], 3)
        self.assertEqual(status["targets"]["eligible"], 3)
        self.assertEqual(status["targets"]["all"]["eligible"], 7)
        self.assertEqual(status["snaps"]["matched"], 1)
        self.assertEqual(status["snaps"]["eligible"], 1)
        self.assertEqual(status["snaps"]["all"]["eligible"], 2)
        self.assertEqual(status["props"]["matched"], 4)
        self.assertEqual(status["props"]["eligible"], 4)
        self.assertEqual(status["dst"]["matched"], 1)
        self.assertEqual(status["dst"]["eligible"], 1)
        names = {r["name"] for r in status["depth"]["missing"]}
        self.assertNotIn("FillerWR", names)
        self.assertNotIn("BackupQB", names)
        self.assertNotIn("OldFloorWR", names)
        text = format_slate_status(status)
        self.assertTrue(text.startswith("slate status\nrelevant  5 / 11"))
        self.assertIn("QB 1  RB 1  WR 1  TE 1  D 1", text)
        self.assertIn("all pool  depth 7/9 skill", text)
        self.assertNotIn("FillerWR", text)
        self.assertNotIn("OldFloorWR", text)

    def test_def_skips_depth_targets_snaps(self):
        pool = [
            _pl("D", 4000, name="Jets", team="NYJ", implied_opp=18.0),
            _pl("DEF", 3900, name="Bills", team="BUF"),
        ]
        status = build_slate_status(
            raw_n=2,
            after_ir_n=2,
            after_inj_n=2,
            pool=pool,
            depth={"skipped": False, "source": "ourlads"},
            targets={"skipped": False},
            snaps={"skipped": False},
            props={"skipped": False},
        )
        self.assertEqual(status["depth"]["eligible"], 0)
        self.assertEqual(status["targets"]["eligible"], 0)
        self.assertEqual(status["snaps"]["eligible"], 0)
        self.assertEqual(status["props"]["eligible"], 0)
        self.assertEqual(status["dst"]["eligible"], 2)
        self.assertEqual(status["dst"]["matched"], 1)
        self.assertEqual(status["dst"]["missing"][0]["name"], "Bills")
        self.assertEqual(status["dst"]["missing"][0]["pos"], "D")
        text = format_slate_status(status)
        self.assertIn("dst  1 / 2 relevant DEF implied opp", text)
        self.assertIn("missing dst (1):", text)
        self.assertIn("Bills  D  BUF  $3,900", text)
        self.assertNotIn("missing depth", text)
        self.assertNotIn("missing targets", text)

    def test_missing_relevant_capped_on_stderr(self):
        pool = [
            _pl("WR", 5100 + i, name=f"Miss{i}", team="NE") for i in range(8)
        ]
        status = build_slate_status(
            raw_n=8,
            after_ir_n=8,
            after_inj_n=8,
            pool=pool,
            depth={"skipped": False, "source": "ourlads"},
            targets={"skipped": False},
            snaps={"skipped": False},
            props={"skipped": False},
        )
        self.assertEqual(len(status["depth"]["missing"]), 8)
        self.assertEqual(status["depth"]["missing"][0]["name"], "Miss7")
        text = format_slate_status(status)
        self.assertIn("missing depth (8):", text)
        self.assertIn("+2 more", text)
        self.assertIn("Miss7  WR  NE  $5,107", text)
        self.assertNotIn("Miss0  WR", text)

    def test_skipped_and_key_gaps(self):
        status = build_slate_status(
            raw_n=5,
            after_ir_n=5,
            after_inj_n=5,
            pool=[_pl("WR", 7200, name="AJ Brown", team="PHI")],
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
        self.assertEqual(status["relevant"]["total"], 1)
        self.assertEqual(status["depth"]["missing"], [])
        text = format_slate_status(status)
        self.assertIn("relevant  1 / 1  QB 0  RB 0  WR 1  TE 0  D 0", text)
        self.assertIn("depth  ourlads  skipped", text)
        self.assertIn("targets  skipped (TARGETS_CSV)", text)
        self.assertIn("snaps  skipped", text)
        self.assertIn("props  skipped (no Odds key)", text)
        self.assertIn("injuries  skipped", text)
        self.assertIn("targets TARGETS_CSV", text)
        self.assertIn("props skipped (no Odds key)", "\n".join(status["gaps"]))
        self.assertNotIn("missing depth", text)
        self.assertNotIn("missing targets", text)


if __name__ == "__main__":
    unittest.main()
