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
    apply_week_context,
    index_actuals,
    is_starter,
    main,
    prior_weeks,
    run_backtest,
    summarize_errors,
)
from nfl.props import props_for_week
from nfl.sim import simulate_games
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
                "nfl.backtest.fetch_closing_lines",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_game_lines",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_week_injuries",
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
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main(
                        ["--csv", str(csv_path), "--season", "2026", "--week", "2", "--n", "20"]
                    )
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn(
            "missing: closing_lines, game_lines, depth, injuries, targets, snaps, props, sim_inputs",
            text,
        )
        self.assertIn("pool: full", text)
        self.assertIn("pool: starters", text)
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
                "nfl.backtest.fetch_closing_lines",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_game_lines",
                return_value=([], {"live": False}),
            ), patch(
                "nfl.backtest.fetch_week_injuries",
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
                    ["--csv", str(csv_path), "--season", "2026", "--week", "2", "--n", "5"]
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
                "nfl.backtest.fetch_closing_lines",
                side_effect=AssertionError("network"),
            ), patch(
                "nfl.backtest.fetch_game_lines",
                side_effect=AssertionError("network"),
            ), patch(
                "nfl.backtest.fetch_week_injuries",
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
        names = [pl.name for pl in players]
        self.assertNotIn("Sam Darnold", names)
        self.assertIn("Drew Lock", names)
        self.assertNotIn("injuries", gaps)
        gs = simulate_games(players, n=40, seed=1)
        self.assertGreater(gs.by_pid["lock"].mean, 12.0)

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
        self.assertFalse(is_starter(bench, actuals[1]))
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
        self.assertEqual(report.starters_n, 1)
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
        self.assertEqual(only.n, 1)


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


if __name__ == "__main__":
    unittest.main()
