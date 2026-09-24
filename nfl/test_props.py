"""Gangstash prop parse, cache, and slate join. No live HTTP."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from nfl.gangstash import (
    CACHE_DIR,
    GangstashKeyMissing,
    GangstashTruncated,
    fetch_props,
)
from nfl.http import HttpError
from nfl.players import Player
from nfl.props import attach_props, ingest_slate_props, prop_field, rows_to_props


class PropFieldTest(unittest.TestCase):
    def test_known_aliases(self):
        self.assertEqual(prop_field("Passing Yards"), "pass_yds")
        self.assertEqual(prop_field("player_pass_tds"), "pass_tds")
        self.assertEqual(prop_field("Rushing Yards Over/Under"), "rush_yds")
        self.assertEqual(prop_field("Receiving Yards"), "rec_yds")
        self.assertEqual(prop_field("Receptions"), "receptions")

    def test_recs_is_receptions(self):
        self.assertEqual(prop_field("Recs"), "receptions")

    def test_unscored_markets_stay_unmapped(self):
        for raw in (
            "Anytime Touchdown",
            "Interceptions",
            "Pass Completions",
            "Rush Attempts",
            "INTs",
            "Pass ATTs",
            "Pass CMPs",
            "Rush ATTs",
            "Rsh + Rec",
        ):
            self.assertIsNone(prop_field(raw), raw)


class RowsTest(unittest.TestCase):
    def test_aggregates_and_keeps_newer_line(self):
        props, meta = rows_to_props(
            [
                {
                    "player_name": "Jared Goff",
                    "prop": "Passing Yards",
                    "line": 240.5,
                    "scraped_at": "2026-09-24T12:00:00Z",
                },
                {
                    "player_name": "Jared Goff",
                    "prop": "Passing Yards",
                    "line": 255.5,
                    "scraped_at": "2026-09-24T18:00:00Z",
                },
                {
                    "player_name": "Jared Goff",
                    "prop": "Passing TDs",
                    "line": 1.5,
                    "scraped_at": "2026-09-24T18:00:00Z",
                },
                {
                    "player_name": "Amon-Ra St. Brown",
                    "prop": "Anytime Touchdown",
                    "line": 0.5,
                    "scraped_at": "2026-09-24T18:00:00Z",
                },
            ]
        )
        goff = props["jared goff"]
        self.assertAlmostEqual(goff.pass_yds or 0, 255.5)
        self.assertAlmostEqual(goff.pass_tds or 0, 1.5)
        self.assertEqual(goff.book, "gangstash")
        self.assertEqual(meta["unmapped_props"], {"Anytime Touchdown": 1})
        self.assertNotIn("amon ra st brown", props)

    def test_same_timestamp_conflict_keeps_first(self):
        props, meta = rows_to_props(
            [
                {
                    "player_name": "Jared Goff",
                    "prop": "Pass Yds",
                    "line": 250,
                    "scraped_at": "2026-09-24T18:00:00Z",
                },
                {
                    "player_name": "Jared Goff",
                    "prop": "Passing Yards",
                    "line": 260,
                    "scraped_at": "2026-09-24T18:00:00Z",
                },
            ]
        )
        self.assertAlmostEqual(props["jared goff"].pass_yds or 0, 250)
        self.assertEqual(len(meta["conflicts"]), 1)
        self.assertEqual(meta["conflicts"][0]["dropped"], 260)


class FetchTest(unittest.TestCase):
    def test_cache_hit_does_not_need_a_key(self):
        day = date(2026, 9, 24)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / day.isoformat() / "props.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "data": [
                            {
                                "player_name": "Jared Goff",
                                "prop": "Passing Yards",
                                "line": 250.5,
                            }
                        ],
                        "truncated": False,
                    }
                ),
                encoding="utf-8",
            )
            with patch("nfl.gangstash.CACHE_DIR", root), patch(
                "nfl.gangstash.http_json"
            ) as http:
                rows, meta = fetch_props(cache_day=day)
            http.assert_not_called()
            self.assertEqual(rows[0]["player_name"], "Jared Goff")
            self.assertFalse(meta["cache_stale"])
            self.assertFalse(meta["live"])

    def test_missing_key_and_cache_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("nfl.gangstash.CACHE_DIR", Path(tmp)), patch(
                "nfl.gangstash.envmod.get", return_value=None
            ):
                with self.assertRaises(GangstashKeyMissing):
                    fetch_props(cache_day=date(2026, 9, 24))

    def test_unreachable_falls_back_to_older_cache(self):
        day = date(2026, 9, 24)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "2026-09-20" / "props.json"
            old.parent.mkdir(parents=True)
            old.write_text(
                json.dumps(
                    {
                        "data": [
                            {
                                "player_name": "Jared Goff",
                                "prop": "Receptions",
                                "line": 1.5,
                            }
                        ],
                        "truncated": False,
                    }
                ),
                encoding="utf-8",
            )
            with patch("nfl.gangstash.CACHE_DIR", root), patch(
                "nfl.gangstash.envmod.get", return_value="secret-key"
            ), patch(
                "nfl.gangstash.http_json",
                side_effect=HttpError("unreachable: down"),
            ):
                rows, meta = fetch_props(cache_day=day)
            self.assertEqual(rows[0]["prop"], "Receptions")
            self.assertTrue(meta["cache_stale"])
            self.assertIn("2026-09-20", meta["cache"])

    def test_truncated_unfiltered_is_not_cached(self):
        day = date(2026, 9, 24)
        calls: list[tuple[str, dict | None]] = []

        def fake_http(url, headers=None, timeout=30):
            calls.append((url, headers))
            return {"data": [{"player_name": "A", "prop": "Receptions", "line": 1}], "truncated": True}, {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("nfl.gangstash.CACHE_DIR", root), patch(
                "nfl.gangstash.envmod.get", return_value="secret-key"
            ), patch("nfl.gangstash.http_json", side_effect=fake_http):
                with self.assertRaises(GangstashTruncated):
                    fetch_props(cache_day=day, refresh=True)
            self.assertFalse((root / day.isoformat() / "props.json").exists())
        self.assertTrue(calls)
        url, headers = calls[0]
        self.assertNotIn("secret-key", url)
        self.assertEqual((headers or {}).get("x-api-key"), "secret-key")
        self.assertTrue(url.startswith("https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/props"))


class IngestTest(unittest.TestCase):
    def test_joins_unique_name_and_marks_ambiguous(self):
        payload = {
            "data": [
                {"player_name": "Jared Goff", "prop": "Passing Yards", "line": 250.5, "scraped_at": "t"},
                {"player_name": "Chris Smith", "prop": "Rushing Yards", "line": 40, "scraped_at": "t"},
                {"player_name": "Nobody", "prop": "Interceptions", "line": 0.5, "scraped_at": "t"},
            ],
            "truncated": False,
        }
        pool = [
            Player(
                pid="goff",
                name="Jared Goff",
                position="QB",
                salary=8000,
                team="DET",
                opponent="CHI",
                game="DET@CHI",
                fppg=None,
                injury="",
                roster_position="",
                implied_total=24.0,
                depth_rank=1,
            ),
            Player(
                pid="s1",
                name="Chris Smith",
                position="RB",
                salary=6000,
                team="DET",
                opponent="CHI",
                game="DET@CHI",
                fppg=None,
                injury="",
                roster_position="",
                implied_total=24.0,
                depth_rank=1,
            ),
            Player(
                pid="s2",
                name="Chris Smith",
                position="WR",
                salary=5000,
                team="CHI",
                opponent="DET",
                game="DET@CHI",
                fppg=None,
                injury="",
                roster_position="",
                implied_total=21.0,
                depth_rank=1,
            ),
        ]
        with patch(
            "nfl.props.fetch_props",
            return_value=(
                payload["data"],
                {"cache": None, "cache_stale": False, "truncated": False, "live": True},
            ),
        ):
            by_pid, stats = ingest_slate_props(pool, slate_day=date(2026, 9, 24))
        self.assertIn("goff", by_pid)
        self.assertAlmostEqual(by_pid["goff"].pass_yds or 0, 250.5)
        self.assertNotIn("s1", by_pid)
        self.assertNotIn("s2", by_pid)
        self.assertEqual(stats["unmapped_props"], {"Interceptions": 1})
        attached = attach_props(pool, by_pid, unmatched=stats["unmatched"])
        by_id = {p.pid: p for p in attached}
        self.assertEqual(by_id["goff"].prop_status, "props")
        self.assertEqual(by_id["goff"].prop_book, "gangstash")
        self.assertIsNotNone(by_id["goff"].prop_fd)
        self.assertEqual(by_id["s1"].prop_status, "unmatched")
        self.assertEqual(by_id["s2"].prop_status, "unmatched")
        self.assertIsNone(by_id["s1"].prop_fd)


class CacheDirTest(unittest.TestCase):
    def test_default_cache_is_under_nfl_data(self):
        self.assertEqual(CACHE_DIR.name, "gangstash-props")
        self.assertEqual(CACHE_DIR.parent.name, "data")


if __name__ == "__main__":
    unittest.main()
