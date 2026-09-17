"""Parser + join tests for Lineups RB/WR/TE snaps (no network)."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from nfl.players import Player
from nfl.projections import USAGE_FACTOR_HI, USAGE_FACTOR_LO, week1_score
from nfl.snaps import (
    SnapsError,
    attach_snaps,
    extract_snaps_ssr,
    latest_week,
    load_snaps_csv,
    print_snaps_gaps,
    rows_from_payload,
    write_snaps_csv,
)

JOIN_CSV = Path(__file__).resolve().parent / "testdata" / "snaps_join.csv"

SSR_HTML = """<!doctype html><html><body>
<script type=application/json class=sc-sports-nfl-metrics>
{"metric":"snaps","data":{"rows":[
  {"name":"Saquon Barkley","team":"Philadelphia Eagles","position":"RB",
   "weeks":[45,null],"weeksPct":[65,null],"total":45,"average":45,"teamSnapPct":65},
  {"name":"A.J. Brown","team":"New England Patriots","position":"WR",
   "weeks":[50],"weeksPct":[80],"total":50,"average":50,"teamSnapPct":80},
  {"name":"Sam LaPorta","team":"Detroit Lions","position":"TE",
   "weeks":[30,20],"weeksPct":[70,50],"total":50,"average":25,"teamSnapPct":60}
]}}
</script></body></html>
"""


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("pid", kw.get("name", "p")),
        name=kw.get("name", "Cam"),
        position="RB",
        salary=8000,
        team="PHI",
        opponent="DAL",
        game="DAL@PHI",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=30.0,
        implied_opp=22.0,
        depth_rank=1,
    )
    fields.update(kw)
    if fields.get("objective") is None:
        fields["objective"] = week1_score(
            fields.get("implied_total") or 0.0,
            depth_rank=fields.get("depth_rank"),
            position=fields["position"],
            target_share=fields.get("target_share"),
            snap_share=fields.get("snap_share"),
        )
    allowed = Player.__dataclass_fields__
    return Player(**{k: v for k, v in fields.items() if k in allowed})


class TestSnapsParse(unittest.TestCase):
    def test_extract_and_rows(self) -> None:
        payload = extract_snaps_ssr(SSR_HTML)
        self.assertEqual(payload["metric"], "snaps")
        rows = rows_from_payload(payload, asof="2026-09-17")
        self.assertEqual(len(rows), 4)
        rb = next(r for r in rows if r.player == "Saquon Barkley")
        self.assertEqual(rb.team, "PHI")
        self.assertEqual(rb.position, "RB")
        self.assertEqual(rb.week, 1)
        self.assertEqual(rb.snaps, 45)
        self.assertAlmostEqual(rb.snap_share, 0.65)
        self.assertAlmostEqual(rb.team_snap_pct or 0, 0.65)
        wr = next(r for r in rows if r.player == "A.J. Brown")
        self.assertEqual(wr.team, "NE")
        te = [r for r in rows if r.player == "Sam LaPorta"]
        self.assertEqual({r.week for r in te}, {1, 2})

    def test_expect_pos_filter(self) -> None:
        payload = extract_snaps_ssr(SSR_HTML)
        rb_only = rows_from_payload(payload, asof="2026-09-17", expect_pos="RB")
        self.assertEqual(len(rb_only), 1)
        self.assertEqual(rb_only[0].position, "RB")

    def test_unmapped_team(self) -> None:
        payload = {
            "metric": "snaps",
            "data": {
                "rows": [
                    {
                        "name": "Ghost",
                        "team": "Atlantis Atlanteans",
                        "position": "RB",
                        "weeks": [10],
                        "weeksPct": [20],
                        "total": 10,
                        "average": 10,
                        "teamSnapPct": 20,
                    }
                ]
            },
        }
        with self.assertRaises(SnapsError) as ctx:
            rows_from_payload(payload, asof="2026-09-17")
        self.assertEqual(ctx.exception.choke, "SNAPS_JOIN")

    def test_write_csv(self) -> None:
        rows = rows_from_payload(extract_snaps_ssr(SSR_HTML), asof="2026-09-17")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snaps.csv"
            write_snaps_csv(rows, path)
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "player,team,position,week,snaps,snap_share",
                text,
            )
            self.assertIn("Saquon Barkley,PHI,RB,1,45,0.65", text)


class TestSnapsJoin(unittest.TestCase):
    def test_join_jr_strip_and_gaps(self) -> None:
        rows = load_snaps_csv(JOIN_CSV)
        self.assertEqual(latest_week(rows), 2)
        pool = [
            _pl(name="Saquon Barkley", team="PHI", position="RB", pid="sb"),
            _pl(name="AJ Brown", team="PHI", position="WR", pid="aj"),
            _pl(name="Sam LaPorta Jr.", team="DET", position="TE", pid="sl"),
            _pl(name="Jahmyr Gibbs", team="DET", position="RB", pid="jg"),
            _pl(name="Jared Goff", team="DET", position="QB", pid="go"),
        ]
        attached, stats = attach_snaps(pool, rows, week=1)
        self.assertEqual(stats["week"], 1)
        self.assertEqual(stats["joined"], 3)
        self.assertEqual(stats["slate_rb_wr_te"], 4)
        lu_names = {r["player"] for r in stats["unmatched_lineups"]}
        sl_names = {r["player"] for r in stats["unmatched_slate_rb_wr_te"]}
        self.assertEqual(lu_names, {"AJ Dillon"})
        self.assertEqual(sl_names, {"Jahmyr Gibbs"})
        by_name = {p.name: p for p in attached}
        self.assertEqual(by_name["Saquon Barkley"].snaps_status, "joined")
        self.assertAlmostEqual(by_name["Saquon Barkley"].snap_share or 0, 0.65)
        self.assertEqual(by_name["AJ Brown"].snaps_status, "joined")
        self.assertEqual(by_name["Sam LaPorta Jr."].snaps_status, "joined")
        self.assertEqual(by_name["Jahmyr Gibbs"].snaps_status, "unmatched")
        self.assertIsNone(by_name["Jahmyr Gibbs"].snap_share)
        self.assertIsNone(by_name["Jared Goff"].snaps_status)
        # RB1 0.65 vs expected 0.65 → factor 1.0
        self.assertAlmostEqual(
            by_name["Saquon Barkley"].objective or 0.0,
            week1_score(30.0, 1, "RB"),
            places=6,
        )
        # WR snap_share stored but not applied (targets-only usage)
        wr_base = week1_score(30.0, 1, "WR")
        self.assertAlmostEqual(by_name["AJ Brown"].objective or 0.0, wr_base, places=6)
        buf = io.StringIO()
        with redirect_stderr(buf):
            print_snaps_gaps(stats)
        text = buf.getvalue()
        self.assertIn("joined 3 / 4 slate RB/WR/TE", text)
        self.assertIn("AJ Dillon (PHI RB)", text)
        self.assertIn("Jahmyr Gibbs (DET RB)", text)
        self.assertNotIn("Jared Goff", text)

    def test_rb_low_snaps_floor(self) -> None:
        rows = load_snaps_csv(JOIN_CSV)
        pool = [_pl(name="AJ Dillon", team="PHI", position="RB", pid="ad")]
        attached, _stats = attach_snaps(pool, rows, week=1)
        # 0.18 / 0.65 → floor 0.80
        self.assertAlmostEqual(
            attached[0].objective or 0.0,
            week1_score(30.0, 1, "RB") * USAGE_FACTOR_LO,
            places=6,
        )

    def test_missing_week_is_unmatched_not_fatal(self) -> None:
        rows = load_snaps_csv(JOIN_CSV)
        pool = [_pl(name="Saquon Barkley", team="PHI", position="RB", pid="sb")]
        attached, stats = attach_snaps(pool, rows, week=99)
        self.assertEqual(stats["joined"], 0)
        self.assertEqual(attached[0].snaps_status, "unmatched")
        self.assertAlmostEqual(
            attached[0].objective or 0.0,
            week1_score(30.0, 1, "RB"),
            places=6,
        )

    def test_missing_csv_chokes(self) -> None:
        with self.assertRaises(SnapsError) as ctx:
            load_snaps_csv(Path("/tmp/dfs-missing-snaps.csv"))
        self.assertEqual(ctx.exception.choke, "SNAPS_CSV")


class TestRbUsageBlend(unittest.TestCase):
    def test_snap_only_and_blend_and_wr_ignores_snaps(self) -> None:
        base = week1_score(30.0, 1, "RB")
        even = week1_score(30.0, 1, "RB", snap_share=0.65)
        self.assertAlmostEqual(even, base)
        hi = week1_score(30.0, 1, "RB", snap_share=0.99)
        lo = week1_score(30.0, 1, "RB", snap_share=0.01)
        self.assertAlmostEqual(hi, base * USAGE_FACTOR_HI, places=6)
        self.assertAlmostEqual(lo, base * USAGE_FACTOR_LO, places=6)
        # Blend 70/30: snap 1.20 * 0.70 + target 0.80 * 0.30 = 1.08
        blended = week1_score(
            30.0, 1, "RB", target_share=0.01, snap_share=0.99
        )
        self.assertAlmostEqual(blended, base * 1.08, places=6)
        wr = week1_score(30.0, 1, "WR", snap_share=0.99)
        self.assertAlmostEqual(wr, week1_score(30.0, 1, "WR"), places=6)
        qb = week1_score(30.0, 1, "QB", snap_share=0.99, target_share=0.50)
        self.assertAlmostEqual(qb, 30.0 * 1.0 * 0.50)


if __name__ == "__main__":
    unittest.main()
