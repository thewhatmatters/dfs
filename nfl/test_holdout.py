"""Holdout measurement. Mocks only; no network."""

from __future__ import annotations

import io
import json
import random
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from nfl.backtest import _depth_table
from nfl.gangstash import GangstashDataError
from nfl.gangstash_data import (
    GangstashDepthSlot,
    fetch_props_closing,
    parse_depth_slot,
    uniquify_depth_ranks,
)
from nfl.holdout import (
    WeekLoad,
    _calibration_block,
    _game_actuals,
    _pair_roles,
    _scale_pace,
    _scale_rz,
    closing_prop_points,
    draw_percentile,
    fetch_prop_rows,
    format_report,
    index_prop_lines,
    load_week,
    main,
    parse_seasons,
    parse_weeks,
    pearson,
    perturb,
    pick_sensitivity_week,
    run_holdout,
    spearman,
)
from nfl.lines import LinesError
from nfl.players import Player
from nfl.projections import week1_score
from nfl.sim import simulate_games
from nfl.sim_efficiency import OpportunityCount, build_efficiency
from nfl.sim_inputs import SimInputs, TargetWeek, TeamStat


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("pid", "p"),
        name=kw.get("name", "Player"),
        position=kw.get("position", "WR"),
        salary=0,
        team=kw.get("team", "DET"),
        opponent=kw.get("opponent", "NO"),
        game=kw.get("game", "NO@DET"),
        fppg=None,
        injury="",
        roster_position="",
        implied_total=kw.get("implied_total", 24.0),
        implied_opp=20.0,
        depth_rank=kw.get("depth_rank", 1),
        total=kw.get("total", 44.0),
        spread=kw.get("spread", -3.0),
    )
    fields.update(kw)
    if fields.get("objective") is None:
        fields["objective"] = week1_score(
            fields.get("implied_total") or 0.0,
            depth_rank=fields.get("depth_rank"),
            position=fields["position"],
        )
    allowed = set(Player.__dataclass_fields__)
    return Player(**{key: value for key, value in fields.items() if key in allowed})


def _actual(name: str, team: str, points: float) -> dict:
    return {
        "player_name": name,
        "team_fd": team,
        "fd_points": points,
        "season": 2025,
        "week": 2,
    }


def _dst(team: str, fd: float, allowed: float) -> dict:
    return {
        "season": 2025,
        "week": 2,
        "team": team,
        "fd_points": fd,
        "points_allowed": allowed,
    }


class ParseTest(unittest.TestCase):
    def test_week_range_and_list(self) -> None:
        self.assertEqual(parse_weeks("1-18"), list(range(1, 19)))
        self.assertEqual(parse_weeks("2"), [2])
        self.assertEqual(parse_weeks("1,2,5"), [1, 2, 5])
        self.assertEqual(parse_weeks("1-3,5"), [1, 2, 3, 5])

    def test_week_rejects_zero_and_backwards(self) -> None:
        with self.assertRaises(ValueError):
            parse_weeks("0")
        with self.assertRaises(ValueError):
            parse_weeks("5-1")
        with self.assertRaises(ValueError):
            parse_weeks("19")

    def test_seasons(self) -> None:
        self.assertEqual(parse_seasons("2024,2025", None), [2024, 2025])
        self.assertEqual(parse_seasons(None, 2026), [2026])
        self.assertEqual(parse_seasons("2024", 2025), [2024, 2025])

    def test_sensitivity_week_skips_week_1(self) -> None:
        self.assertEqual(pick_sensitivity_week([1, 2, 18], None), 18)
        self.assertEqual(pick_sensitivity_week([1], None), 1)
        self.assertEqual(pick_sensitivity_week([1, 2], 2), 2)


class DepthTieTest(unittest.TestCase):
    def test_null_pos_slot_parses(self) -> None:
        slot = parse_depth_slot(
            {
                "player_name": "Zed Zee",
                "team_fd": "DET",
                "pos_grp": "3WR 1TE",
                "pos_abb": "WR",
                "pos_rank": 1,
                "pos_slot": None,
                "chart_format": "nflverse_weekly",
            }
        )
        self.assertIsNotNone(slot)
        assert slot is not None
        self.assertIsNone(slot.pos_slot)
        self.assertEqual(slot.chart_format, "nflverse_weekly")

    def test_tied_ranks_become_unique_and_espn_ranks_stay(self) -> None:
        tied = [
            GangstashDepthSlot("DET", "WR", 1, "Zed Zee", chart_format="nflverse_weekly"),
            GangstashDepthSlot("DET", "WR", 1, "Amy Ace", chart_format="nflverse_weekly"),
            GangstashDepthSlot("DET", "RB", 1, "Back"),
            GangstashDepthSlot("DET", "RB", 5, "Deep"),
        ]
        out = uniquify_depth_ranks(tied)
        by_name = {slot.player_name: slot.rank for slot in out}
        self.assertEqual(by_name["Amy Ace"], 1)
        self.assertEqual(by_name["Zed Zee"], 2)
        self.assertEqual(by_name["Back"], 1)
        self.assertEqual(by_name["Deep"], 5)

    def test_depth_table_rewrites_ties(self) -> None:
        rows = [
            {
                "player_name": "Zed Zee",
                "team_fd": "DET",
                "pos_grp": "3WR 1TE",
                "pos_abb": "WR",
                "pos_rank": 1,
                "pos_slot": None,
                "chart_format": "nflverse_weekly",
                "season": 2024,
                "week": 5,
            },
            {
                "player_name": "Amy Ace",
                "team_fd": "DET",
                "pos_grp": "3WR 1TE",
                "pos_abb": "WR",
                "pos_rank": 1,
                "pos_slot": None,
                "chart_format": "nflverse_weekly",
                "season": 2024,
                "week": 5,
            },
        ]
        chart, _note = _depth_table(rows, season=2024, week=5)
        by_name = {row.name: row.rank for row in chart}
        self.assertEqual(by_name, {"Amy Ace": 1, "Zed Zee": 2})


class MetricTest(unittest.TestCase):
    def test_calibration_cuts(self) -> None:
        rows = []
        for i in range(10):
            rows.append(("WR", i / 10 + 0.05, i >= 1 and i <= 8))
        block = _calibration_block(rows)
        below = block["WR"]["below"]
        self.assertAlmostEqual(below["10"], 0.1)
        self.assertAlmostEqual(below["20"], 0.2)
        self.assertAlmostEqual(below["90"], 0.9)
        self.assertAlmostEqual(block["WR"]["p10_p90"], 0.8)
        draws = tuple(range(10))
        self.assertAlmostEqual(draw_percentile(draws, 0), 0.1)
        self.assertTrue(draw_percentile(draws, 9) > 0.9)

    def test_pearson_and_spearman(self) -> None:
        self.assertAlmostEqual(pearson([1, 2, 3, 4], [1, 2, 3, 4]), 1.0)
        self.assertAlmostEqual(spearman([1, 2, 3], [3, 2, 1]), -1.0)
        self.assertIsNone(pearson([1, 1, 1], [1, 2, 3]))

    def test_game_margin_uses_points_allowed(self) -> None:
        players = [
            _pl(pid="qb", name="Home QB", position="QB", team="DET", opponent="NO", spread=-3),
            _pl(pid="aqb", name="Away QB", position="QB", team="NO", opponent="DET", spread=3),
        ]
        games = _game_actuals(
            players,
            {"DET": 20.0, "NO": 27.0},
        )
        self.assertEqual(len(games), 1)
        self.assertAlmostEqual(games[0]["margin"], 7.0)
        self.assertAlmostEqual(games[0]["total"], 47.0)
        self.assertAlmostEqual(games[0]["closing_margin"], 3.0)

    def test_pair_roles(self) -> None:
        players = [
            _pl(pid="qb", name="QB", position="QB", team="DET", opponent="NO"),
            _pl(pid="wr1", name="WR1", position="WR", team="DET", depth_rank=1),
            _pl(pid="wr2", name="WR2", position="WR", team="DET", depth_rank=2),
            _pl(pid="te", name="TE", position="TE", team="DET"),
            _pl(pid="rb", name="RB", position="RB", team="DET"),
            _pl(pid="def", name="Lions", position="D", team="DET"),
            _pl(
                pid="oqb",
                name="OQB",
                position="QB",
                team="NO",
                opponent="DET",
                game="NO@DET",
            ),
            _pl(pid="odef", name="Saints", position="D", team="NO", opponent="DET"),
        ]
        roles = _pair_roles(players, "DET")
        self.assertEqual(roles["QB-WR1"][1].pid, "wr1")
        self.assertEqual(roles["WR1-WR2"][1].pid, "wr2")
        self.assertEqual(roles["QB-TE1"][1].pid, "te")
        self.assertEqual(roles["QB-RB1"][1].pid, "rb")
        self.assertEqual(roles["QB-oppQB"][1].pid, "oqb")
        self.assertEqual(roles["QB-oppDEF"][1].pid, "odef")


class PropTest(unittest.TestCase):
    def test_closing_points_include_ppr_bonus_and_tds(self) -> None:
        pts = closing_prop_points(
            {
                "pass_yds": 300.0,
                "pass_tds": 2.0,
                "rush_yds": 100.0,
                "receptions": 10.0,
                "rush_tds": 1.0,
                "rec_tds": 1.0,
            }
        )
        # 300*0.04+3 + 2*4 + 100*0.1+3 + 10*0.5 + 6 + 6
        self.assertAlmostEqual(pts, 12 + 3 + 8 + 10 + 3 + 5 + 6 + 6)

    def test_query_params(self) -> None:
        with patch(
            "nfl.gangstash_data.fetch_dataset", return_value=([], {"live": False})
        ) as fetch:
            fetch_props_closing(
                season=2026,
                week=3,
                player="A.J. Brown",
                team="phi",
                prop="Pass YDs",
            )
        self.assertEqual(fetch.call_args.args[0], "props_closing")
        params = fetch.call_args.args[1]
        self.assertEqual(params["season"], "2026")
        self.assertEqual(params["week"], "3")
        self.assertEqual(params["player"], "A.J. Brown")
        self.assertEqual(params["team"], "PHI")
        self.assertEqual(params["prop"], "Pass YDs")

    def test_season_is_required(self) -> None:
        with self.assertRaises(GangstashDataError):
            fetch_props_closing(season=0)

    def test_unknown_dataset_is_skipped(self) -> None:
        with patch(
            "nfl.holdout.fetch_props_closing",
            side_effect=GangstashDataError("Unknown dataset props_closing"),
        ):
            rows, note = fetch_prop_rows(2024)
        self.assertEqual(rows, [])
        self.assertIn("skipped", note)

    def test_index_ignores_weeks_outside_the_request(self) -> None:
        rows = [
            {
                "season": 2026,
                "week": 3,
                "player_name": "A.J. Brown",
                "team": "PHI",
                "prop": "Rec YDs",
                "line": 80.5,
            },
            {
                "season": 2026,
                "week": 2,
                "player_name": "A.J. Brown",
                "team": "PHI",
                "prop": "Rec YDs",
                "line": 70,
            },
        ]
        indexed = index_prop_lines(rows, {3})
        self.assertEqual(len(indexed), 1)
        only = next(iter(indexed.values()))
        self.assertAlmostEqual(only["rec_yds"], 80.5)


class SensitivityInputTest(unittest.TestCase):
    def test_pace_and_red_zone_scale_copies(self) -> None:
        row = TeamStat(
            team_fd="DET",
            plays_per_game=70.0,
            seconds_per_play=30.0,
            red_zone_td_rate=0.50,
        )
        paced = _scale_pace(row, 1.1)
        self.assertAlmostEqual(paced.plays_per_game, 77.0)
        self.assertAlmostEqual(paced.seconds_per_play, 30.0 / 1.1)
        self.assertAlmostEqual(row.plays_per_game, 70.0)
        zoned = _scale_rz(row, 0.9)
        self.assertAlmostEqual(zoned.red_zone_td_rate, 0.45)
        self.assertAlmostEqual(row.red_zone_td_rate, 0.50)

    def test_pass_multiplier_scale_changes_points_and_not_the_original(self) -> None:
        player = _pl(position="WR", depth_rank=1)
        base = build_efficiency("data", SimInputs(), before_week=2)
        _bundle, scaled = perturb(
            "pass_multiplier",
            1.1,
            SimInputs(),
            [player],
            before_week=2,
        )
        self.assertAlmostEqual(base.pass_multiplier("NO"), 1.0)
        self.assertAlmostEqual(scaled.pass_multiplier("NO"), 1.1)
        count = OpportunityCount(targets=8.0)
        base_pts = []
        scaled_pts = []
        for i in range(20):
            base_pts.append(base.points(random.Random(i), player, count))
            scaled_pts.append(scaled.points(random.Random(i), player, count))
        self.assertGreater(sum(scaled_pts), sum(base_pts))

    def test_starter_target_share_scales_without_touching_the_backup(self) -> None:
        inputs = SimInputs(
            targets=(
                TargetWeek(2025, 1, "WR", "Alpha", "DET", 8, target_share=0.30),
                TargetWeek(2025, 1, "WR", "Backup", "DET", 4, target_share=0.10),
            )
        )
        players = [
            _pl(pid="a", name="Alpha", depth_rank=1),
            _pl(pid="b", name="Backup", depth_rank=2),
        ]
        bundle, _model = perturb(
            "target_carry_shares",
            1.1,
            inputs,
            players,
            before_week=2,
        )
        self.assertAlmostEqual(bundle.targets[0].target_share, 0.33)
        self.assertAlmostEqual(bundle.targets[1].target_share, 0.10)
        self.assertAlmostEqual(inputs.targets[0].target_share, 0.30)


class WeekOneSeedTest(unittest.TestCase):
    def test_default_does_not_request_the_prior_season(self) -> None:
        def fake_load(players, **kwargs):
            self.assertEqual(kwargs["week"], 1)
            self.assertIsNone(players)
            return [], [], SimInputs(), [], []

        with patch("nfl.holdout._load_live", side_effect=fake_load), patch(
            "nfl.holdout.resolve_sim_inputs",
            side_effect=AssertionError("prior season was requested"),
        ):
            loaded = load_week(2025, 1, seed_prior=False)
        self.assertIsNotNone(loaded.sim_inputs)

    def test_flag_requests_prior_season_week_18(self) -> None:
        calls = []

        def fake_resolve(**kwargs):
            calls.append(kwargs)
            return SimInputs(team_stats=(TeamStat(team_fd="DET", n=10),)), "seeded"

        with patch(
            "nfl.holdout._load_live",
            return_value=([], [], None, [], []),
        ), patch("nfl.holdout.resolve_sim_inputs", side_effect=fake_resolve):
            loaded = load_week(2025, 1, seed_prior=True)
        self.assertEqual(calls[0]["season"], 2024)
        self.assertEqual(calls[0]["weeks"], [18])
        self.assertEqual(loaded.sim_inputs.team_stats[0].team_fd, "DET")


class ReportTest(unittest.TestCase):
    def _load(self, season, week, *, seed_prior=False):
        self.assertFalse(seed_prior)
        players = [
            _pl(pid="qb", name="Home QB", position="QB", team="DET", opponent="NO"),
            _pl(pid="wr", name="Home WR", position="WR", team="DET", opponent="NO"),
            _pl(
                pid="oqb",
                name="Away QB",
                position="QB",
                team="NO",
                opponent="DET",
                spread=3.0,
            ),
            _pl(
                pid="owr",
                name="Away WR",
                position="WR",
                team="NO",
                opponent="DET",
                spread=3.0,
            ),
            _pl(pid="def", name="Lions", position="D", team="DET", opponent="NO"),
        ]
        actuals = [
            _actual("Home QB", "DET", 18.0),
            _actual("Home WR", "DET", 12.0),
            _actual("Away QB", "NO", 14.0),
            _actual("Away WR", "NO", 9.0),
            _dst("DET", 8.0, 17.0),
            _dst("NO", 6.0, 24.0),
        ]
        return WeekLoad(players, actuals, SimInputs(), [], ["pool: depth charts"])

    def test_report_shape_and_empty_props(self) -> None:
        report = run_holdout(
            [2025],
            [2],
            n=6,
            seed=1,
            load=self._load,
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        self.assertFalse(report["props"]["available"])
        self.assertIn("no rows", report["props"]["notes"][0])
        self.assertGreater(report["errors"]["full"]["board"]["QB"]["n"], 0)
        self.assertGreater(report["errors"]["starters"]["board"]["QB"]["n"], 0)
        self.assertIn("sim_data", report["errors"]["starters"])
        self.assertIn("sim_placeholder", report["errors"]["full"])
        self.assertIn("10", report["calibration"]["sim_data"]["full"]["QB"]["below"])
        self.assertEqual(report["game_variance"]["n_games"], 1)
        self.assertEqual(report["correlations"]["QB-WR1"]["n_sim_data"], 2)
        self.assertEqual(report["correlations"]["QB-oppQB"]["n_actual"], 1)
        self.assertEqual(report["sensitivity"]["week"], 2)
        self.assertIn("pass_multiplier", report["sensitivity"]["inputs"])
        text = format_report(report)
        self.assertIn("pool: starters", text)
        self.assertIn("pool: full", text)
        self.assertIn("props_closing: skipped", text)
        self.assertIn("calibration", text)

    def test_props_mae_sits_next_to_the_sim(self) -> None:
        def props(_season):
            return (
                [
                    {
                        "season": 2026,
                        "week": 2,
                        "player_name": "Home WR",
                        "team": "DET",
                        "prop": "Rec YDs",
                        "line": 80,
                    },
                    {
                        "season": 2026,
                        "week": 2,
                        "player_name": "Home WR",
                        "team": "DET",
                        "prop": "Recs",
                        "line": 6,
                    },
                ],
                "",
            )

        report = run_holdout(
            [2025],
            [2],
            n=4,
            seed=1,
            run_sensitivity=False,
            load=self._load,
            prop_fetch=props,
        )
        wr = report["props"]["by_position"]["WR"]
        self.assertEqual(wr["props"]["n"], 1)
        self.assertEqual(wr["sim_data"]["n"], 1)
        self.assertIsNotNone(wr["props"]["mae"])

    def test_help_runs(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "nfl.holdout", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--sensitivity-week", proc.stdout)
        self.assertIn("±10%", proc.stdout)

    def test_main_writes_json_without_network(self) -> None:
        def boom(*_args, **_kwargs):
            raise AssertionError("network")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "holdout.json"
            with patch("nfl.holdout._load_live", side_effect=boom), patch(
                "nfl.holdout.fetch_props_closing", side_effect=boom
            ), patch("nfl.holdout.load_week", side_effect=self._load), patch(
                "nfl.holdout.fetch_prop_rows",
                return_value=([], "props_closing: no rows"),
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(
                    ["--season", "2025", "--weeks", "2", "--n", "4", "--json-out", str(path)]
                )
            self.assertEqual(code, 0)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["seasons"], [2025])
        self.assertEqual(payload["weeks"], [2])
        self.assertIn("errors", payload)
        self.assertIn("calibration", payload)
        self.assertIn("game_variance", payload)
        self.assertIn("correlations", payload)
        self.assertIn("ranking", payload)
        self.assertIn("sensitivity", payload)
        self.assertIn("props", payload)

    def test_main_week1_does_not_seed_unless_asked(self) -> None:
        def boom(*_args, **_kwargs):
            raise AssertionError("network")

        seen = []

        def fake_load(season, week, *, seed_prior=False):
            seen.append(seed_prior)
            return self._load(season, week, seed_prior=False)

        with patch("nfl.holdout.fetch_props_closing", side_effect=boom), patch(
            "nfl.holdout._load_live", side_effect=boom
        ), patch("nfl.holdout.resolve_sim_inputs", side_effect=boom), patch(
            "nfl.holdout.load_week", side_effect=fake_load
        ), patch(
            "nfl.holdout.fetch_prop_rows",
            return_value=([], "props_closing: no rows"),
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["--season", "2025", "--weeks", "1", "--n", "4"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [False])

    def test_failed_week_is_recorded(self) -> None:
        def fail_load(*_args, **_kwargs):
            raise LinesError("no lines")

        with patch("nfl.holdout.fetch_props_closing", side_effect=AssertionError("network")), patch(
            "nfl.holdout.fetch_prop_rows",
            return_value=([], "props_closing: no rows"),
        ):
            report = run_holdout(
                [2024],
                [1],
                n=2,
                run_sensitivity=False,
                load=fail_load,
                prop_fetch=lambda season: ([], "props_closing: no rows"),
            )
        self.assertEqual(report["failed"][0]["week"], 1)
        self.assertFalse(report["scored"])

    def test_game_draws_are_recorded_without_dropping_player_draws(self) -> None:
        players = [
            _pl(pid="a", name="A", team="DET", opponent="NO"),
            _pl(pid="b", name="B", team="NO", opponent="DET", spread=3.0),
        ]
        sim = simulate_games(players, n=5, seed=1)
        self.assertEqual(len(sim.game_draws), 5)
        self.assertEqual(len(sim.draws["a"]), 5)
        self.assertEqual(sim.game_draws[0][1], "NO")
        self.assertEqual(sim.game_draws[0][2], "DET")


if __name__ == "__main__":
    unittest.main()
