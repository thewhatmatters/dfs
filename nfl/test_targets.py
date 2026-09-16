"""Parser tests for Lineups WR/TE targets (no network)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nfl.targets import (
    TargetsError,
    extract_ssr_payload,
    rows_from_payload,
    write_targets_csv,
)

FIXTURE = Path(__file__).resolve().parent / "testdata" / "lineups_targets_ssr.html"


class TestTargetsParse(unittest.TestCase):
    def test_extract_and_rows(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8")
        payload = extract_ssr_payload(html)
        self.assertEqual(payload["metric"], "targets")
        rows = rows_from_payload(payload, asof="2026-09-16")
        # WR week1 + TE week1 + TE week2
        self.assertEqual(len(rows), 3)
        wr = next(r for r in rows if r.player == "A.J. Brown")
        self.assertEqual(wr.team, "NE")
        self.assertEqual(wr.position, "WR")
        self.assertEqual(wr.week, 1)
        self.assertEqual(wr.targets, 4)
        self.assertAlmostEqual(wr.target_share, 0.12)
        te = [r for r in rows if r.player == "Sam LaPorta"]
        self.assertEqual({r.week for r in te}, {1, 2})
        self.assertEqual(te[0].team, "DET")

    def test_expect_pos_filter(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8")
        payload = extract_ssr_payload(html)
        wr_only = rows_from_payload(payload, asof="2026-09-16", expect_pos="WR")
        self.assertEqual(len(wr_only), 1)
        self.assertEqual(wr_only[0].position, "WR")

    def test_unmapped_team(self) -> None:
        payload = {
            "metric": "targets",
            "data": {
                "rows": [
                    {
                        "name": "Ghost",
                        "team": "Atlantis Atlanteans",
                        "position": "WR",
                        "weeks": [1],
                        "weeksPct": [10],
                        "total": 1,
                        "average": 1,
                    }
                ]
            },
        }
        with self.assertRaises(TargetsError) as ctx:
            rows_from_payload(payload, asof="2026-09-16")
        self.assertEqual(ctx.exception.choke, "TARGETS_JOIN")

    def test_write_csv(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8")
        rows = rows_from_payload(extract_ssr_payload(html), asof="2026-09-16")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "targets.csv"
            write_targets_csv(rows, path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("player,team,position,week,targets,target_share", text)
            self.assertIn("A.J. Brown,NE,WR,1,4,0.12", text)


if __name__ == "__main__":
    unittest.main()
