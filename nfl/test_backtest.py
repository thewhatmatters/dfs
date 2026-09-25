"""player_stats_weekly reader and the board-vs-sim backtest. No network."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from nfl.backtest import (
    index_actuals,
    main,
    run_backtest,
    summarize_errors,
)
from nfl.gangstash import GangstashDataKeyMissing, dataset_cache_file
from nfl.gangstash_data import fetch_player_stats_weekly
from nfl.players import Player
from nfl.projections import week1_score
from nfl.sim_inputs import sim_inputs_from_records


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("pid", "p"),
        name=kw.get("name", "Player"),
        position=kw.get("position", "WR"),
        salary=6000,
        team=kw.get("team", "DET"),
        opponent="NO",
        game="NO@DET",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=kw.get("implied_total", 24.0),
        implied_opp=20.0,
        depth_rank=1,
        total=44.0,
        spread=0.0,
    )
    fields.update(kw)
    if fields.get("objective") is None:
        fields["objective"] = week1_score(
            fields.get("implied_total") or 0.0,
            depth_rank=fields.get("depth_rank"),
            position=fields["position"],
        )
    allowed = set(Player.__dataclass_fields__)
    return Player(**{k: v for k, v in fields.items() if k in allowed})


class PlayerStatsReaderTest(unittest.TestCase):
    def test_query_is_season_and_week(self) -> None:
        with patch(
            "nfl.gangstash_data.fetch_dataset",
            return_value=([], {"cache": "x"}),
        ) as fetch:
            fetch_player_stats_weekly(season=2026, week=2)
        self.assertEqual(fetch.call_args.args[0], "player_stats_weekly")
        self.assertEqual(fetch.call_args.args[1]["season"], "2026")
        self.assertEqual(fetch.call_args.args[1]["week"], "2")

    def test_same_day_cache_skips_the_network(self) -> None:
        day = date(2026, 9, 20)
        params = {"season": "2026", "week": "2"}
        payload = {
            "data": [
                {
                    "season": 2026,
                    "week": 2,
                    "player_name": "Bijan Robinson",
                    "team_fd": "ATL",
                    "position": "RB",
                    "fd_points": 9.6,
                    "carries": 12,
                }
            ],
            "truncated": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = dataset_cache_file(root, day, "player_stats_weekly", params)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("nfl.gangstash.DATA_CACHE_DIR", root), patch(
                "nfl.gangstash.http_json", side_effect=AssertionError("network")
            ):
                rows, meta = fetch_player_stats_weekly(
                    season=2026, week=2, cache_day=day
                )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fd_points"], 9.6)
        self.assertFalse(meta["live"])
        self.assertIn("player_stats_weekly", meta["cache"])


class BacktestReportTest(unittest.TestCase):
    def test_mean_error_and_mae_by_position(self) -> None:
        paired = [
            ("QB", 18.0, 16.0, 14.0),
            ("QB", 12.0, 15.0, 16.0),
            ("RB", 20.0, 14.0, 10.0),
            ("RB", 16.0, 12.0, 12.0),
        ]
        report = summarize_errors(paired, season=2026, week=2, missing=["props", "game_lines"])
        by = {row.position: row for row in report.rows}
        self.assertEqual(by["QB"].n, 2)
        self.assertAlmostEqual(by["QB"].board_me, ((18 - 14) + (12 - 16)) / 2, places=4)
        self.assertAlmostEqual(by["QB"].board_mae, (4 + 4) / 2, places=4)
        self.assertAlmostEqual(by["RB"].sim_me, ((14 - 10) + (12 - 12)) / 2, places=4)
        self.assertAlmostEqual(by["RB"].sim_mae, (4 + 0) / 2, places=4)
        self.assertEqual(by["ALL"].n, 4)
        text = report.to_text()
        self.assertIn("missing: props, game_lines", text)
        self.assertIn("RB", text)

    def test_missing_inputs_still_score(self) -> None:
        qb = _pl(pid="qb", name="Jared Goff", position="QB", team="DET")
        rb = _pl(pid="rb", name="Jahmyr Gibbs", position="RB", team="DET")
        actuals = [
            {"player_name": "Jared Goff", "team_fd": "DET", "fd_points": 15.0, "position": "QB"},
            {"player_name": "Jahmyr Gibbs", "team_fd": "DET", "fd_points": 10.0, "position": "RB"},
        ]
        report = run_backtest(
            [qb, rb],
            actuals,
            season=2026,
            week=2,
            sim_inputs=None,
            missing=["props", "game_lines"],
            n=50,
            seed=1,
        )
        self.assertEqual(report.missing, ("props", "game_lines"))
        self.assertGreaterEqual(report.n, 2)
        self.assertIn("props", report.to_text())
        self.assertIn("game_lines", report.to_text())

    def test_command_reports_empty_props_and_lines(self) -> None:
        qb = _pl(pid="1", name="QB One", position="QB")
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "players.csv"
            csv_path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game,FPPG,Injury Indicator,Roster Position\n"
                "1,QB,QB One,8000,DET,NO,NO@DET,,,\n",
                encoding="utf-8",
            )
            actual = [
                {
                    "player_name": "QB One",
                    "team_fd": "DET",
                    "position": "QB",
                    "week": 2,
                    "season": 2026,
                    "fd_points": 11.0,
                }
            ]
            with patch(
                "nfl.backtest.fetch_player_stats_weekly",
                return_value=(actual, {"live": False}),
            ), patch(
                "nfl.backtest.fetch_game_lines",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_props",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.resolve_sim_inputs",
                return_value=(None, "sim inputs: gangstash unavailable — role shares deterministic"),
            ), patch("nfl.gangstash.http_json", side_effect=AssertionError("network")):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main(
                        ["--csv", str(csv_path), "--season", "2026", "--week", "2", "--n", "20"]
                    )
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn("missing: game_lines, props, sim_inputs", text)
        self.assertIn("QB", text)

    def test_index_actuals_uses_fd_points(self) -> None:
        rows = [
            {
                "player_name": "Bijan Robinson",
                "team": "ATL",
                "fd_points": 9.6,
                "carries": 12,
                "week": 2,
            }
        ]
        got = index_actuals(rows)
        self.assertEqual(got[("ATL", "bijan robinson")], 9.6)

    def test_missing_key_is_a_choke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "players.csv"
            csv_path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game,FPPG,Injury Indicator,Roster Position\n"
                "1,QB,QB One,8000,DET,NO,NO@DET,,,\n",
                encoding="utf-8",
            )
            with patch(
                "nfl.backtest.fetch_player_stats_weekly",
                side_effect=GangstashDataKeyMissing("GANGSTASH_API_KEY is not set"),
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main(["--csv", str(csv_path), "--season", "2026", "--week", "2"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
