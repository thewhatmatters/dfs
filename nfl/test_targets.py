"""Parser + join tests for Lineups RB/WR/TE targets (no network)."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from nfl.names import match_key
from nfl.players import Player
from nfl.projections import week1_score
from nfl.targets import (
    TargetsError,
    attach_targets,
    extract_ssr_payload,
    latest_week,
    load_targets_csv,
    print_targets_gaps,
    rows_from_payload,
    write_targets_csv,
)

FIXTURE = Path(__file__).resolve().parent / "testdata" / "lineups_targets_ssr.html"
JOIN_CSV = Path(__file__).resolve().parent / "testdata" / "targets_join.csv"


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("pid", kw.get("name", "p")),
        name=kw.get("name", "Cam"),
        position="WR",
        salary=5000,
        team="DET",
        opponent="NO",
        game="NO@DET",
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


class TestTargetsParse(unittest.TestCase):
    @unittest.skipUnless(FIXTURE.is_file(), "SSR HTML fixture gitignored (*.html)")
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

    @unittest.skipUnless(FIXTURE.is_file(), "SSR HTML fixture gitignored (*.html)")
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

    @unittest.skipUnless(FIXTURE.is_file(), "SSR HTML fixture gitignored (*.html)")
    def test_write_csv(self) -> None:
        html = FIXTURE.read_text(encoding="utf-8")
        rows = rows_from_payload(extract_ssr_payload(html), asof="2026-09-16")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "targets.csv"
            write_targets_csv(rows, path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("player,team,position,week,targets,target_share", text)
            self.assertIn("A.J. Brown,NE,WR,1,4,0.12", text)


class TestTargetsJoin(unittest.TestCase):
    def test_join_jr_strip_and_gaps(self) -> None:
        rows = load_targets_csv(JOIN_CSV)
        self.assertEqual(latest_week(rows), 2)
        pool = [
            _pl(name="AJ Brown", team="PHI", position="WR", pid="aj"),
            _pl(name="DeVonta Smith", team="PHI", position="WR", pid="ds"),
            _pl(name="Sam LaPorta Jr.", team="DET", position="TE", pid="sl"),
            _pl(name="Marvin Harrison", team="ARI", position="WR", pid="mh"),
            _pl(name="Saquon Barkley", team="PHI", position="RB", pid="sb"),
            _pl(name="Jared Goff", team="DET", position="QB", pid="jg"),
        ]
        attached, stats = attach_targets(pool, rows, week=1)
        self.assertEqual(stats["week"], 1)
        self.assertEqual(stats["joined"], 4)
        self.assertEqual(stats["slate_rb_wr_te"], 5)
        lu_names = {r["player"] for r in stats["unmatched_lineups"]}
        sl_names = {r["player"] for r in stats["unmatched_slate_wr_te"]}
        self.assertEqual(lu_names, {"Hollywood Brown"})
        self.assertEqual(sl_names, {"DeVonta Smith"})
        by_name = {p.name: p for p in attached}
        self.assertEqual(by_name["AJ Brown"].targets_status, "joined")
        self.assertAlmostEqual(by_name["AJ Brown"].target_share or 0, 0.24)
        self.assertEqual(by_name["Sam LaPorta Jr."].targets_status, "joined")
        self.assertEqual(by_name["Marvin Harrison"].targets_status, "joined")
        self.assertEqual(by_name["Saquon Barkley"].targets_status, "joined")
        self.assertAlmostEqual(by_name["Saquon Barkley"].target_share or 0, 0.12)
        self.assertEqual(by_name["DeVonta Smith"].targets_status, "unmatched")
        self.assertIsNone(by_name["DeVonta Smith"].target_share)
        self.assertIsNone(by_name["Jared Goff"].targets_status)
        # A.J. Brown 0.24 vs WR1 expected 0.24 → factor 1.0 (Vegas base unchanged)
        self.assertAlmostEqual(
            by_name["AJ Brown"].objective or 0.0,
            week1_score(30.0, 1, "WR"),
            places=6,
        )
        # Harrison 0.08 vs WR1 0.24 → floor 0.80
        self.assertAlmostEqual(
            by_name["Marvin Harrison"].objective or 0.0,
            week1_score(30.0, 1, "WR") * 0.80,
            places=6,
        )
        buf = io.StringIO()
        with redirect_stderr(buf):
            print_targets_gaps(stats)
        text = buf.getvalue()
        self.assertIn("joined 4 / 5 slate RB/WR/TE", text)
        self.assertIn("Hollywood Brown (PHI WR)", text)
        self.assertIn("DeVonta Smith (PHI WR)", text)
        self.assertNotIn("Jared Goff", text)

    def test_rb_target_join_even_share_is_neutral(self) -> None:
        rows = load_targets_csv(JOIN_CSV)
        pool = [_pl(name="Saquon Barkley", team="PHI", position="RB", pid="sb")]
        attached, stats = attach_targets(pool, rows, week=1)
        self.assertEqual(stats["joined"], 1)
        pl = attached[0]
        self.assertEqual(pl.targets_status, "joined")
        self.assertAlmostEqual(pl.target_share or 0, 0.12)
        # RB1 expected target 0.12 → factor 1.0 (Vegas base unchanged)
        self.assertAlmostEqual(
            pl.objective or 0.0,
            week1_score(30.0, 1, "RB"),
            places=6,
        )

    def test_missing_week_is_unmatched_not_fatal(self) -> None:
        rows = load_targets_csv(JOIN_CSV)
        pool = [
            _pl(name="AJ Brown", team="PHI", position="WR", pid="aj"),
            _pl(name="Sam LaPorta Jr.", team="DET", position="TE", pid="sl"),
        ]
        attached, stats = attach_targets(pool, rows, week=99)
        self.assertEqual(stats["week"], 99)
        self.assertEqual(stats["joined"], 0)
        self.assertEqual(stats["week_rows"], 0)
        self.assertEqual(len(stats["unmatched_lineups"]), 0)
        sl_names = {r["player"] for r in stats["unmatched_slate_wr_te"]}
        self.assertEqual(sl_names, {"AJ Brown", "Sam LaPorta Jr."})
        for pl in attached:
            self.assertEqual(pl.targets_status, "unmatched")
            self.assertAlmostEqual(
                pl.objective or 0.0,
                week1_score(30.0, 1, pl.position),
                places=6,
            )

    def test_latest_week_does_not_join_week1_only_names(self) -> None:
        rows = load_targets_csv(JOIN_CSV)
        pool = [
            _pl(name="AJ Brown", team="PHI", position="WR", pid="aj"),
            _pl(name="Amon-Ra St. Brown", team="DET", position="WR", pid="ar"),
        ]
        attached, stats = attach_targets(pool, rows)  # latest = 2
        self.assertEqual(stats["week"], 2)
        self.assertEqual(stats["joined"], 0)
        lu_names = {r["player"] for r in stats["unmatched_lineups"]}
        self.assertEqual(lu_names, {"Ghost Receiver"})
        self.assertEqual(attached[0].targets_status, "unmatched")
        sl_names = {r["player"] for r in stats["unmatched_slate_wr_te"]}
        self.assertEqual(sl_names, {"AJ Brown", "Amon-Ra St. Brown"})

    def test_missing_csv_chokes(self) -> None:
        with self.assertRaises(TargetsError) as ctx:
            load_targets_csv(Path("/tmp/dfs-missing-targets.csv"))
        self.assertEqual(ctx.exception.choke, "TARGETS_CSV")


class TestWalkerAbsentFromLineups(unittest.TestCase):
    """Devontez Walker is not in the Lineups week-1 BAL WR feed.

    FanDuel lists him (BAL WR, Q groin). Lineups week 1 BAL WRs are
    Flowers, Bateman, Lane, Wester, Chris Moore. Jahdae Walker is CHI —
    do not invent an alias. He stays unmatched / usage 1.0.
    """

    def test_week1_bal_wrs_omit_devontez_walker(self) -> None:
        path = Path(__file__).resolve().parent / "data" / "targets.csv"
        self.assertTrue(path.is_file(), path)
        rows = [r for r in load_targets_csv(path) if r.week == 1 and r.team == "BAL"]
        wr_keys = {match_key(r.player) for r in rows if r.position == "WR"}
        self.assertEqual(
            wr_keys,
            {
                match_key("Zay Flowers"),
                match_key("Rashod Bateman"),
                match_key("Ja'Kobi Lane"),
                match_key("LaJohntay Wester"),
                match_key("Chris Moore"),
            },
        )
        self.assertNotIn(match_key("Devontez Walker"), wr_keys)
        self.assertNotIn(match_key("Devontez Walker"), {match_key(r.player) for r in rows})


if __name__ == "__main__":
    unittest.main()
