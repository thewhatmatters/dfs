"""Holdout measurement. Mocks only; no network."""

from __future__ import annotations

import hashlib
import io
import json
import random
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from nfl.backtest import _depth_table, index_actual_rows
from nfl.gangstash import (
    GangstashDataError,
    capture_pulls,
    dataset_cache_file,
    fetch_dataset,
)
from nfl.gangstash_data import (
    GangstashDepthSlot,
    fetch_props_closing,
    parse_depth_slot,
    uniquify_depth_ranks,
)

from nfl.holdout import (
    HoldoutMetricsError,
    WeekLoad,
    _calibration_block,
    _excluded_out,
    _fmt,
    _missing_starter_fate,
    _game_actuals,
    _pair_roles,
    _resolve_actual,
    _scale_pace,
    _scale_rz,
    closing_prop_points,
    draw_percentile,
    fetch_prop_rows,
    format_report,
    index_actual_ids,
    index_prop_lines,
    is_pregame_starter,
    _pack_draws,
    load_week,
    main,
    metrics_requested,
    parse_seasons,
    parse_seeds,
    parse_weeks,
    pearson,
    write_draw_archive,
    perturb,
    pick_sensitivity_week,
    require_scoring,
    run_holdout,
    spearman,
)
from nfl.lines import LinesError
from nfl.players import Player
from nfl.projections import week1_score
from nfl.sim import simulate_games
from nfl.sim_efficiency import OpportunityCount, build_efficiency
from nfl.sim_inputs import SimInputs, TargetWeek, TeamStat


def _has_scoring() -> bool:
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
    except ImportError:
        return False
    return True


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

    def test_default_report_does_not_import_scoring(self) -> None:
        with patch("nfl.holdout.require_scoring", side_effect=AssertionError("scoring")):
            report = run_holdout(
                [2025],
                [2],
                n=4,
                seed=1,
                load=self._load,
                prop_fetch=lambda season: ([], "props_closing: no rows"),
            )
        self.assertNotIn("scores", report)
        self.assertGreater(report["errors"]["full"]["board"]["QB"]["n"], 0)

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
        self.assertIn("--metrics", proc.stdout)
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
        self.assertNotIn("scores", payload)
        self.assertFalse(path.with_suffix(".draws.npz").is_file())

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


class ActualJoinTest(unittest.TestCase):
    def test_gsis_id_wins_over_a_different_name(self) -> None:
        rows = [
            {
                "player_name": "Joshua Palmer",
                "team_fd": "LAC",
                "fd_points": 14.5,
                "gsis_id": "00-PALMER",
                "targets": 6,
            }
        ]
        player = _pl(pid="00-PALMER", name="Josh Palmer", team="LAC", opponent="DEN")
        found = _resolve_actual(player, index_actual_rows(rows), index_actual_ids(rows), {})
        self.assertIsNotNone(found)
        assert found is not None
        actual, _row, how = found
        self.assertEqual(how, "id")
        self.assertEqual(actual, 14.5)

    def test_name_is_the_fallback_when_the_id_misses(self) -> None:
        rows = [
            {
                "player_name": "Josh Palmer",
                "team_fd": "LAC",
                "fd_points": 9.0,
                "targets": 4,
            }
        ]
        player = _pl(pid="fd-1", name="Josh Palmer", team="LAC", opponent="DEN")
        found = _resolve_actual(player, index_actual_rows(rows), index_actual_ids(rows), {})
        self.assertIsNotNone(found)
        assert found is not None
        _points, _row, how = found
        self.assertEqual(how, "name")

    def test_out_before_kickoff_excludes_and_a_later_stamp_does_not(self) -> None:
        player = _pl(pid="00-1", name="Silent WR", position="WR", team="DET")
        kickoff = datetime(2024, 10, 6, 17, 0, tzinfo=timezone.utc)
        early = [{"gsis_id": "00-1", "status": "Out", "date_modified": "2024-10-06T12:00:00Z"}]
        late = [{"gsis_id": "00-1", "status": "Out", "date_modified": "2024-10-06T23:00:00Z"}]
        weekly = [{"player_name": "Silent WR", "team": "DET", "status": "IR"}]
        questionable = [{"gsis_id": "00-1", "status": "Questionable"}]
        self.assertTrue(_excluded_out(player, early, kickoff))
        self.assertFalse(_excluded_out(player, late, kickoff))
        self.assertTrue(_excluded_out(player, weekly, None))
        self.assertFalse(_excluded_out(player, questionable, None))

    def _slate(self, *, extra_actual=True, injury_rows=None, kickoff=None):
        def load(season, week, *, seed_prior=False):
            players = [
                _pl(pid="qb", name="Home QB", position="QB", team="DET", opponent="NO"),
                _pl(pid="wr", name="Home WR", position="WR", team="DET", opponent="NO"),
                _pl(
                    pid="ghost",
                    name="Silent WR",
                    position="WR",
                    team="DET",
                    opponent="NO",
                    depth_rank=2,
                ),
                _pl(pid="oqb", name="Away QB", position="QB", team="NO", opponent="DET", spread=3.0),
                _pl(pid="owr", name="Away WR", position="WR", team="NO", opponent="DET", spread=3.0),
                _pl(pid="def", name="Lions", position="D", team="DET", opponent="NO"),
            ]
            actuals = [
                _actual("Home QB", "DET", 18.0),
                _actual("Away QB", "NO", 14.0),
                _actual("Away WR", "NO", 9.0),
                _dst("DET", 8.0, 17.0),
                _dst("NO", 6.0, 24.0),
            ]
            if extra_actual:
                actuals.append(_actual("Home WR", "DET", 12.0))
            return WeekLoad(
                players,
                actuals,
                SimInputs(),
                [],
                ["pool: depth charts"],
                injury_rows=list(injury_rows or []),
                kickoff=kickoff,
            )

        return load

    def test_pregame_keeps_a_starter_with_no_box_score(self) -> None:
        common = dict(
            n=4,
            seed=1,
            run_sensitivity=False,
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        pre = run_holdout(
            [2025], [2], population="pregame", load=self._slate(extra_actual=False), **common
        )
        played = run_holdout(
            [2025], [2], population="played", load=self._slate(extra_actual=False), **common
        )
        self.assertEqual(pre["join"]["kept_as_zero"], 0)
        self.assertEqual(pre["join"]["kept_zero_no_injury_row"], 2)
        self.assertEqual(pre["join"]["kept_zero_no_injury_row_by_pos"]["WR"], 2)
        self.assertEqual(pre["join"]["excluded_as_out"], 0)
        self.assertGreater(
            pre["errors"]["starters"]["board"]["WR"]["n"],
            played["errors"]["starters"]["board"]["WR"]["n"],
        )
        self.assertEqual(played["join"]["kept_zero_no_injury_row"], 0)
        self.assertGreater(
            pre["headline"]["with_kept_zero_no_injury_row"]["n"],
            pre["headline"]["without_kept_zero_no_injury_row"]["n"],
        )
        text = format_report(pre)
        self.assertIn("kept-zero-no-injury-row 2", text)
        self.assertIn("without kept-zero-no-injury-row", text)
        self.assertIn("id-matched", text)

    def test_weekly_out_is_excluded_and_a_post_kickoff_stamp_is_kept(self) -> None:
        kickoff = datetime(2024, 10, 6, 17, 0, tzinfo=timezone.utc)
        out_row = {
            "gsis_id": "ghost",
            "status": "Out",
            "player_name": "Silent WR",
            "team": "DET",
        }
        excluded = run_holdout(
            [2025],
            [2],
            n=4,
            seed=1,
            population="pregame",
            run_sensitivity=False,
            load=self._slate(extra_actual=True, injury_rows=[out_row]),
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        self.assertEqual(excluded["join"]["excluded_as_out"], 1)
        self.assertEqual(excluded["join"]["kept_as_zero"], 0)
        late = {**out_row, "date_modified": "2024-10-06T23:00:00Z"}
        kept = run_holdout(
            [2025],
            [2],
            n=4,
            seed=1,
            population="pregame",
            run_sensitivity=False,
            load=self._slate(extra_actual=True, injury_rows=[late], kickoff=kickoff),
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        self.assertEqual(kept["join"]["excluded_as_out"], 0)
        self.assertEqual(kept["join"]["kept_as_zero"], 1)
        self.assertEqual(kept["join"]["kept_as_zero_by_pos"]["WR"], 1)

    def test_gangstash_report_status_and_a_missing_kickoff(self) -> None:
        player = _pl(pid="00-1", name="Josh Palmer", position="WR", team="LAC", opponent="DEN")
        row = {
            "season": 2025,
            "week": 2,
            "team": "LAC",
            "gsis_id": "00-1",
            "player_key": "00-1",
            "full_name": "Joshua Palmer",
            "report_status": "Out",
            "practice_status": "Did Not Participate In Practice",
            "date_modified": "2025-09-10T15:00:00Z",
        }
        kickoff = datetime(2025, 9, 14, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(_missing_starter_fate(player, [row], kickoff), "exclude")
        self.assertTrue(_excluded_out(player, [row], kickoff))
        self.assertEqual(_missing_starter_fate(player, [row], None), "unresolved_kickoff")
        self.assertFalse(_excluded_out(player, [row], None))
        weekly = {k: v for k, v in row.items() if k != "date_modified"}
        self.assertEqual(_missing_starter_fate(player, [weekly], None), "exclude")
        bare = _pl(pid="00-2", name="No Report", position="WR", team="LAC", opponent="DEN")
        self.assertEqual(_missing_starter_fate(bare, [row], kickoff), "zero_no_injury")
        named = _pl(pid="other", name="Joshua Palmer", position="WR", team="LAC", opponent="DEN")
        by_name = {k: v for k, v in weekly.items() if k not in {"gsis_id", "player_key"}}
        self.assertEqual(_missing_starter_fate(named, [by_name], None), "exclude")

        def load(season, week, *, seed_prior=False):
            players = [
                _pl(pid="qb", name="Home QB", position="QB", team="DET", opponent="NO"),
                _pl(pid="00-1", name="Josh Palmer", position="WR", team="LAC", opponent="DEN"),
                _pl(pid="oqb", name="Away QB", position="QB", team="NO", opponent="DET", spread=3.0),
                _pl(pid="owr", name="Away WR", position="WR", team="NO", opponent="DET", spread=3.0),
                _pl(pid="def", name="Lions", position="D", team="DET", opponent="NO"),
                _pl(pid="ddef", name="Broncos", position="D", team="DEN", opponent="LAC"),
            ]
            # Two games so the slate still solves; Palmer has no box score.
            actuals = [
                _actual("Home QB", "DET", 18.0),
                _actual("Away QB", "NO", 14.0),
                _actual("Away WR", "NO", 9.0),
                _dst("DET", 8.0, 17.0),
                _dst("NO", 6.0, 24.0),
                _dst("DEN", 4.0, 20.0),
            ]
            return WeekLoad(
                players,
                actuals,
                SimInputs(),
                [],
                ["pool: depth charts"],
                injury_rows=[row],
                kickoff=None,
                kickoff_note="kickoff missing",
            )

        report = run_holdout(
            [2025],
            [2],
            n=4,
            seed=1,
            population="pregame",
            run_sensitivity=False,
            load=load,
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        self.assertEqual(report["join"]["out_unresolved_no_kickoff"], 1)
        self.assertEqual(report["join"]["excluded_as_out"], 0)
        self.assertEqual(report["kickoff"]["missing"][0]["week"], 2)
        text = format_report(report)
        self.assertIn("kickoff missing", text)
        self.assertIn("out-unresolved-no-kickoff 1", text)

    def test_played_keeps_the_name_join_and_duplicate_draws(self) -> None:
        rows = [
            {
                "player_name": "Joshua Palmer",
                "team_fd": "LAC",
                "fd_points": 14.5,
                "gsis_id": "00-PALMER",
                "targets": 6,
            }
        ]
        player = _pl(pid="00-PALMER", name="Josh Palmer", team="LAC", opponent="DEN")
        played = _resolve_actual(player, index_actual_rows(rows), {}, {})
        pregame = _resolve_actual(player, index_actual_rows(rows), index_actual_ids(rows), {})
        self.assertIsNone(played)
        self.assertIsNotNone(pregame)
        assert pregame is not None
        self.assertEqual(pregame[2], "id")

        def load(season, week, *, seed_prior=False):
            players = [
                _pl(pid="tay", name="Taysom Hill", position="QB", team="DET", opponent="NO"),
                _pl(
                    pid="tay",
                    name="Taysom Hill",
                    position="TE",
                    team="DET",
                    opponent="NO",
                    depth_rank=2,
                ),
                _pl(pid="wr", name="Home WR", position="WR", team="DET", opponent="NO"),
                _pl(pid="oqb", name="Away QB", position="QB", team="NO", opponent="DET", spread=3.0),
                _pl(pid="owr", name="Away WR", position="WR", team="NO", opponent="DET", spread=3.0),
                _pl(pid="def", name="Lions", position="D", team="DET", opponent="NO"),
            ]
            actuals = [
                {**_actual("Taysom Hill", "DET", 8.0), "gsis_id": "tay", "targets": 1},
                _actual("Home WR", "DET", 12.0),
                _actual("Away QB", "NO", 14.0),
                _actual("Away WR", "NO", 9.0),
                _dst("DET", 8.0, 17.0),
                _dst("NO", 6.0, 24.0),
            ]
            return WeekLoad(players, actuals, SimInputs(), [], ["pool: depth charts"])

        played_report = run_holdout(
            [2025],
            [2],
            n=6,
            seed=1,
            population="played",
            run_sensitivity=False,
            load=load,
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        pre_report = run_holdout(
            [2025],
            [2],
            n=6,
            seed=1,
            population="pregame",
            run_sensitivity=False,
            load=load,
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        self.assertEqual(played_report["draw_skips"]["n"], 0)
        self.assertEqual(played_report["join"]["id_matched"], 0)
        self.assertEqual(played_report["errors"]["starters"]["board"]["QB"]["n"], 2)
        self.assertEqual(pre_report["draw_skips"]["n"], 2)
        self.assertEqual(pre_report["errors"]["starters"]["board"]["QB"]["n"], 1)

    @unittest.skipUnless(_has_scoring(), "numpy and scipy are required")
    def test_duplicate_pid_is_skipped_and_named(self) -> None:
        def load(season, week, *, seed_prior=False):
            players = [
                _pl(pid="tay", name="Taysom Hill", position="QB", team="DET", opponent="NO"),
                _pl(
                    pid="tay",
                    name="Taysom Hill",
                    position="TE",
                    team="DET",
                    opponent="NO",
                    depth_rank=2,
                ),
                _pl(pid="wr", name="Home WR", position="WR", team="DET", opponent="NO"),
                _pl(pid="oqb", name="Away QB", position="QB", team="NO", opponent="DET", spread=3.0),
                _pl(pid="owr", name="Away WR", position="WR", team="NO", opponent="DET", spread=3.0),
                _pl(pid="def", name="Lions", position="D", team="DET", opponent="NO"),
            ]
            actuals = [
                {**_actual("Taysom Hill", "DET", 8.0), "gsis_id": "tay", "targets": 1},
                _actual("Home WR", "DET", 12.0),
                _actual("Away QB", "NO", 14.0),
                _actual("Away WR", "NO", 9.0),
                _dst("DET", 8.0, 17.0),
                _dst("NO", 6.0, 24.0),
            ]
            return WeekLoad(players, actuals, SimInputs(), [], ["pool: depth charts"])

        report = run_holdout(
            [2025],
            [2],
            n=6,
            seed=1,
            metrics=True,
            population="pregame",
            run_sensitivity=False,
            load=load,
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        skips = report["draw_skips"]
        self.assertEqual(skips["n"], 2)
        self.assertEqual(skips["n_draws"], 6)
        self.assertEqual({row["position"] for row in skips["rows"]}, {"QB", "TE"})
        self.assertTrue(all(row["got"] == 12 for row in skips["rows"]))
        self.assertTrue(all(row["name"] == "Taysom Hill" for row in skips["rows"]))
        text = format_report(report)
        self.assertIn("draw skips 2", text)
        self.assertIn("Taysom Hill", text)

    @unittest.skipUnless(_has_scoring(), "numpy and scipy are required")
    def test_monte_carlo_columns_keep_three_or_more_decimals(self) -> None:
        self.assertEqual(_fmt(0.0042, 9, 4).strip(), "0.0042")
        report = run_holdout(
            [2025],
            [2],
            n=5,
            seeds=[1, 2],
            population="played",
            run_sensitivity=False,
            load=self._slate(extra_actual=True),
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        text = format_report(report)
        self.assertRegex(text, r"se\s+-?\d+\.\d{4}")
        self.assertRegex(text, r"half\s+-?\d+\.\d{4}")


class MeasurementHarnessTest(unittest.TestCase):
    def _load(self, season, week, *, seed_prior=False):
        players = [
            _pl(pid="qb", name="Home QB", position="QB", team="DET", opponent="NO", salary=8000),
            _pl(pid="wr", name="Home WR", position="WR", team="DET", opponent="NO", salary=7000),
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
            {**_actual("Home WR", "DET", 0.0), "targets": 0, "snaps": 0},
            _actual("Away QB", "NO", 14.0),
            _actual("Away WR", "NO", 9.0),
            _dst("DET", 8.0, 17.0),
            _dst("NO", 6.0, 24.0),
        ]
        return WeekLoad(players, actuals, SimInputs(), [], ["pool: depth charts"])

    @unittest.skipUnless(_has_scoring(), "numpy and scipy are required")
    def test_long_ensembles_store_quantiles(self) -> None:
        import numpy as np

        kind, values = _pack_draws(np.linspace(0.0, 10.0, 300))
        self.assertEqual(kind, "quantiles")
        self.assertEqual(values.shape, (99,))
        self.assertAlmostEqual(float(values[0]), 0.1, places=6)
        self.assertAlmostEqual(float(values[-1]), 9.9, places=6)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draws.npz"
            info = write_draw_archive(
                path,
                [
                    {
                        "kind": kind,
                        "values": values,
                        "actual": 4.0,
                        "mean": 5.0,
                        "salary": 6000,
                        "season": 2024,
                        "week": 2,
                        "seed": 1,
                        "starter": True,
                        "mode": "sim_data",
                        "pid": "qb",
                        "position": "QB",
                        "block": "2024-2",
                    }
                ],
            )
            self.assertEqual(info["kind"], "quantiles")
            self.assertEqual(info["quantiles"], "q01..q99")
            loaded = np.load(path, allow_pickle=False)
            self.assertEqual(loaded["values"].shape, (1, 99))
            self.assertAlmostEqual(float(loaded["quantile"][0]), 0.01)

    def test_parse_seeds(self) -> None:
        self.assertEqual(parse_seeds(None, 4), [4])
        self.assertEqual(parse_seeds("1,2,3", 9), [1, 2, 3])
        with self.assertRaises(ValueError):
            parse_seeds("1,2,3,4,5,6", 1)

    def test_pregame_keeps_a_zero_point_starter(self) -> None:
        self.assertTrue(
            is_pregame_starter(_pl(name="Home WR", position="WR", depth_rank=1))
        )
        pre = run_holdout(
            [2025],
            [2],
            n=4,
            seed=1,
            population="pregame",
            run_sensitivity=False,
            load=self._load,
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        played = run_holdout(
            [2025],
            [2],
            n=4,
            seed=1,
            population="played",
            run_sensitivity=False,
            load=self._load,
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        self.assertGreater(
            pre["errors"]["starters"]["board"]["WR"]["n"],
            played["errors"]["starters"]["board"]["WR"]["n"],
        )
        self.assertEqual(
            pre["errors"]["full"]["board"]["WR"]["n"],
            played["errors"]["full"]["board"]["WR"]["n"],
        )
        self.assertEqual(pre["correlations"], played["correlations"])
        self.assertNotIn("scores", pre)
        self.assertNotIn("paired", pre)
        self.assertNotIn("mc_se", pre)
        text = format_report(pre)
        self.assertIn("population: pregame", text)
        self.assertIn("correlations (actual Pearson", text)

    @unittest.skipUnless(_has_scoring(), "numpy and scipy are required")
    def test_metrics_scores_the_primary_seed(self) -> None:
        pre = run_holdout(
            [2025],
            [2],
            n=4,
            seed=1,
            metrics=True,
            population="pregame",
            run_sensitivity=False,
            load=self._load,
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        self.assertEqual(
            pre["correlations"]["QB-WR1"]["sim_data"],
            pre["residual_correlations"]["QB-WR1"]["sim_data"]["sim"],
        )
        self.assertIn("scores", pre)
        self.assertIn("paired", pre)
        self.assertEqual(pre["paired"]["starters"]["board_minus_sim_data"]["abs_error"]["n_boot"], 2000)
        self.assertIn("few blocks", pre["scores"]["pools"]["starters"]["sim_data"]["crps"]["note"])
        self.assertGreater(pre["scores"]["salary_thresholds"]["n"], 0)
        self.assertEqual(pre["scores"]["lineup"]["thresholds"], [125.0, 165.0])
        self.assertEqual(pre["mc_se"]["n_seeds"], 1)
        self.assertIsNone(pre["mc_se"]["metrics"]["starter_mae.sim_data.ALL"]["se"])
        self.assertEqual(
            pre["errors"]["starters"]["board"]["QB"]["mae"],
            pre["mc_se"]["metrics"]["starter_mae.board.QB"]["mean"],
        )
        text = format_report(pre)
        self.assertIn("residual correlations", text)

    @unittest.skipUnless(_has_scoring(), "numpy and scipy are required")
    def test_seeds_report_monte_carlo_se(self) -> None:
        report = run_holdout(
            [2025],
            [2],
            n=5,
            seeds=[1, 2],
            population="played",
            run_sensitivity=False,
            load=self._load,
            prop_fetch=lambda season: ([], "props_closing: no rows"),
        )
        metric = report["mc_se"]["metrics"]["starter_mae.sim_data.ALL"]
        self.assertEqual(metric["n"], 2)
        self.assertEqual(len(metric["values"]), 2)
        self.assertAlmostEqual(metric["mean"], sum(metric["values"]) / 2, places=4)
        self.assertIsNotNone(metric["se"])
        self.assertIsNotNone(metric["half_width_95"])
        self.assertIn("coverage.sim_data.p10_p90", report["mc_se"]["metrics"])
        self.assertIn("game_total_sd.sim_data", report["mc_se"]["metrics"])
        self.assertEqual(report["seed"], 1)
        self.assertEqual(report["seeds"], [1, 2])

    @unittest.skipUnless(_has_scoring(), "numpy and scipy are required")
    def test_main_writes_manifest_and_draws(self) -> None:
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
                    [
                        "--season",
                        "2025",
                        "--weeks",
                        "2",
                        "--n",
                        "4",
                        "--seeds",
                        "1,2",
                        "--population",
                        "pregame",
                        "--json-out",
                        str(path),
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(path.read_text(encoding="utf-8"))
            draws = path.with_suffix(".draws.npz")
            manifest_path = path.with_suffix(".manifest.json")
            self.assertTrue(draws.is_file())
            self.assertTrue(manifest_path.is_file())
            archive = np_load(draws)
            self.assertEqual(str(archive["kind"][0]), "draws")
            self.assertEqual(archive["values"].shape[0], payload["draws"]["n_rows"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        self.assertEqual(manifest["git_sha"], sha)
        self.assertIn(manifest["git_dirty"], (True, False))
        self.assertEqual(manifest["seeds"], [1, 2])
        self.assertEqual(manifest["draws"]["n"], 4)
        self.assertEqual(manifest["draws"]["sha256"], payload["draws"]["sha256"])
        self.assertEqual(manifest["population"], "pregame")
        self.assertIn("--seeds", manifest["argv"])
        self.assertTrue(manifest["python"])
        self.assertTrue(manifest["numpy"])
        self.assertTrue(manifest["scipy"])
        self.assertEqual(manifest["datasets"], [])
        self.assertEqual(payload["population"], "pregame")
        self.assertIn("errors", payload)
        self.assertIn("calibration", payload)


def np_load(path: Path):
    import numpy as np

    return np.load(path, allow_pickle=False)


class ScoringImportTest(unittest.TestCase):
    def test_optimizer_nightly_and_holdout_stay_off_numpy(self) -> None:
        script = (
            "import sys\n"
            "import nfl.optimize\n"
            "import nfl.publish_projections\n"
            "import nfl.holdout\n"
            "bad = [name for name in sys.modules if name == 'numpy' or name.startswith('numpy.')"
            " or name == 'scipy' or name.startswith('scipy.') or name == 'nfl.calibration']\n"
            "print('\\n'.join(bad))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_require_scoring_names_the_requirements_file(self) -> None:
        import builtins

        saved = {
            key: sys.modules.pop(key)
            for key in list(sys.modules)
            if key == "numpy"
            or key.startswith("numpy.")
            or key == "scipy"
            or key.startswith("scipy.")
            or key == "nfl.calibration"
        }
        if hasattr(require_scoring, "_cached"):
            delattr(require_scoring, "_cached")
        real_import = builtins.__import__

        def blocked(name, globals=None, locals=None, fromlist=(), level=0):
            root = name.split(".", 1)[0]
            if root in {"numpy", "scipy"}:
                raise ImportError(name)
            return real_import(name, globals, locals, fromlist, level)

        try:
            with patch("builtins.__import__", blocked):
                with self.assertRaises(HoldoutMetricsError) as caught:
                    require_scoring()
            self.assertIn("pip3 install -r requirements.txt", str(caught.exception))
        finally:
            for key in list(sys.modules):
                if (
                    key == "numpy"
                    or key.startswith("numpy.")
                    or key == "scipy"
                    or key.startswith("scipy.")
                    or key == "nfl.calibration"
                ):
                    sys.modules.pop(key, None)
            sys.modules.update(saved)
            if hasattr(require_scoring, "_cached"):
                delattr(require_scoring, "_cached")

    def test_metrics_flag_errors_when_scoring_libs_are_missing(self) -> None:
        err = io.StringIO()
        with patch(
            "nfl.holdout.require_scoring",
            side_effect=HoldoutMetricsError(
                "numpy and scipy are required for holdout scoring metrics. "
                "pip3 install -r requirements.txt"
            ),
        ), redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = main(["--season", "2025", "--weeks", "2", "--metrics"])
        self.assertEqual(code, 1)
        self.assertIn("pip3 install -r requirements.txt", err.getvalue())
        self.assertIn("choke HOLDOUT_METRICS:", err.getvalue())

    def test_multiple_seeds_count_as_a_metrics_request(self) -> None:
        self.assertFalse(metrics_requested(metrics=False, seeds=[1], draws_out=None))
        self.assertTrue(metrics_requested(metrics=True, seeds=[1], draws_out=None))
        self.assertTrue(metrics_requested(metrics=False, seeds=[1, 2], draws_out=None))
        self.assertTrue(metrics_requested(metrics=False, seeds=[1], draws_out="draws.npz"))


class PullManifestTest(unittest.TestCase):
    def test_cache_hit_records_sha_rows_and_timestamp(self) -> None:
        day = date(2026, 9, 24)
        params = {"season": "2024", "week": "2"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = dataset_cache_file(root, day, "player_stats_weekly", params)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"data": [{"player_name": "A"}, {"player_name": "B"}], "truncated": False}),
                encoding="utf-8",
            )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with capture_pulls() as pulls, patch("nfl.gangstash.http_json") as http:
                rows, _meta = fetch_dataset(
                    "player_stats_weekly", params, cache_day=day, cache_root=root
                )
            http.assert_not_called()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(pulls), 1)
        recorded = pulls[0]
        self.assertEqual(recorded["dataset"], "player_stats_weekly")
        self.assertEqual(recorded["n_rows"], 2)
        self.assertEqual(recorded["sha256"], digest)
        self.assertTrue(recorded["pulled_at"])
        self.assertEqual(recorded["params"]["season"], "2024")
        self.assertFalse(recorded["live"])


if __name__ == "__main__":
    unittest.main()
