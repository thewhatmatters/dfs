"""Gangstash /data readers and source switches. No network."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path
from unittest.mock import patch

from nfl.depth import GangstashDepthError, ingest_slate_depth
from nfl.gangstash import (
    GangstashDataError,
    GangstashDataKeyMissing,
    GangstashTruncated,
    dataset_cache_file,
    fetch_dataset,
)
from nfl.http import HttpAuthError, HttpError
from nfl.gangstash_data import (
    aggregate_target_window,
    fetch_game_lines,
    fetch_targets,
    fetch_team_stats,
    fetch_team_stats_weekly,
    parse_game_line,
)
from nfl.http import HttpError
from nfl.lines import ingest_slate_lines
from nfl.optimize import parse_args
from nfl.players import Player
from nfl.projections import week1_score
from nfl.targets import attach_targets, load_optimizer_targets


def _env_get(name: str) -> str | None:
    if name == "GANGSTASH_API_KEY":
        return "secret-key"
    return None


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("pid", kw.get("name", "p")),
        name=kw.get("name", "A.J. Brown"),
        position=kw.get("position", "WR"),
        salary=8000,
        team=kw.get("team", "PHI"),
        opponent=kw.get("opponent", "DAL"),
        game=kw.get("game", "DAL@PHI"),
        fppg=None,
        injury="",
        roster_position="",
        implied_total=30.0,
        depth_rank=1,
    )
    fields.update(kw)
    allowed = set(Player.__dataclass_fields__)
    return Player(**{k: v for k, v in fields.items() if k in allowed})


class FetchDatasetTest(unittest.TestCase):
    def test_cache_hit_skips_network_and_key(self) -> None:
        day = date(2026, 9, 24)
        params = {"season": "2026", "week": "1,2"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = dataset_cache_file(root, day, "targets", params)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"data": [{"player_name": "A.J. Brown"}], "truncated": False}),
                encoding="utf-8",
            )
            with patch("nfl.gangstash.http_json") as http:
                rows, meta = fetch_dataset(
                    "targets", params, cache_day=day, cache_root=root
                )
            http.assert_not_called()
            self.assertEqual(rows[0]["player_name"], "A.J. Brown")
            self.assertFalse(meta["cache_stale"])
            self.assertFalse(meta["live"])

    def test_missing_key_and_cache_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("nfl.gangstash.DATA_CACHE_DIR", Path(tmp)), patch(
                "nfl.gangstash.envmod.get", return_value=None
            ):
                with self.assertRaises(GangstashDataKeyMissing):
                    fetch_dataset("targets", {"season": "2026"}, cache_day=date(2026, 9, 24))

    def test_unreachable_falls_back_to_older_cache(self) -> None:
        day = date(2026, 9, 24)
        params = {"date": "2026-09-13"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = dataset_cache_file(root, date(2026, 9, 20), "game_lines", params)
            old.parent.mkdir(parents=True)
            old.write_text(
                json.dumps(
                    {"data": [{"home_team_fd": "PHI", "away_team_fd": "DAL"}], "truncated": False}
                ),
                encoding="utf-8",
            )
            with patch("nfl.gangstash.envmod.get", side_effect=_env_get), patch(
                "nfl.gangstash.http_json", side_effect=HttpError("unreachable: down")
            ):
                rows, meta = fetch_dataset(
                    "game_lines", params, cache_day=day, cache_root=root
                )
            self.assertEqual(rows[0]["home_team_fd"], "PHI")
            self.assertTrue(meta["cache_stale"])
            self.assertIn("2026-09-20", meta["cache"])

    def test_truncated_is_not_cached_and_key_stays_in_header(self) -> None:
        day = date(2026, 9, 24)
        calls: list[tuple[str, dict | None]] = []

        def fake_http(url, headers=None, timeout=30):
            calls.append((url, headers))
            return {"data": [{"player_name": "A"}], "truncated": True}, {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("nfl.gangstash.envmod.get", side_effect=_env_get), patch(
                "nfl.gangstash.http_json", side_effect=fake_http
            ):
                with self.assertRaises(GangstashTruncated):
                    fetch_dataset(
                        "targets",
                        {"season": "2026"},
                        cache_day=day,
                        cache_root=root,
                        refresh=True,
                    )
            self.assertEqual(list(root.rglob("*.json")), [])
        url, headers = calls[0]
        self.assertNotIn("secret-key", url)
        self.assertIn("dataset=targets", url)
        self.assertIn("season=2026", url)
        self.assertEqual((headers or {}).get("x-api-key"), "secret-key")
        self.assertTrue(url.startswith("https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/data?"))


class TargetWindowTest(unittest.TestCase):
    def test_window_share_is_sum_over_sum_not_mean_of_shares(self) -> None:
        rows = aggregate_target_window(
            [
                {
                    "player_name": "A.J. Brown",
                    "team_fd": "PHI",
                    "position": "WR",
                    "week": 1,
                    "targets": 8,
                    "target_share": 8 / 30,
                    "team_targets": 30,
                    "team_pass_attempts": 32,
                    "air_yards_share": 0.4,
                    "wopr": 0.55,
                    "receptions": 6,
                    "rec_yards": 80,
                    "gsis_id": "00-0035676",
                    "player_id": "brown",
                },
                {
                    "player_name": "A.J. Brown",
                    "team_fd": "PHI",
                    "position": "WR",
                    "week": 2,
                    "targets": 4,
                    "target_share": 4 / 20,
                    "team_targets": 20,
                    "team_pass_attempts": 28,
                },
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["week"], 2)
        self.assertEqual(rows[0]["targets"], 12)
        self.assertEqual(rows[0]["targets_total"], 12)
        self.assertAlmostEqual(rows[0]["targets_avg"], 6.0)
        self.assertAlmostEqual(rows[0]["target_share"], 12 / 50)
        self.assertEqual(rows[0]["team_fd"], "PHI")
        self.assertEqual(rows[0]["gsis_id"], "00-0035676")

    def test_jax_maps_to_fanduel_jac(self) -> None:
        rows = aggregate_target_window(
            [
                {
                    "player_name": "Brian Thomas Jr.",
                    "team_fd": "JAX",
                    "position": "WR",
                    "week": 1,
                    "targets": 6,
                    "target_share": 0.2,
                    "team_targets": 30,
                }
            ]
        )
        self.assertEqual(rows[0]["team_fd"], "JAC")
        self.assertAlmostEqual(rows[0]["target_share"], 6 / 30)

    def test_single_week_without_team_targets_uses_row_share(self) -> None:
        rows = aggregate_target_window(
            [
                {
                    "player_name": "A.J. Brown",
                    "team_fd": "PHI",
                    "position": "WR",
                    "week": 2,
                    "targets": 9,
                    "target_share": 0.31,
                }
            ]
        )
        self.assertAlmostEqual(rows[0]["target_share"], 0.31)

    def test_multi_week_without_team_targets_errors(self) -> None:
        with self.assertRaises(Exception) as ctx:
            aggregate_target_window(
                [
                    {
                        "player_name": "A.J. Brown",
                        "team_fd": "PHI",
                        "position": "WR",
                        "week": 1,
                        "targets": 8,
                        "target_share": 0.2,
                    },
                    {
                        "player_name": "A.J. Brown",
                        "team_fd": "PHI",
                        "position": "WR",
                        "week": 2,
                        "targets": 4,
                        "target_share": 0.2,
                    },
                ]
            )
        self.assertIn("team_targets", str(ctx.exception))

    def test_null_player_name_rows_are_skipped_and_counted(self) -> None:
        buf = io.StringIO()
        with redirect_stderr(buf):
            rows = aggregate_target_window(
                [
                    {
                        "player_name": "A.J. Brown",
                        "team_fd": "PHI",
                        "position": "WR",
                        "week": 1,
                        "targets": 8,
                        "team_targets": 30,
                        "target_share": 8 / 30,
                    },
                    {
                        "player_name": None,
                        "team_fd": "NYG",
                        "position": "WR",
                        "week": 1,
                        "targets": 4,
                        "team_targets": 30,
                        "gsis_id": None,
                        "player_id": None,
                    },
                    {
                        "team_fd": "DAL",
                        "position": "TE",
                        "week": 1,
                        "targets": 2,
                        "team_targets": 20,
                    },
                ]
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["player_name"], "A.J. Brown")
        self.assertAlmostEqual(rows[0]["target_share"], 8 / 30)
        self.assertIn(
            "gangstash targets skipped 2 rows with null player_name",
            buf.getvalue(),
        )

    def test_empty_targets_response_is_fatal(self) -> None:
        with self.assertRaises(GangstashDataError) as ctx:
            aggregate_target_window([])
        self.assertIn("empty", str(ctx.exception))

    def test_missing_player_name_column_is_fatal(self) -> None:
        with self.assertRaises(GangstashDataError) as ctx:
            aggregate_target_window(
                [
                    {
                        "team_fd": "PHI",
                        "position": "WR",
                        "week": 1,
                        "targets": 8,
                        "team_targets": 30,
                    }
                ]
            )
        self.assertIn("player_name", str(ctx.exception))
        self.assertIn("absent", str(ctx.exception))

    def test_lineups_source_does_not_call_gangstash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.csv"
            path.write_text(
                "player,team,position,week,targets,target_share,targets_avg,targets_total,source,asof\n"
                "A.J. Brown,PHI,WR,1,8,0.22,8,8,lineups,2026-09-16\n",
                encoding="utf-8",
            )
            with patch("nfl.targets.fetch_targets") as fetch:
                rows, meta = load_optimizer_targets(
                    source="lineups",
                    csv_path=path,
                    week=None,
                    weeks=None,
                    season=2026,
                )
            fetch.assert_not_called()
        self.assertEqual(meta["source"], "lineups")
        self.assertEqual(rows[0].source, "lineups")
        self.assertAlmostEqual(rows[0].target_share, 0.22)

    def test_gangstash_source_joins_window_and_leaves_unmatched_empty(self) -> None:
        payload = [
            {
                "player_name": "A.J. Brown",
                "team_fd": "PHI",
                "position": "WR",
                "week": 1,
                "targets": 8,
                "team_targets": 30,
                "target_share": 8 / 30,
            },
            {
                "player_name": "A.J. Brown",
                "team_fd": "PHI",
                "position": "WR",
                "week": 2,
                "targets": 4,
                "team_targets": 20,
                "target_share": 0.2,
            },
            {
                "player_name": "Other Guy",
                "team_fd": "PHI",
                "position": "WR",
                "week": 2,
                "targets": 3,
                "team_targets": 20,
                "target_share": 0.15,
            },
        ]
        with patch(
            "nfl.targets.fetch_targets",
            return_value=(payload, {"cache": "c", "cache_stale": False, "live": True}),
        ) as fetch:
            rows, meta = load_optimizer_targets(
                source="gangstash",
                csv_path=Path("unused.csv"),
                week=None,
                weeks=[1, 2],
                season=2026,
            )
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs["season"], 2026)
        self.assertEqual(fetch.call_args.kwargs["weeks"], [1, 2])
        self.assertEqual(meta["source"], "gangstash")
        pool = [
            _pl(name="A.J. Brown", team="PHI"),
            _pl(name="Devontez Walker", pid="walker", team="BAL", opponent="CLE", game="CLE@BAL"),
        ]
        attached, stats = attach_targets(pool, rows, week=meta["join_week"])
        by_name = {p.name: p for p in attached}
        brown = by_name["A.J. Brown"]
        walker = by_name["Devontez Walker"]
        self.assertAlmostEqual(brown.target_share or 0, 12 / 50)
        self.assertEqual(brown.targets_status, "joined")
        self.assertAlmostEqual(
            brown.objective,
            week1_score(30.0, 1, "WR", target_share=12 / 50),
        )
        self.assertIsNone(walker.target_share)
        self.assertEqual(walker.targets_status, "unmatched")
        self.assertAlmostEqual(
            walker.objective,
            week1_score(30.0, 1, "WR", target_share=None),
        )
        self.assertEqual(stats["joined"], 1)
        self.assertEqual(len(stats["unmatched_slate_rb_wr_te"]), 1)

    def test_targets_request_keeps_week_commas(self) -> None:
        calls: list[str] = []

        def fake_http(url, headers=None, timeout=30):
            calls.append(url)
            return {"data": [], "truncated": False}, {}

        with tempfile.TemporaryDirectory() as tmp, patch(
            "nfl.gangstash.DATA_CACHE_DIR", Path(tmp)
        ), patch("nfl.gangstash.envmod.get", side_effect=_env_get), patch(
            "nfl.gangstash.http_json", side_effect=fake_http
        ):
            fetch_targets(season=2026, weeks=[1, 2], refresh=True, cache_day=date(2026, 9, 24))
        self.assertIn("dataset=targets", calls[0])
        self.assertIn("season=2026", calls[0])
        self.assertIn("week=1,2", calls[0])
        self.assertNotIn("secret-key", calls[0])


class GameLinesTest(unittest.TestCase):
    def test_flat_moneylines_and_home_spread(self) -> None:
        row = parse_game_line(
            {
                "game_id": "2026_03_WAS_JAC",
                "season": 2026,
                "week": 3,
                "home_team_fd": "JAX",
                "away_team_fd": "WSH",
                "spread": -2.5,
                "total": 44,
                "home_moneyline": -130,
                "away_moneyline": 110,
                "commence_time": "2026-09-24T00:15:00Z",
                "updated_at": "2026-09-23T12:00:00Z",
            }
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.home_fd, "JAC")
        self.assertEqual(row.away_fd, "WAS")
        self.assertAlmostEqual(row.spread, -2.5)
        self.assertAlmostEqual(row.home_moneyline or 0, -130)

    def test_gangstash_source_builds_implied_totals_and_skips_odds(self) -> None:
        raw = [
            {
                "home_team_fd": "PHI",
                "away_team_fd": "DAL",
                "game_id": "2026_01_DAL_PHI",
                "season": 2026,
                "week": 1,
                "spread": -3.5,
                "total": 45.5,
                "home_moneyline": -170,
                "away_moneyline": 145,
                "commence_time": "2026-09-13T17:00:00Z",
                "updated_at": "2026-09-12T18:00:00Z",
            }
        ]
        players = [_pl()]
        with patch(
            "nfl.lines.fetch_game_lines",
            return_value=(raw, {"live": True, "cache": None, "cache_stale": False}),
        ) as gs, patch("nfl.lines.fetch_odds") as odds:
            by_team = ingest_slate_lines(
                players, slate_day=date(2026, 9, 13), source="gangstash"
            )
        odds.assert_not_called()
        gs.assert_called_once()
        self.assertEqual(gs.call_args.kwargs["on_date"], date(2026, 9, 13))
        line = by_team["PHI"]
        self.assertEqual(line.source, "gangstash")
        self.assertEqual(line.provider, "bettingpros")
        self.assertAlmostEqual(line.implied_home, (45.5 - (-3.5)) / 2)
        self.assertAlmostEqual(line.implied_away, (45.5 + (-3.5)) / 2)
        self.assertAlmostEqual(line.home_moneyline or 0, -170)
        self.assertEqual(by_team["DAL"].game, "DAL@PHI")

    def test_oddsapi_source_does_not_call_gangstash(self) -> None:
        players = [_pl()]
        sentinel = {"PHI": object()}
        with patch("nfl.lines.fetch_odds", return_value=[]) as odds, patch(
            "nfl.lines.parse_odds_games", return_value=sentinel
        ), patch("nfl.lines.fetch_game_lines") as gs:
            out = ingest_slate_lines(
                players, slate_day=date(2026, 9, 13), source="oddsapi"
            )
        odds.assert_called_once()
        gs.assert_not_called()
        self.assertIs(out, sentinel)

    def test_lines_json_wins_over_gangstash_source(self) -> None:
        players = [_pl()]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lines.json"
            path.write_text(
                json.dumps([{"away": "DAL", "home": "PHI", "spread": -3, "total": 44}]),
                encoding="utf-8",
            )
            with patch("nfl.lines.fetch_game_lines") as gs, patch(
                "nfl.lines.fetch_odds"
            ) as odds:
                by_team = ingest_slate_lines(
                    players,
                    lines_json=path,
                    slate_day=date(2026, 9, 13),
                    source="gangstash",
                )
        gs.assert_not_called()
        odds.assert_not_called()
        self.assertEqual(by_team["PHI"].source, "lines-json")
        self.assertAlmostEqual(by_team["PHI"].total, 44)


class DepthSourceTest(unittest.TestCase):
    def test_gangstash_depth_maps_rank_and_skips_non_skill(self) -> None:
        raw = [
            {
                "player_name": "A.J. Brown",
                "team_fd": "PHI",
                "team": "PHI",
                "pos_grp": "3WR 1TE",
                "pos_abb": "WR",
                "pos_name": "Wide Receiver",
                "pos_slot": "WR",
                "pos_rank": 1,
                "gsis_id": "00-0035676",
                "espn_id": "404",
                "player_id": "brown",
                "snapshot_at": "2026-09-24T00:00:00Z",
            },
            {
                "player_name": "A.J. Brown",
                "team_fd": "PHI",
                "pos_grp": "2WR 2TE",
                "pos_abb": "WR",
                "pos_rank": 1,
            },
            {
                "player_name": "A.J. Brown",
                "team_fd": "PHI",
                "pos_grp": "3WR 1TE",
                "pos_abb": "WR",
                "pos_rank": 5,
            },
            {
                "player_name": "DeVonta Smith",
                "team_fd": "PHI",
                "pos_grp": "3WR 1TE",
                "pos_abb": "WR",
                "pos_rank": 2,
            },
            {
                "player_name": "Lane Johnson",
                "team_fd": "PHI",
                "pos_grp": "3WR 1TE",
                "pos_abb": "LT",
                "pos_rank": 1,
            },
            {
                "player_name": "Travis Etienne",
                "team_fd": "JAX",
                "pos_grp": "3WR 1TE",
                "pos_abb": "RB",
                "pos_rank": 1,
            },
        ]
        with patch("nfl.depth.fetch_depth_charts", return_value=(raw, {"live": True})):
            rows = ingest_slate_depth({"PHI", "JAC"}, source="gangstash")
        by_name = {r.name: r for r in rows}
        self.assertEqual(set(by_name), {"A.J. Brown", "DeVonta Smith", "Travis Etienne"})
        self.assertEqual(by_name["A.J. Brown"].pos, "WR")
        self.assertEqual(by_name["A.J. Brown"].rank, 1)
        self.assertEqual(by_name["DeVonta Smith"].rank, 2)
        self.assertEqual(by_name["Travis Etienne"].team, "JAC")

    def test_missing_pos_rank_is_fatal(self) -> None:
        raw = [
            {
                "player_name": "A.J. Brown",
                "team_fd": "PHI",
                "pos_grp": "3WR 1TE",
                "pos_abb": "WR",
            }
        ]
        with patch("nfl.depth.fetch_depth_charts", return_value=(raw, {})):
            with self.assertRaises(GangstashDepthError) as ctx:
                ingest_slate_depth({"PHI"}, source="gangstash")
        self.assertIn("pos_rank", str(ctx.exception))
        self.assertIn("absent", str(ctx.exception))

    def test_null_identity_rows_are_skipped_and_counted(self) -> None:
        raw = [
            {
                "player_name": "A.J. Brown",
                "team_fd": "PHI",
                "team": "PHI",
                "pos_grp": "3WR 1TE",
                "pos_abb": "WR",
                "pos_rank": 1,
            },
            {
                "team": "NYG",
                "team_fd": "NYG",
                "pos_grp": "3WR 1TE",
                "pos_abb": "TE",
                "pos_rank": 3,
                "player_name": None,
                "gsis_id": None,
                "player_id": None,
                "espn_id": "2531358",
            },
            {
                "team": "LAR",
                "team_fd": "LAR",
                "pos_grp": "Base 4-3 D",
                "pos_abb": "DE",
                "pos_rank": 1,
                "player_name": None,
                "gsis_id": None,
                "player_id": None,
            },
            {
                "team": "LAR",
                "team_fd": "LAR",
                "pos_grp": "Base 4-3 D",
                "pos_abb": "LB",
                "pos_rank": None,
                "player_name": None,
            },
            {
                "player_name": "Saquon Barkley",
                "team_fd": None,
                "pos_grp": "3WR 1TE",
                "pos_abb": "RB",
                "pos_rank": 1,
            },
        ]
        buf = io.StringIO()
        with patch("nfl.depth.fetch_depth_charts", return_value=(raw, {})), redirect_stderr(
            buf
        ):
            rows = ingest_slate_depth({"PHI"}, source="gangstash")
        self.assertEqual([r.name for r in rows], ["A.J. Brown"])
        self.assertIn(
            "gangstash depth_charts skipped 4 rows with null player_name, "
            "team_fd, pos_abb, or pos_rank",
            buf.getvalue(),
        )

    def test_empty_depth_response_is_fatal(self) -> None:
        with patch("nfl.depth.fetch_depth_charts", return_value=([], {})):
            with self.assertRaises(GangstashDepthError) as ctx:
                ingest_slate_depth({"PHI"}, source="gangstash")
        self.assertIn("empty", str(ctx.exception))

    def test_ourlads_source_does_not_call_gangstash(self) -> None:
        with patch("nfl.depth.ingest_ourlads_depth", return_value=[]) as ol, patch(
            "nfl.depth.fetch_depth_charts"
        ) as gs:
            ingest_slate_depth({"PHI"}, source="ourlads")
        ol.assert_called_once()
        gs.assert_not_called()


class TeamStatsTest(unittest.TestCase):
    def test_team_stats_queries_are_cached_not_scored(self) -> None:
        calls: list[str] = []

        def fake_http(url, headers=None, timeout=30):
            calls.append(url)
            return {
                "data": [
                    {
                        "team": "PHI",
                        "team_fd": "PHI",
                        "side": "offense",
                        "pass_rate": 0.61,
                        "neutral_pass_rate": 0.55,
                        "proe": 0.02,
                        "success_rate": 0.47,
                        "epa_per_play": 0.08,
                    }
                ],
                "truncated": False,
            }, {}

        with tempfile.TemporaryDirectory() as tmp, patch(
            "nfl.gangstash.DATA_CACHE_DIR", Path(tmp)
        ), patch("nfl.gangstash.envmod.get", side_effect=_env_get), patch(
            "nfl.gangstash.http_json", side_effect=fake_http
        ):
            rows, meta = fetch_team_stats(
                season=2026, side="offense", team="PHI", refresh=True, cache_day=date(2026, 9, 24)
            )
            weekly, _wmeta = fetch_team_stats_weekly(
                season=2026, week=2, refresh=True, cache_day=date(2026, 9, 24)
            )
            self.assertTrue(list(Path(tmp).rglob("*.json")))
        self.assertEqual(rows[0]["team"], "PHI")
        self.assertTrue(meta["live"])
        self.assertEqual(weekly, rows)
        self.assertIn("dataset=team_stats", calls[0])
        self.assertIn("season=2026", calls[0])
        self.assertIn("season_type=REG", calls[0])
        self.assertIn("side=offense", calls[0])
        self.assertIn("team=PHI", calls[0])
        self.assertEqual(rows[0]["pass_rate"], 0.61)
        self.assertIn("dataset=team_stats_weekly", calls[1])
        self.assertIn("week=2", calls[1])
        self.assertNotIn("secret-key", calls[0])


class FlagDefaultTest(unittest.TestCase):
    def test_existing_sources_stay_the_default(self) -> None:
        args = parse_args(["--csv", "players.csv"])
        self.assertEqual(args.lines_source, "oddsapi")
        self.assertEqual(args.targets_source, "lineups")
        self.assertEqual(args.depth_source, "ourlads")
        self.assertIsNone(args.targets_weeks)
        self.assertIsNone(args.targets_week)
        self.assertFalse(args.refresh_targets)

    def test_week_list_parses(self) -> None:
        args = parse_args(
            ["--csv", "players.csv", "--targets-source", "gangstash", "--targets-weeks", "1,2"]
        )
        self.assertEqual(args.targets_source, "gangstash")
        self.assertEqual(args.targets_weeks, [1, 2])


class PagingTest(unittest.TestCase):
    def test_truncated_pages_join_and_cache_the_full_board(self) -> None:
        day = date(2026, 9, 24)
        calls: list[str] = []

        def fake_http(url, headers=None, timeout=30):
            calls.append(url)
            if "offset=" not in url:
                return {"data": [{"player_name": "A"}], "truncated": True}, {}
            return {"data": [{"player_name": "B"}], "truncated": False}, {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("nfl.gangstash.envmod.get", side_effect=_env_get), patch(
                "nfl.gangstash.http_json", side_effect=fake_http
            ):
                rows, meta = fetch_dataset(
                    "targets",
                    {"season": "2026"},
                    cache_day=day,
                    cache_root=root,
                    refresh=True,
                )
            self.assertEqual([r["player_name"] for r in rows], ["A", "B"])
            self.assertFalse(meta["truncated"])
            self.assertTrue(meta["live"])
            cached = json.loads(Path(meta["cache"]).read_text(encoding="utf-8"))
            self.assertEqual(len(cached["data"]), 2)
            self.assertFalse(cached["truncated"])
        self.assertEqual(len(calls), 2)
        self.assertNotIn("offset=", calls[0])
        self.assertIn("offset=1000", calls[1])
        self.assertNotIn("secret-key", calls[0])

    def test_cap_refuses_a_partial_board(self) -> None:
        def fake_http(url, headers=None, timeout=30):
            return {"data": [{"player_name": "A"}], "truncated": True}, {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("nfl.gangstash.PAGE_SIZE", 1000), patch(
                "nfl.gangstash.MAX_ROWS", 2000
            ), patch("nfl.gangstash.envmod.get", side_effect=_env_get), patch(
                "nfl.gangstash.http_json", side_effect=fake_http
            ):
                with self.assertRaises(GangstashTruncated):
                    fetch_dataset(
                        "targets",
                        {"season": "2026"},
                        cache_day=date(2026, 9, 24),
                        cache_root=root,
                        refresh=True,
                    )
            self.assertEqual(list(root.rglob("*.json")), [])

    def test_http_400_and_401_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("nfl.gangstash.envmod.get", side_effect=_env_get), patch(
                "nfl.gangstash.http_json",
                side_effect=HttpError("HTTP 400: unknown dataset"),
            ):
                with self.assertRaises(GangstashDataError) as ctx:
                    fetch_dataset(
                        "nope",
                        {},
                        cache_day=date(2026, 9, 24),
                        cache_root=root,
                        refresh=True,
                    )
            self.assertIn("400", str(ctx.exception))
            with patch("nfl.gangstash.envmod.get", side_effect=_env_get), patch(
                "nfl.gangstash.http_json",
                side_effect=HttpAuthError("HTTP 401 (key rejected)."),
            ):
                with self.assertRaises(GangstashDataError) as ctx:
                    fetch_dataset(
                        "targets",
                        {"season": "2026"},
                        cache_day=date(2026, 9, 24),
                        cache_root=root,
                        refresh=True,
                    )
            self.assertIn("401", str(ctx.exception))
            self.assertEqual(list(root.rglob("*.json")), [])


@unittest.skipUnless(
    os.environ.get("GANGSTASH_API_KEY") and os.environ.get("GANGSTASH_LIVE_SMOKE") == "1",
    "set GANGSTASH_API_KEY and GANGSTASH_LIVE_SMOKE=1 to hit gangstash",
)
class LiveSmokeTest(unittest.TestCase):
    """Optional live check. Skipped unless both the key and the flag are set."""

    def test_live_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            targets, tmeta = fetch_dataset(
                "targets",
                {"season": "2026", "week": "1,2"},
                refresh=True,
                cache_root=root,
                cache_day=date.today(),
            )
            self.assertTrue(tmeta["live"])
            self.assertFalse(tmeta["truncated"])
            self.assertGreaterEqual(len(targets), 1)
            row = targets[0]
            for key in (
                "season",
                "week",
                "position",
                "player_name",
                "team_fd",
                "targets",
                "target_share",
                "team_targets",
                "gsis_id",
                "player_id",
            ):
                self.assertIn(key, row)
            self.assertNotIn("targets_total", row)
            self.assertNotIn("targets_avg", row)
            self.assertIn(row["position"], {"WR", "TE", "RB"})

            lines, lmeta = fetch_dataset(
                "game_lines",
                {"season": "2026", "week": "3"},
                refresh=True,
                cache_root=root,
                cache_day=date.today(),
            )
            self.assertTrue(lmeta["live"])
            self.assertEqual(len(lines), 16)
            game = lines[0]
            for key in (
                "game_id",
                "season",
                "week",
                "commence_time",
                "home_team_fd",
                "away_team_fd",
                "spread",
                "total",
                "home_moneyline",
                "away_moneyline",
                "updated_at",
            ):
                self.assertIn(key, game)
            self.assertNotIn("moneylines", game)


if __name__ == "__main__":
    unittest.main()
