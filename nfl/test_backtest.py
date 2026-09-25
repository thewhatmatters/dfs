"""player_stats_weekly reader and the board-vs-sim backtest. No network."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from nfl.backtest import (
    apply_week_context,
    hindsight_qb_pids,
    index_actuals,
    is_hindsight_starter,
    is_starter,
    main,
    prior_weeks,
    run_backtest,
    select_backtest_depth,
    summarize_errors,
    _depth_table,
)
from nfl.injuries import injury_rows_from_records
from nfl.props import props_for_week
from nfl.sim import simulate_games
from nfl.gangstash import GangstashDataError, GangstashDataKeyMissing, dataset_cache_file
from nfl.depth import attach_depth_ranks
from nfl.gangstash_data import (
    fetch_depth_charts_weekly,
    fetch_player_stats_weekly,
    fetch_player_usage,
)
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

    def test_player_usage_query_is_optional_filters(self) -> None:
        with patch(
            "nfl.gangstash_data.fetch_dataset",
            return_value=([], {"cache": "x"}),
        ) as fetch:
            fetch_player_usage(
                season=2026,
                weeks=[1],
                team="DET",
                gsis_id="00-ARSB",
                position="WR",
            )
        self.assertEqual(fetch.call_args.args[0], "player_usage")
        params = fetch.call_args.args[1]
        self.assertEqual(params["season"], "2026")
        self.assertEqual(params["week"], "1")
        self.assertEqual(params["team"], "DET")
        self.assertEqual(params["gsis_id"], "00-ARSB")
        self.assertEqual(params["position"], "WR")

    def test_depth_charts_weekly_query(self) -> None:
        with patch(
            "nfl.gangstash_data.fetch_dataset",
            return_value=([], {"cache": "x"}),
        ) as fetch:
            fetch_depth_charts_weekly(season=2026, weeks=[1, 2], team="ATL")
        self.assertEqual(fetch.call_args.args[0], "depth_charts_weekly")
        params = fetch.call_args.args[1]
        self.assertEqual(params["season"], "2026")
        self.assertEqual(params["week"], "1,2")
        self.assertEqual(params["team"], "ATL")
        self.assertEqual(params["pos_grp"], "3WR 1TE")

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
        self.assertIn("missing: game_lines, props", text)
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
        self.assertEqual(report.missing, ("game_lines", "props"))
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
                "nfl.backtest.fetch_dst_weekly",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_closing_lines",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_game_lines",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_week_injuries",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_depth_charts_weekly",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_depth_charts",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.load_optimizer_targets",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.load_optimizer_snaps",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_props",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.resolve_sim_inputs",
                return_value=(None, "sim inputs: gangstash unavailable — role shares deterministic"),
            ), patch("nfl.gangstash.http_json", side_effect=AssertionError("network")):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = main(
                        ["--csv", str(csv_path), "--season", "2026", "--week", "2", "--n", "20"]
                    )
        self.assertEqual(code, 1)
        self.assertIn("choke LINES:", err.getvalue())
        self.assertIn("no lines", err.getvalue())

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
            ), patch(
                "nfl.backtest.fetch_dst_weekly",
                return_value=([], {}),
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main(["--csv", str(csv_path), "--season", "2026", "--week", "2"])
        self.assertEqual(code, 1)


def _depth(name: str, team: str, pos: str, rank: int) -> dict:
    return {
        "player_name": name,
        "team_fd": team,
        "pos_grp": "3WR 1TE",
        "pos_abb": pos,
        "pos_rank": rank,
        "week": 2,
        "season": 2026,
    }


def _actual(name: str, team: str, fd: float, **extra) -> dict:
    row = {
        "player_name": name,
        "team_fd": team,
        "fd_points": fd,
        "season": 2026,
        "week": 2,
    }
    row.update(extra)
    return row


class WeekScopeTest(unittest.TestCase):
    def test_depth_1_board_is_the_role_share_not_unlisted(self) -> None:
        qb = _pl(
            pid="qb",
            name="Malik Willis",
            position="QB",
            team="MIA",
            opponent="NE",
            game="NE@MIA",
            depth_rank=None,
            implied_total=None,
        )
        qb = apply_week_context(
            [qb],
            season=2026,
            week=2,
            line_rows=[
                {
                    "home_team_fd": "MIA",
                    "away_team_fd": "NE",
                    "season": 2026,
                    "week": 2,
                    "home_implied_total": 17.5,
                    "away_implied_total": 24.0,
                    "commence_time": "2026-09-14T17:00:00Z",
                }
            ],
            depth_raw=[_depth("Malik Willis", "MIA", "QB", 1)],
            lines_attached=False,
        )[0][0]
        self.assertEqual(qb.depth_rank, 1)
        self.assertAlmostEqual(qb.implied_total or 0, 17.5, places=4)
        self.assertAlmostEqual(float(week1_score(17.5, 1, "QB")), 8.75, places=4)
        self.assertAlmostEqual(qb.objective or 0, 8.75, places=4)

    def test_week_3_props_do_not_join_week_2(self) -> None:
        rows = [
            {
                "player_name": "Malik Willis",
                "prop": "Pass YDs",
                "line": 210.5,
                "season": 2026,
                "week": 3,
                "scraped_at": "2026-09-20T12:00:00Z",
            },
            {
                "player_name": "Malik Willis",
                "prop": "Pass YDs",
                "line": 40,
                "season": 2026,
                "week": 2,
                "scraped_at": "2026-09-15T18:00:00Z",
            },
            {
                "player_name": "Malik Willis",
                "prop": "Pass YDs",
                "line": 180.5,
                "season": 2026,
                "week": 2,
                "scraped_at": "2026-09-13T12:00:00Z",
            },
        ]
        kept = props_for_week(
            rows,
            season=2026,
            week=2,
            kickoff=None,
        )
        from datetime import datetime, timezone

        pre = props_for_week(
            rows,
            season=2026,
            week=2,
            kickoff=datetime(2026, 9, 14, 17, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["line"], 40)
        self.assertEqual(pre[0]["line"], 180.5)
        self.assertEqual(
            props_for_week(
                [{"player_name": "Malik Willis", "prop": "Pass YDs", "line": 99, "scraped_at": "2026-09-21T00:00:00Z"}],
                season=2026,
                week=2,
                kickoff=None,
            ),
            [],
        )

    def test_prior_weeks_are_only_before_the_scored_week(self) -> None:
        self.assertEqual(prior_weeks(2), [1])
        self.assertEqual(prior_weeks(1), [])

    def test_targets_request_is_prior_weeks_only(self) -> None:
        qb = _pl(pid="1", name="QB One", position="QB")
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "players.csv"
            csv_path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game,FPPG,Injury Indicator,Roster Position\n"
                "1,QB,QB One,8000,DET,NO,NO@DET,,,\n",
                encoding="utf-8",
            )
            actual = [_actual("QB One", "DET", 11.0, pass_attempts=30)]
            with patch(
                "nfl.backtest.fetch_player_stats_weekly",
                return_value=(actual, {"live": False}),
            ), patch(
                "nfl.backtest.fetch_dst_weekly",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_closing_lines",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_game_lines",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_week_injuries",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_depth_charts_weekly",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_depth_charts",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.load_optimizer_targets",
                return_value=([], {}),
            ) as targets, patch(
                "nfl.backtest.load_optimizer_snaps",
                return_value=([], {}),
            ) as snaps, patch(
                "nfl.backtest.fetch_props",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.resolve_sim_inputs",
                return_value=(None, ""),
            ), patch("nfl.gangstash.http_json", side_effect=AssertionError("network")):
                code = main(
                    [
                        "--csv",
                        str(csv_path),
                        "--season",
                        "2026",
                        "--week",
                        "2",
                        "--n",
                        "5",
                        "--allow-missing-lines",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(targets.call_args.kwargs["weeks"], [1])
        self.assertEqual(snaps.call_args.kwargs["weeks"], [1])

    def test_lines_file_csv_sets_implied_and_skips_the_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "players.csv"
            lines_path = root / "lines.csv"
            csv_path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game,FPPG,Injury Indicator,Roster Position\n"
                "1,QB,Malik Willis,7000,MIA,NE,NE@MIA,,,\n",
                encoding="utf-8",
            )
            lines_path.write_text(
                "away,home,home_implied,away_implied,commence_time\n"
                "NE,MIA,17.5,24.0,2026-09-14T17:00:00Z\n",
                encoding="utf-8",
            )
            actual = [_actual("Malik Willis", "MIA", 14.0, pass_attempts=28)]
            with patch(
                "nfl.backtest.fetch_player_stats_weekly",
                return_value=(actual, {"live": False}),
            ), patch(
                "nfl.backtest.fetch_dst_weekly",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_closing_lines",
                side_effect=AssertionError("network"),
            ), patch(
                "nfl.backtest.fetch_game_lines",
                side_effect=AssertionError("network"),
            ), patch(
                "nfl.backtest.fetch_week_injuries",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_depth_charts_weekly",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_depth_charts",
                return_value=([_depth("Malik Willis", "MIA", "QB", 1)], {"live": False}),
            ), patch(
                "nfl.backtest.load_optimizer_targets",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.load_optimizer_snaps",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_props",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.resolve_sim_inputs",
                return_value=(None, ""),
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main(
                        [
                            "--csv",
                            str(csv_path),
                            "--season",
                            "2026",
                            "--week",
                            "2",
                            "--n",
                            "30",
                            "--lines-file",
                            str(lines_path),
                        ]
                    )
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertNotIn("closing_lines", text.split("missing:")[1].split("\n")[0])
        self.assertIn("QB", text)
        self.assertIn("sim efficiency: placeholder", text)

    def test_injured_starter_loses_the_pass_volume(self) -> None:
        darnold = _pl(
            pid="darnold",
            name="Sam Darnold",
            position="QB",
            team="SEA",
            opponent="NE",
            game="NE@SEA",
            salary=8000,
            depth_rank=None,
            implied_total=None,
        )
        lock = _pl(
            pid="lock",
            name="Drew Lock",
            position="QB",
            team="SEA",
            opponent="NE",
            game="NE@SEA",
            salary=6000,
            depth_rank=None,
            implied_total=None,
        )
        players, gaps, _notes = apply_week_context(
            [darnold, lock],
            season=2026,
            week=2,
            line_rows=[
                {
                    "home_team_fd": "SEA",
                    "away_team_fd": "NE",
                    "season": 2026,
                    "week": 2,
                    "spread": -3.0,
                    "total": 44.0,
                }
            ],
            depth_raw=[
                _depth("Sam Darnold", "SEA", "QB", 1),
                _depth("Drew Lock", "SEA", "QB", 2),
            ],
            injury_raw=[
                {
                    "player_name": "Sam Darnold",
                    "team_fd": "SEA",
                    "status": "Out",
                    "season": 2026,
                    "week": 2,
                }
            ],
        )
        by_name = {pl.name: pl for pl in players}
        self.assertIn("Sam Darnold", by_name)
        self.assertIn("Drew Lock", by_name)
        self.assertIsNone(by_name["Sam Darnold"].depth_rank)
        self.assertEqual(by_name["Drew Lock"].depth_rank, 1)
        self.assertNotIn("injuries", gaps)
        gs = simulate_games(players, n=40, seed=1)
        self.assertGreater(gs.by_pid["lock"].mean, 12.0)
        self.assertLess(gs.by_pid["darnold"].mean, 0.5)

    def test_injury_id_stamps_without_a_name(self) -> None:
        rows = injury_rows_from_records(
            [
                {
                    "player_id": "00-0035676",
                    "gsis_id": "00-0035676",
                    "status": "IR",
                    "season": 2026,
                    "week": 2,
                },
                {
                    "player_id": "lane",
                    "status": "Doubtful",
                    "team": "BAL",
                    "season": 2026,
                },
                {
                    "player_id": "dated",
                    "status": "Out",
                    "team": "NE",
                    "season": 2026,
                    "week": 2,
                },
            ],
            season=2026,
            week=2,
        )
        # One row in the payload has a week, so the undated Lane row is dropped.
        self.assertEqual([row.player_id for row in rows], ["00-0035676", "dated"])
        undated = injury_rows_from_records(
            [
                {
                    "player_id": "lane",
                    "player_name": "Ja'Kobi Lane",
                    "status": "D",
                    "team": "BAL",
                    "season": 2026,
                }
            ],
            season=2026,
            week=2,
        )
        self.assertEqual(len(undated), 1)
        brown = _pl(
            pid="00-0035676",
            name="A.J. Brown",
            position="WR",
            team="NE",
            opponent="MIA",
            game="MIA@NE",
        )
        players, gaps, notes = apply_week_context(
            [brown],
            season=2026,
            week=2,
            line_rows=[
                {
                    "home_team_fd": "NE",
                    "away_team_fd": "MIA",
                    "season": 2026,
                    "week": 2,
                    "spread": -3.0,
                    "total": 44.0,
                }
            ],
            injury_raw=[
                {
                    "gsis_id": "00-0035676",
                    "player_id": "00-0035676",
                    "status": "IR",
                    "season": 2026,
                    "week": 2,
                }
            ],
            lines_attached=True,
        )
        self.assertEqual(players[0].injury, "IR")
        self.assertIn("injuries: gangstash 1", notes)
        self.assertNotIn("injuries", gaps)

    def test_starters_are_depth_1_who_played(self) -> None:
        starter = _pl(
            pid="s",
            name="Starter WR",
            position="WR",
            team="MIA",
            opponent="NE",
            game="NE@MIA",
            depth_rank=1,
        )
        bench = _pl(
            pid="b",
            name="Bench WR",
            position="WR",
            team="MIA",
            opponent="NE",
            game="NE@MIA",
            depth_rank=2,
        )
        actuals = [
            _actual("Starter WR", "MIA", 12.0, targets=7),
            _actual("Bench WR", "MIA", 4.0, targets=2),
        ]
        self.assertTrue(is_starter(starter, actuals[0]))
        self.assertTrue(is_starter(bench, actuals[1]))
        fourth = _pl(
            pid="w4",
            name="Fourth WR",
            position="WR",
            team="MIA",
            opponent="NE",
            game="NE@MIA",
            depth_rank=4,
        )
        self.assertFalse(
            is_starter(fourth, _actual("Fourth WR", "MIA", 3.0, targets=1))
        )
        backup_qb = _pl(
            pid="qb2",
            name="Backup QB",
            position="QB",
            team="MIA",
            opponent="NE",
            game="NE@MIA",
            depth_rank=2,
        )
        self.assertFalse(
            is_starter(backup_qb, _actual("Backup QB", "MIA", 4.0, pass_attempts=10))
        )
        sat = _pl(
            pid="z",
            name="Sat WR",
            position="WR",
            team="MIA",
            opponent="NE",
            game="NE@MIA",
            depth_rank=1,
        )
        self.assertFalse(is_starter(sat, _actual("Sat WR", "MIA", 0.0, targets=0, snaps=0)))
        report = run_backtest(
            [starter, bench],
            actuals,
            season=2026,
            week=2,
            n=20,
            seed=1,
            missing=[],
        )
        self.assertEqual(report.n, 2)
        self.assertEqual(report.starters_n, 2)
        only = run_backtest(
            [starter, bench],
            actuals,
            season=2026,
            week=2,
            n=20,
            seed=1,
            starters_only=True,
        )
        self.assertEqual(only.pool, "starters")
        self.assertEqual(only.n, 2)

    def test_csv_doubtful_hands_off_and_questionable_stays(self) -> None:
        darnold = _pl(
            pid="darnold",
            name="Sam Darnold",
            position="QB",
            team="SEA",
            opponent="NE",
            game="NE@SEA",
            salary=8000,
            injury="D",
            depth_rank=None,
            implied_total=None,
        )
        lock = _pl(
            pid="lock",
            name="Drew Lock",
            position="QB",
            team="SEA",
            opponent="NE",
            game="NE@SEA",
            salary=6000,
            injury="",
            depth_rank=None,
            implied_total=None,
        )
        murray = _pl(
            pid="murray",
            name="Kyler Murray",
            position="QB",
            team="ARI",
            opponent="LAR",
            game="LAR@ARI",
            salary=7800,
            injury="Q",
            depth_rank=None,
            implied_total=None,
        )
        wentz = _pl(
            pid="wentz",
            name="Carson Wentz",
            position="QB",
            team="ARI",
            opponent="LAR",
            game="LAR@ARI",
            salary=5600,
            injury="",
            depth_rank=None,
            implied_total=None,
        )
        players, _gaps, _notes = apply_week_context(
            [darnold, lock, murray, wentz],
            season=2026,
            week=2,
            line_rows=[
                {
                    "home_team_fd": "SEA",
                    "away_team_fd": "NE",
                    "season": 2026,
                    "week": 2,
                    "spread": -3.0,
                    "total": 44.0,
                },
                {
                    "home_team_fd": "ARI",
                    "away_team_fd": "LAR",
                    "season": 2026,
                    "week": 2,
                    "spread": 1.0,
                    "total": 45.0,
                },
            ],
            depth_raw=[
                _depth("Sam Darnold", "SEA", "QB", 1),
                _depth("Drew Lock", "SEA", "QB", 2),
                _depth("Kyler Murray", "ARI", "QB", 1),
                _depth("Carson Wentz", "ARI", "QB", 2),
            ],
        )
        by_name = {pl.name: pl for pl in players}
        self.assertIsNone(by_name["Sam Darnold"].depth_rank)
        self.assertEqual(by_name["Drew Lock"].depth_rank, 1)
        self.assertEqual(by_name["Kyler Murray"].depth_rank, 1)
        self.assertEqual(by_name["Carson Wentz"].depth_rank, 2)
        before = by_name["Kyler Murray"].objective
        report = run_backtest(
            players,
            [
                _actual("Sam Darnold", "SEA", 0.0, pass_attempts=0),
                _actual("Drew Lock", "SEA", 21.4, pass_attempts=30),
                _actual("Kyler Murray", "ARI", 0.0, pass_attempts=0),
                _actual("Carson Wentz", "ARI", 6.3, pass_attempts=12),
            ],
            season=2026,
            week=2,
            n=20,
            seed=1,
        )
        self.assertEqual(by_name["Kyler Murray"].objective, before)
        self.assertEqual(by_name["Kyler Murray"].depth_rank, 1)
        text = report.to_text()
        self.assertIn("questionable DNP: 1 (projection kept)", text)
        self.assertIn("pool: hindsight (actual QB1 by snaps)", text)
        starters = {row.position: row for row in report.starters}
        hindsight = {row.position: row for row in report.hindsight}
        self.assertEqual(starters["QB"].n, 1)
        self.assertEqual(hindsight["QB"].n, 2)
        lock_row = _actual("Drew Lock", "SEA", 21.4, pass_attempts=30)
        wentz_row = _actual("Carson Wentz", "ARI", 6.3, pass_attempts=12)
        murray_row = _actual("Kyler Murray", "ARI", 0.0, pass_attempts=0)
        self.assertTrue(is_starter(by_name["Drew Lock"], lock_row))
        self.assertFalse(is_starter(by_name["Carson Wentz"], wentz_row))
        self.assertFalse(is_starter(by_name["Kyler Murray"], murray_row))
        qb_pids = hindsight_qb_pids(players, {
            ("SEA", "sam darnold"): {"row": _actual("Sam Darnold", "SEA", 0.0, pass_attempts=0)},
            ("SEA", "drew lock"): {"row": lock_row},
            ("ARI", "kyler murray"): {"row": murray_row},
            ("ARI", "carson wentz"): {"row": wentz_row},
        })
        self.assertIn(by_name["Carson Wentz"].pid, qb_pids)
        self.assertNotIn(by_name["Kyler Murray"].pid, qb_pids)
        self.assertTrue(is_hindsight_starter(by_name["Carson Wentz"], wentz_row, qb_pids))
        self.assertFalse(is_hindsight_starter(by_name["Kyler Murray"], murray_row, qb_pids))

    def test_questionable_without_a_row_or_snaps_is_a_dnp(self) -> None:
        murray = _pl(
            pid="murray",
            name="Kyler Murray",
            position="QB",
            team="ARI",
            opponent="LAR",
            game="LAR@ARI",
            injury="Q",
            depth_rank=1,
            implied_total=22.0,
        )
        penix = _pl(
            pid="penix",
            name="Michael Penix",
            position="QB",
            team="ARI",
            opponent="LAR",
            game="LAR@ARI",
            injury="Q",
            depth_rank=2,
            implied_total=22.0,
        )
        flowers = _pl(
            pid="flowers",
            name="Zay Flowers",
            position="WR",
            team="ARI",
            opponent="LAR",
            game="LAR@ARI",
            injury="Q",
            depth_rank=1,
            implied_total=22.0,
        )
        played = _pl(
            pid="played",
            name="Played WR",
            position="WR",
            team="ARI",
            opponent="LAR",
            game="LAR@ARI",
            injury="Q",
            depth_rank=2,
            implied_total=22.0,
        )
        report = run_backtest(
            [murray, penix, flowers, played],
            [
                _actual("Kyler Murray", "ARI", 0.0, pass_attempts=0, offense_snaps=0),
                _actual("Zay Flowers", "ARI", 4.2, targets=3, offense_snaps=0),
                _actual("Played WR", "ARI", 8.0, targets=5, offense_snaps=40),
            ],
            season=2026,
            week=2,
            n=8,
            seed=1,
        )
        self.assertIn("questionable DNP: 3 (projection kept)", report.to_text())
        self.assertEqual(murray.depth_rank, 1)

    def test_nflverse_lines_file_filters_week_and_maps_teams(self) -> None:
        from nfl.lines import load_lines_csv

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lines.csv"
            path.write_text(
                "home_team,away_team,spread_line,total_line,home_implied_tt,away_implied_tt,season,week\n"
                "MIA,NE,-6.5,41.5,17.5,24.0,2026,2\n"
                "LA,JAX,3,47,25,22,2026,3\n",
                encoding="utf-8",
            )
            by_team = load_lines_csv(
                path,
                [("NE@MIA", "NE", "MIA")],
                season=2026,
                week=2,
            )
        self.assertAlmostEqual(by_team["MIA"].implied_home or 0, 17.5, places=2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rams.csv"
            path.write_text(
                "home_team,away_team,spread_line,total_line,season,week\n"
                "LA,JAX,3,47,2026,2\n",
                encoding="utf-8",
            )
            rams = load_lines_csv(
                path,
                [("JAC@LAR", "JAC", "LAR")],
                season=2026,
                week=2,
            )
        self.assertIn("LAR", rams)
        self.assertIn("JAC", rams)
        self.assertAlmostEqual(rams["LAR"].home_spread, -3.0, places=2)
        self.assertGreater(rams["LAR"].implied_home, rams["LAR"].implied_away)

    def test_nflverse_favorite_gets_the_higher_implied_total(self) -> None:
        from nfl.lines import LinesError, load_lines_csv

        header = (
            "game_id,gameday,gametime,home_team,away_team,spread_line,total_line,"
            "home_line,away_line,home_spread_odds,away_spread_odds,under_odds,over_odds,"
            "home_moneyline,away_moneyline,total,season,week"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.csv"
            path.write_text(
                header
                + "\n"
                + "2026_02_MIA_SF,2026-09-20,13:00,SF,MIA,12.5,44.5,"
                + "-12.5,12.5,-110,-110,-110,-110,-900,650,41,2026,2\n",
                encoding="utf-8",
            )
            by_team = load_lines_csv(
                path,
                [("MIA@SF", "MIA", "SF")],
                season=2026,
                week=2,
            )
        sf = by_team["SF"]
        self.assertAlmostEqual(sf.home_spread, -12.5, places=2)
        self.assertAlmostEqual(sf.total, 44.5, places=2)
        self.assertAlmostEqual(sf.implied_home, 28.5, places=2)
        self.assertAlmostEqual(sf.implied_away, 16.0, places=2)
        self.assertGreater(sf.implied_home, sf.implied_away)
        self.assertAlmostEqual(sf.home_moneyline or 0, -900)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "implied.csv"
            path.write_text(
                "home_team,away_team,spread_line,total_line,home_implied_tt,away_implied_tt,season,week\n"
                "SF,MIA,12.5,44.5,20.0,24.5,2026,2\n",
                encoding="utf-8",
            )
            implied = load_lines_csv(
                path,
                [("MIA@SF", "MIA", "SF")],
                season=2026,
                week=2,
            )
        self.assertAlmostEqual(implied["SF"].implied_home, 20.0, places=2)
        self.assertAlmostEqual(implied["SF"].implied_away, 24.5, places=2)
        self.assertGreater(implied["SF"].implied_away, implied["SF"].implied_home)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.csv"
            path.write_text(
                "game_id,home_team,away_team,spread_line,season,week\n"
                "2026_02_MIA_SF,SF,MIA,12.5,2026,2\n",
                encoding="utf-8",
            )
            with self.assertRaises(LinesError) as raised:
                load_lines_csv(
                    path,
                    [("MIA@SF", "MIA", "SF")],
                    season=2026,
                    week=2,
                )
        self.assertIn("missing required columns", str(raised.exception))
        self.assertIn("total_line", str(raised.exception))

    def test_unrecognized_line_column_is_a_hard_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "players.csv"
            lines_path = root / "lines.csv"
            csv_path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game,FPPG,Injury Indicator,Roster Position\n"
                "1,QB,Malik Willis,7000,MIA,NE,NE@MIA,,,\n",
                encoding="utf-8",
            )
            lines_path.write_text(
                "home,away,spread,total,extra_book\n"
                "MIA,NE,-6.5,41.5,pinnacle\n",
                encoding="utf-8",
            )
            with patch(
                "nfl.backtest.fetch_player_stats_weekly",
                return_value=([_actual("Malik Willis", "MIA", 14.0)], {}),
            ), patch(
                "nfl.backtest.fetch_dst_weekly",
                return_value=([], {}),
            ):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = main(
                        [
                            "--csv",
                            str(csv_path),
                            "--season",
                            "2026",
                            "--week",
                            "2",
                            "--lines-file",
                            str(lines_path),
                        ]
                    )
        self.assertEqual(code, 1)
        text = err.getvalue()
        self.assertIn("unrecognized line columns", text)
        self.assertIn("extra_book", text)
        self.assertIn("choke LINES:", text)

    def test_zero_matched_line_games_is_a_hard_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "players.csv"
            lines_path = root / "lines.csv"
            csv_path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game,FPPG,Injury Indicator,Roster Position\n"
                "1,QB,Malik Willis,7000,MIA,NE,NE@MIA,,,\n",
                encoding="utf-8",
            )
            lines_path.write_text(
                "home_team,away_team,spread_line,total_line,season,week\n"
                "DAL,PHI,-3,44,2026,3\n",
                encoding="utf-8",
            )
            with patch(
                "nfl.backtest.fetch_player_stats_weekly",
                return_value=([_actual("Malik Willis", "MIA", 14.0)], {}),
            ), patch(
                "nfl.backtest.fetch_dst_weekly",
                return_value=([], {}),
            ):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = main(
                        [
                            "--csv",
                            str(csv_path),
                            "--season",
                            "2026",
                            "--week",
                            "2",
                            "--lines-file",
                            str(lines_path),
                        ]
                    )
        self.assertEqual(code, 1)
        self.assertIn("0 games", err.getvalue())

    def test_unknown_closing_lines_is_loud_and_refuses_to_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "players.csv"
            csv_path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game,FPPG,Injury Indicator,Roster Position\n"
                "1,QB,QB One,8000,DET,NO,NO@DET,,,\n",
                encoding="utf-8",
            )
            with patch(
                "nfl.backtest.fetch_player_stats_weekly",
                return_value=([_actual("QB One", "DET", 11.0)], {}),
            ), patch(
                "nfl.backtest.fetch_dst_weekly",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_closing_lines",
                side_effect=GangstashDataError("gangstash closing_lines Unknown dataset"),
            ), patch(
                "nfl.backtest.fetch_game_lines",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_week_injuries",
                side_effect=GangstashDataError("gangstash injuries Unknown dataset"),
            ), patch(
                "nfl.backtest.fetch_depth_charts_weekly",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_depth_charts",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.load_optimizer_targets",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.load_optimizer_snaps",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_props",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.resolve_sim_inputs",
                return_value=(None, ""),
            ):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = main(
                        ["--csv", str(csv_path), "--season", "2026", "--week", "2", "--n", "5"]
                    )
        self.assertEqual(code, 1)
        text = err.getvalue()
        self.assertIn("closing_lines: not available (Unknown dataset)", text)
        self.assertIn("choke LINES:", text)
        self.assertNotIn("injuries", text)

    def test_depth_uses_the_snapshot_closest_before_the_week(self) -> None:
        from datetime import datetime, timezone

        from nfl.depth import depth_rows_for_backtest

        kickoff = datetime(2026, 9, 14, 17, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            early = root / "2026-09-08" / "depth_charts"
            late = root / "2026-09-20" / "depth_charts"
            early.mkdir(parents=True)
            late.mkdir(parents=True)
            early_row = {
                "player_name": "Drew Lock",
                "team_fd": "SEA",
                "pos_grp": "3WR 1TE",
                "pos_abb": "QB",
                "pos_rank": 1,
                "snapshot_at": "2026-09-08T12:00:00Z",
            }
            (early / "chart.json").write_text(
                json.dumps({"data": [early_row], "truncated": False}),
                encoding="utf-8",
            )
            (late / "chart.json").write_text(
                json.dumps(
                    {
                        "data": [
                            {
                                "player_name": "Sam Darnold",
                                "team_fd": "SEA",
                                "pos_grp": "3WR 1TE",
                                "pos_abb": "QB",
                                "pos_rank": 1,
                                "snapshot_at": "2026-09-20T12:00:00Z",
                            }
                        ],
                        "truncated": False,
                    }
                ),
                encoding="utf-8",
            )
            rows, note = depth_rows_for_backtest(
                [_depth("Sam Darnold", "SEA", "QB", 1)],
                season=2026,
                week=2,
                kickoff=kickoff,
                cache_root=root,
            )
            empty, current = depth_rows_for_backtest(
                [],
                season=2026,
                week=2,
                kickoff=kickoff,
                cache_root=root / "missing",
            )
        self.assertEqual(rows[0]["player_name"], "Drew Lock")
        self.assertIn("snapshot", note)
        self.assertNotIn("no week column", note)
        self.assertEqual(empty, [])
        self.assertIn("current chart", current)
        self.assertNotIn("no week column", current)

    def test_depth_source_uses_weekly_then_falls_back(self) -> None:
        weekly = [_depth("Cooper Rush", "ATL", "QB", 1)]
        weekly[0]["player_id"] = ""
        current = [_depth("Michael Penix Jr.", "ATL", "QB", 1)]
        missing: list[str] = []
        with patch(
            "nfl.backtest.fetch_depth_charts_weekly",
            return_value=(weekly, {}),
        ) as weekly_fetch, patch(
            "nfl.backtest.fetch_depth_charts",
            side_effect=AssertionError("current chart"),
        ):
            rows, note = select_backtest_depth(
                season=2026, week=2, kickoff=None, missing=missing
            )
        weekly_fetch.assert_called_once()
        self.assertEqual(weekly_fetch.call_args.kwargs["season"], 2026)
        self.assertEqual(weekly_fetch.call_args.kwargs["week"], 2)
        self.assertEqual(rows[0]["player_name"], "Cooper Rush")
        self.assertIn("depth_charts_weekly", note)
        self.assertEqual(missing, [])
        chart, _chart_note = _depth_table(rows, season=2026, week=2)
        attached, _stats = attach_depth_ranks(
            [_pl(pid="fd-rush", name="Cooper Rush", team="ATL", position="QB")],
            chart,
        )
        self.assertEqual(attached[0].depth_rank, 1)
        fallback_missing: list[str] = []
        with patch(
            "nfl.backtest.fetch_depth_charts_weekly",
            return_value=([], {}),
        ), patch(
            "nfl.backtest.fetch_depth_charts",
            return_value=(current, {}),
        ) as charts:
            fallback, fallback_note = select_backtest_depth(
                season=2026, week=2, kickoff=None, missing=fallback_missing
            )
        charts.assert_called_once()
        self.assertEqual(fallback[0]["player_name"], "Michael Penix Jr.")
        self.assertIn("depth_charts_weekly", fallback_missing)
        self.assertIn("current chart", fallback_note)


def week2_fixture_report() -> str:
    """Offline week-2 board vs sim. No API key."""
    willis = _pl(
        pid="willis",
        name="Malik Willis",
        position="QB",
        team="MIA",
        opponent="NE",
        game="NE@MIA",
        salary=7000,
        depth_rank=None,
        implied_total=None,
    )
    backup = _pl(
        pid="backup",
        name="Backup QB",
        position="QB",
        team="MIA",
        opponent="NE",
        game="NE@MIA",
        salary=5000,
        depth_rank=None,
        implied_total=None,
    )
    wr1 = _pl(
        pid="wr1",
        name="MIA WR1",
        position="WR",
        team="MIA",
        opponent="NE",
        game="NE@MIA",
        depth_rank=None,
        implied_total=None,
    )
    wr2 = _pl(
        pid="wr2",
        name="MIA WR2",
        position="WR",
        team="MIA",
        opponent="NE",
        game="NE@MIA",
        depth_rank=None,
        implied_total=None,
    )
    rb = _pl(
        pid="rb",
        name="MIA RB",
        position="RB",
        team="MIA",
        opponent="NE",
        game="NE@MIA",
        depth_rank=None,
        implied_total=None,
    )
    defense = _pl(
        pid="def",
        name="Miami Dolphins",
        position="D",
        team="MIA",
        opponent="NE",
        game="NE@MIA",
        depth_rank=None,
        implied_total=None,
    )
    mariota = _pl(
        pid="mariota",
        name="Marcus Mariota",
        position="QB",
        team="WAS",
        opponent="NYG",
        game="NYG@WAS",
        salary=6500,
        depth_rank=None,
        implied_total=None,
    )
    daniels = _pl(
        pid="daniels",
        name="Jayden Daniels",
        position="QB",
        team="WAS",
        opponent="NYG",
        game="NYG@WAS",
        salary=8500,
        depth_rank=None,
        implied_total=None,
    )
    was_wr = _pl(
        pid="waswr",
        name="WAS WR1",
        position="WR",
        team="WAS",
        opponent="NYG",
        game="NYG@WAS",
        depth_rank=None,
        implied_total=None,
    )
    pool, _gaps, _notes = apply_week_context(
        [willis, backup, wr1, wr2, rb, defense, mariota, daniels, was_wr],
        season=2026,
        week=2,
        line_rows=[
            {
                "home_team_fd": "MIA",
                "away_team_fd": "NE",
                "season": 2026,
                "week": 2,
                "home_implied_total": 17.5,
                "away_implied_total": 24.0,
            },
            {
                "home_team_fd": "WAS",
                "away_team_fd": "NYG",
                "season": 2026,
                "week": 2,
                "spread": -1.0,
                "total": 43.0,
            },
        ],
        depth_raw=[
            _depth("Malik Willis", "MIA", "QB", 1),
            _depth("Backup QB", "MIA", "QB", 2),
            _depth("MIA WR1", "MIA", "WR", 1),
            _depth("MIA WR2", "MIA", "WR", 2),
            _depth("MIA RB", "MIA", "RB", 1),
            _depth("Marcus Mariota", "WAS", "QB", 2),
            _depth("Jayden Daniels", "WAS", "QB", 1),
            _depth("WAS WR1", "WAS", "WR", 1),
        ],
        injury_raw=[
            {
                "player_name": "Jayden Daniels",
                "team_fd": "WAS",
                "status": "Out",
                "season": 2026,
                "week": 2,
            }
        ],
    )
    actuals = [
        _actual("Malik Willis", "MIA", 14.2, pass_attempts=28),
        _actual("Backup QB", "MIA", 0.0, pass_attempts=0),
        _actual("MIA WR1", "MIA", 11.0, targets=6),
        _actual("MIA WR2", "MIA", 6.4, targets=3),
        _actual("MIA RB", "MIA", 9.6, carries=14),
        _actual("Miami Dolphins", "MIA", 7.0),
        _actual("Marcus Mariota", "WAS", 15.1, pass_attempts=32),
        _actual("WAS WR1", "WAS", 10.2, targets=5),
    ]
    report = run_backtest(
        pool,
        actuals,
        season=2026,
        week=2,
        n=400,
        seed=1,
        missing=["props", "targets", "snaps", "sim_inputs"],
    )
    return report.to_text()


class LiveBacktestFixesTest(unittest.TestCase):
    def test_null_kickoff_uses_sunday_and_drops_postgame_props(self) -> None:
        from datetime import datetime, timezone

        from nfl.backtest import earliest_kickoff, prop_kickoff

        rows = [{"home_team": "SF", "away_team": "MIA", "kickoff": None}]
        self.assertIsNone(earliest_kickoff(rows))
        self.assertEqual(
            earliest_kickoff([{"gameday": "2026-09-20"}]),
            datetime(2026, 9, 20, 17, 0, tzinfo=timezone.utc),
        )
        stamp, note = prop_kickoff(rows, season=2026, week=2)
        self.assertEqual(stamp, datetime(2026, 9, 20, 17, 0, tzinfo=timezone.utc))
        self.assertIn("week schedule", note or "")
        kept = props_for_week(
            [
                {
                    "season": 2026,
                    "week": 2,
                    "scraped_at": "2026-09-20T12:00:00Z",
                    "player_name": "Early",
                },
                {
                    "season": 2026,
                    "week": 2,
                    "scraped_at": "2026-09-21T12:00:00Z",
                    "player_name": "Late",
                },
            ],
            season=2026,
            week=2,
            kickoff=stamp,
        )
        self.assertEqual([row["player_name"] for row in kept], ["Early"])

    def test_dst_weekly_is_a_def_row(self) -> None:
        defense = _pl(
            pid="dst:DET",
            name="Detroit Lions",
            position="D",
            team="DET",
            salary=0,
            fppg=None,
        )
        dst = {
            "season": 2026,
            "week": 2,
            "team": "DET",
            "fd_points": 9.0,
            "sacks": 3,
            "ints": 1,
            "points_allowed": 17,
            "points_allowed_fd": 4,
        }
        report = run_backtest(
            [defense],
            [dst],
            season=2026,
            week=2,
            n=20,
            seed=1,
            sim_inputs=None,
        )
        positions = [row.position for row in report.rows]
        self.assertIn("DEF", positions)
        def_row = next(row for row in report.rows if row.position == "DEF")
        self.assertEqual(def_row.n, 1)
        self.assertIn("DEF", report.to_text())
        self.assertIn("sim efficiency: placeholder", report.to_text())
        starter_pos = [row.position for row in report.starters]
        self.assertIn("DEF", starter_pos)

    def test_unparseable_closing_falls_back_without_blaming_player_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "players.csv"
            csv_path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game,FPPG,Injury Indicator,Roster Position\n"
                "1,QB,Jared Goff,8000,DET,NO,NO@DET,,,\n",
                encoding="utf-8",
            )
            closing = [
                {
                    "season": 2026,
                    "week": 2,
                    "home_team": "CAR",
                    "away_team": "ATL",
                    "kickoff": None,
                }
            ]
            game = [
                {
                    "home_team_fd": "DET",
                    "away_team_fd": "NO",
                    "season": 2026,
                    "week": 2,
                    "spread": -3.0,
                    "total": 47.0,
                }
            ]
            with patch(
                "nfl.backtest.fetch_player_stats_weekly",
                return_value=([_actual("Jared Goff", "DET", 18.0, pass_attempts=30)], {}),
            ), patch(
                "nfl.backtest.fetch_dst_weekly",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_closing_lines",
                return_value=(closing, {}),
            ), patch(
                "nfl.backtest.fetch_game_lines",
                return_value=(game, {}),
            ), patch(
                "nfl.backtest.fetch_week_injuries",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_depth_charts_weekly",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_depth_charts",
                return_value=([_depth("Jared Goff", "DET", "QB", 1)], {}),
            ), patch(
                "nfl.backtest.load_optimizer_targets",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.load_optimizer_snaps",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.fetch_props",
                return_value=([], {}),
            ), patch(
                "nfl.backtest.resolve_sim_inputs",
                return_value=(None, ""),
            ), patch("nfl.gangstash.http_json", side_effect=AssertionError("network")):
                err = io.StringIO()
                buf = io.StringIO()
                with redirect_stderr(err), redirect_stdout(buf):
                    code = main(
                        ["--csv", str(csv_path), "--season", "2026", "--week", "2", "--n", "15"]
                    )
        self.assertEqual(code, 0)
        self.assertNotIn("PLAYER_STATS_WEEKLY", err.getvalue())
        self.assertIn("QB", buf.getvalue())
        self.assertIn("kickoff missing", buf.getvalue())

    def test_no_csv_pool_scores_starters_hindsight_and_def(self) -> None:
        closing = [
            {
                "season": 2026,
                "week": 2,
                "home_team": "DET",
                "away_team": "NO",
                "home_line": -3.0,
                "total": 47.0,
                "implied_home_total": 25.0,
                "implied_away_total": 22.0,
                "is_final": True,
                "kickoff": None,
            }
        ]
        depth = [
            _depth("Jared Goff", "DET", "QB", 1),
            _depth("Amon-Ra St. Brown", "DET", "WR", 1),
            _depth("Jahmyr Gibbs", "DET", "RB", 1),
            _depth("Sam LaPorta", "DET", "TE", 1),
            _depth("Derek Carr", "NO", "QB", 1),
            _depth("Chris Olave", "NO", "WR", 1),
            _depth("Alvin Kamara", "NO", "RB", 1),
            _depth("Juwan Johnson", "NO", "TE", 1),
        ]
        actual = [
            _actual("Jared Goff", "DET", 18.0, pass_attempts=32, offense_snaps=60),
            _actual("Amon-Ra St. Brown", "DET", 14.0, targets=8),
        ]
        dst = [
            {
                "season": 2026,
                "week": 2,
                "team": "DET",
                "fd_points": 8.0,
                "sacks": 2,
                "points_allowed": 20,
            }
        ]
        with patch(
            "nfl.backtest.fetch_player_stats_weekly",
            return_value=(actual, {}),
        ), patch(
            "nfl.backtest.fetch_dst_weekly",
            return_value=(dst, {}),
        ), patch(
            "nfl.backtest.fetch_closing_lines",
            return_value=(closing, {}),
        ), patch(
            "nfl.backtest.fetch_game_lines",
            side_effect=AssertionError("game_lines"),
        ), patch(
            "nfl.backtest.fetch_week_injuries",
            return_value=([], {}),
        ), patch(
            "nfl.backtest.fetch_depth_charts_weekly",
            return_value=([], {}),
        ), patch(
            "nfl.backtest.fetch_depth_charts",
            return_value=(depth, {}),
        ), patch(
            "nfl.backtest.load_optimizer_targets",
            return_value=([], {}),
        ), patch(
            "nfl.backtest.load_optimizer_snaps",
            return_value=([], {}),
        ), patch(
            "nfl.backtest.fetch_props",
            return_value=([], {}),
        ), patch(
            "nfl.backtest.resolve_sim_inputs",
            return_value=(None, ""),
        ) as sim, patch("nfl.gangstash.http_json", side_effect=AssertionError("network")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["--season", "2026", "--week", "2", "--n", "20"])
        self.assertEqual(code, 0)
        self.assertEqual(sim.call_args.kwargs["team_stats_scope"], "weekly")
        self.assertEqual(sim.call_args.kwargs["weeks"], [1])
        text = buf.getvalue()
        self.assertIn("salary omitted", text)
        self.assertIn("value blank", text)
        self.assertIn("pool: full", text)
        self.assertIn("pool: starters", text)
        self.assertIn("pool: hindsight", text)
        self.assertIn("DEF", text)
        self.assertIn("QB", text)

    def test_no_csv_names_a_failed_injury_fetch(self) -> None:
        closing = [
            {
                "season": 2026,
                "week": 2,
                "home_team": "DET",
                "away_team": "NO",
                "home_line": -3.0,
                "total": 47.0,
                "implied_home_total": 25.0,
                "implied_away_total": 22.0,
                "kickoff": "2026-09-14T17:00:00Z",
            }
        ]
        depth = [
            _depth("Jared Goff", "DET", "QB", 1),
            _depth("Derek Carr", "NO", "QB", 1),
        ]
        with patch(
            "nfl.backtest.fetch_player_stats_weekly",
            return_value=([_actual("Jared Goff", "DET", 18.0, pass_attempts=30)], {}),
        ), patch(
            "nfl.backtest.fetch_dst_weekly",
            return_value=([], {}),
        ), patch(
            "nfl.backtest.fetch_closing_lines",
            return_value=(closing, {}),
        ), patch(
            "nfl.backtest.fetch_game_lines",
            side_effect=AssertionError("game_lines"),
        ), patch(
            "nfl.backtest.fetch_week_injuries",
            side_effect=GangstashDataError("gangstash injuries down"),
        ), patch(
            "nfl.backtest.fetch_depth_charts_weekly",
            return_value=([], {}),
        ), patch(
            "nfl.backtest.fetch_depth_charts",
            return_value=(depth, {}),
        ), patch(
            "nfl.backtest.load_optimizer_targets",
            return_value=([], {}),
        ), patch(
            "nfl.backtest.load_optimizer_snaps",
            return_value=([], {}),
        ), patch(
            "nfl.backtest.fetch_props",
            return_value=([], {}),
        ), patch(
            "nfl.backtest.resolve_sim_inputs",
            return_value=(None, ""),
        ), patch("nfl.gangstash.http_json", side_effect=AssertionError("network")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["--season", "2026", "--week", "2", "--n", "5"])
        self.assertEqual(code, 0)
        missing = buf.getvalue().split("missing:", 1)[1].split("\n", 1)[0]
        self.assertIn("injuries", missing)


if __name__ == "__main__":
    unittest.main()
