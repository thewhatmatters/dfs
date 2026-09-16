"""Preflight CSV Team/Opponent join vs ncaaf.teams.TEAMS."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "preflight.py"
SLATE_134050 = ROOT / "ncaaf" / "data" / (
    "FanDuel-CFB-2026 CDT-09 CDT-12 CDT-134050-players-list.csv"
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PREFLIGHT), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


class PreflightJoinTest(unittest.TestCase):
    def test_unmapped_opponent_chokes_lines_join(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.csv"
            path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent\n"
                "1,QB,Test,5000,OSU,ZZZZ\n",
                encoding="utf-8-sig",
            )
            proc = _run("--csv", str(path))
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(payload["overall"], "down")
        self.assertEqual(payload["choke"], "LINES_JOIN")
        self.assertEqual(payload["checks"]["join"]["gate"], "LINES_JOIN")
        self.assertIn("ZZZZ", payload["checks"]["join"]["detail"])
        self.assertIn("ncaaf/teams.py", payload["checks"]["join"]["detail"])
        self.assertIn("choke LINES_JOIN", proc.stderr)

    def test_slate_134050_join_ready(self):
        self.assertTrue(SLATE_134050.is_file(), SLATE_134050)
        proc = _run("--csv", str(SLATE_134050))
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["checks"]["join"]["status"], "ready")
        self.assertNotEqual(payload["choke"], "LINES_JOIN")
        self.assertNotEqual(payload["overall"], "down")
        self.assertNotEqual(proc.returncode, 1)

    def test_no_csv_skips_join(self):
        proc = _run()
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["checks"]["join"]["status"], "ready")
        self.assertIsNone(payload["checks"]["join"]["gate"])
        self.assertIn("join skipped", payload["checks"]["join"]["detail"])
        self.assertNotEqual(payload["overall"], "down")


if __name__ == "__main__":
    unittest.main()
