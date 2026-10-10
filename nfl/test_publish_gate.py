"""Publish gate. Fixtures only — no network.

Case (c) prints the injury-gap line so a passing run still shows the block.
"""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from nfl.players import Player
from nfl.publish_gate import (
    COLLECTORS,
    GateLimits,
    GateResult,
    GateSnapshot,
    evaluate_gate,
    gate_allows_stale,
    limits_from_args,
    load_gate_snapshot,
)
from nfl.publish_projections import PublishEntry, main, parse_args
from nfl.report import build_report
from nfl.teams import TEAMS

NOW = datetime(2026, 10, 10, 17, 0, tzinfo=timezone.utc)
SEASON = 2026
WEEK = 5
PREV = 4

DEPTH_COLLECTORS = COLLECTORS["depth"]


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _succeeded(
    collector: str,
    now: datetime,
    *,
    age_hours: float = 1.0,
    run_id: str = "ok",
    status: str = "succeeded",
) -> dict:
    finished = now - timedelta(hours=age_hours)
    started = finished - timedelta(minutes=5)
    return {
        "run_id": run_id,
        "collector": collector,
        "started_at": _iso(started),
        "finished_at": None if status == "running" else _iso(finished),
        "status": status,
        "row_counts": {"rows": 10},
        "error": None,
        "host": "test",
        "git_sha": "abc",
        "args": {},
    }


def _running(collector: str, now: datetime, *, minutes: float, run_id: str = "live") -> dict:
    started = now - timedelta(minutes=minutes)
    return {
        "run_id": run_id,
        "collector": collector,
        "started_at": _iso(started),
        "finished_at": None,
        "status": "running",
        "row_counts": {},
        "error": None,
        "host": "test",
        "git_sha": "abc",
        "args": {},
    }


def _fresh_runs(now: datetime, *, age_hours: float = 1.0) -> list[dict]:
    rows = []
    for collectors in COLLECTORS.values():
        for name in collectors:
            rows.append(_succeeded(name, now, age_hours=age_hours, run_id=f"{name}-ok"))
    return rows


def _replace_collector(runs: list[dict], collector: str, rows: list[dict]) -> list[dict]:
    kept = [row for row in runs if row.get("collector") != collector]
    return kept + rows


def _games(teams: list[str]) -> list[dict]:
    rows = []
    for index in range(0, len(teams), 2):
        rows.append(
            {
                "season": SEASON,
                "week": WEEK,
                "home_team_fd": teams[index],
                "away_team_fd": teams[index + 1],
                "spread": -3.0,
                "total": 44.5,
                "game_id": f"{SEASON}_{WEEK}_{teams[index + 1]}_{teams[index]}",
            }
        )
    return rows


def _injury_rows(teams: list[str]) -> list[dict]:
    rows = []
    for team in teams:
        code = "JAX" if team == "JAC" else team
        rows.append(
            {
                "season": SEASON,
                "week": WEEK,
                "team_fd": code,
                "player_name": f"{team} Player",
                "report_status": "Questionable",
            }
        )
    return rows


def _depth_rows(teams: list[str]) -> list[dict]:
    return [
        {
            "team_fd": team,
            "player_name": f"{team} QB",
            "pos_abb": "QB",
            "pos_rank": 1,
            "week": WEEK,
        }
        for team in teams
    ]


def _week_rows(week: int = PREV) -> dict:
    return {
        name: [{"week": week, "team_fd": "KC", "player_name": "Someone"}]
        for name in ("player_stats_weekly", "player_usage", "snaps", "targets")
    }


def _snapshot(
    *,
    runs: list[dict] | None = None,
    teams: list[str] | None = None,
    injury_teams: list[str] | None = None,
    depth_teams: list[str] | None = None,
    week_rows: dict | None = None,
    games: list | None = None,
) -> GateSnapshot:
    playing = list(TEAMS) if teams is None else list(teams)
    playing = sorted(playing)
    return GateSnapshot(
        runs=_fresh_runs(NOW) if runs is None else runs,
        games=_games(playing) if games is None else games,
        injury_rows=_injury_rows(playing if injury_teams is None else injury_teams),
        depth_rows=_depth_rows(playing if depth_teams is None else depth_teams),
        week_rows=_week_rows() if week_rows is None else week_rows,
    )


def _judge(snapshot: GateSnapshot, *, limits: GateLimits | None = None) -> GateResult:
    return evaluate_gate(snapshot, now=NOW, week=WEEK, limits=limits)


class PublishGateTest(unittest.TestCase):
    def test_collector_names_live_in_one_map(self) -> None:
        self.assertEqual(COLLECTORS["lines"], ("bettingpros-odds",))
        self.assertEqual(COLLECTORS["props"], ("bettingpros-pbcs",))
        self.assertEqual(COLLECTORS["injuries"], ("nflverse-injuries",))
        self.assertEqual(
            COLLECTORS["depth"],
            ("nflverse-depth-charts", "nflverse-depth-charts-weekly"),
        )

    def test_fresh_full_coverage_passes(self) -> None:
        result = _judge(_snapshot())
        self.assertTrue(result.ok, "\n".join(result.failures))
        self.assertEqual(result.failures, ())
        header = "\n".join(result.header)
        self.assertIn("lines bettingpros-odds age 1.0h (limit 26h) latest succeeded", header)
        self.assertIn("props bettingpros-pbcs age 1.0h (limit 26h) latest succeeded", header)
        self.assertIn("injuries nflverse-injuries age 1.0h (limit 26h) latest succeeded", header)
        self.assertIn("depth nflverse-depth-charts age 1.0h (limit 72h) latest succeeded", header)
        self.assertIn(
            "depth nflverse-depth-charts-weekly age 1.0h (limit 72h) latest succeeded",
            header,
        )
        self.assertIn("coverage lines 32/32 teams", header)
        self.assertIn("coverage injuries 32/32 teams", header)
        self.assertIn("coverage depth 32/32 teams", header)
        self.assertIn("previous week 4 player_stats_weekly present", header)
        self.assertIn("previous week 4 player_usage present", header)
        self.assertIn("previous week 4 snaps present", header)
        self.assertIn("previous week 4 targets present", header)

    def test_lines_13h_within_26h_limit_pass(self) -> None:
        runs = _replace_collector(
            _fresh_runs(NOW),
            "bettingpros-odds",
            [_succeeded("bettingpros-odds", NOW, age_hours=13, run_id="lines-13")],
        )
        result = _judge(_snapshot(runs=runs))
        self.assertTrue(result.ok, "\n".join(result.failures))
        self.assertIn("age 13.0h (limit 26h)", "\n".join(result.header))

    def test_lines_27h_block(self) -> None:
        runs = _replace_collector(
            _fresh_runs(NOW),
            "bettingpros-odds",
            [_succeeded("bettingpros-odds", NOW, age_hours=27, run_id="lines-27")],
        )
        result = _judge(_snapshot(runs=runs))
        self.assertFalse(result.ok)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("bettingpros-odds", result.failures[0])
        self.assertIn("27.0h > 26h", result.failures[0])
        self.assertNotIn("as_of", result.failures[0])

    def test_tighter_lines_limit_blocks_13h(self) -> None:
        runs = _replace_collector(
            _fresh_runs(NOW),
            "bettingpros-odds",
            [_succeeded("bettingpros-odds", NOW, age_hours=13, run_id="lines-13")],
        )
        result = _judge(_snapshot(runs=runs), limits=GateLimits(lines_hours=12))
        self.assertFalse(result.ok)
        self.assertIn("13.0h > 12h", result.failures[0])

    def test_injury_missing_nine_teams_blocks_and_names_them(self) -> None:
        teams = sorted(TEAMS)
        missing = teams[:9]
        result = _judge(_snapshot(injury_teams=teams[9:]))
        self.assertFalse(result.ok)
        injury = [line for line in result.failures if line.startswith("injuries missing")]
        self.assertEqual(len(injury), 1)
        self.assertEqual(
            injury[0],
            "injuries missing 9 teams: " + ", ".join(missing),
        )
        print("CASE (c) BLOCKED")
        print(injury[0])
        self.assertIn("coverage injuries 23/32 teams missing " + ", ".join(missing), result.header)

    def test_bye_week_teams_are_not_required(self) -> None:
        teams = sorted(TEAMS)
        bye = teams[-2:]
        playing = teams[:-2]
        self.assertEqual(bye, ["TEN", "WAS"])
        self.assertEqual(len(playing), 30)
        result = _judge(
            _snapshot(teams=playing, injury_teams=playing, depth_teams=playing)
        )
        self.assertTrue(result.ok, "\n".join(result.failures))
        header = "\n".join(result.header)
        self.assertIn("coverage lines 30/30 teams", header)
        self.assertIn("coverage injuries 30/30 teams", header)
        self.assertIn("coverage depth 30/30 teams", header)
        self.assertNotIn("TEN", header)
        self.assertNotIn("WAS", header)

    def test_latest_partial_blocks(self) -> None:
        partial = _succeeded(
            "bettingpros-odds", NOW, age_hours=0.1, run_id="new", status="partial"
        )
        older = _succeeded("bettingpros-odds", NOW, age_hours=2, run_id="old")
        runs = _replace_collector(_fresh_runs(NOW), "bettingpros-odds", [older, partial])
        result = _judge(_snapshot(runs=runs))
        self.assertFalse(result.ok)
        self.assertTrue(any("latest load partial" in line for line in result.failures))
        self.assertIn("bettingpros-odds", "\n".join(result.failures))

    def test_running_45_minutes_blocks(self) -> None:
        older = _succeeded("bettingpros-pbcs", NOW, age_hours=2, run_id="old")
        live = _running("bettingpros-pbcs", NOW, minutes=45, run_id="dead")
        runs = _replace_collector(_fresh_runs(NOW), "bettingpros-pbcs", [older, live])
        result = _judge(_snapshot(runs=runs))
        self.assertFalse(result.ok)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("latest load running 45m (> 30m)", result.failures[0])
        self.assertIn("bettingpros-pbcs", result.failures[0])
        self.assertIn("run_id=dead", result.failures[0])

    def test_running_under_30_minutes_does_not_block(self) -> None:
        older = _succeeded("bettingpros-pbcs", NOW, age_hours=2, run_id="old")
        live = _running("bettingpros-pbcs", NOW, minutes=10, run_id="live")
        runs = _replace_collector(_fresh_runs(NOW), "bettingpros-pbcs", [older, live])
        result = _judge(_snapshot(runs=runs))
        self.assertTrue(result.ok, "\n".join(result.failures))
        self.assertIn("latest running", "\n".join(result.header))

    def test_failed_blocks(self) -> None:
        failed = _succeeded(
            "nflverse-injuries", NOW, age_hours=0.05, run_id="bad", status="failed"
        )
        older = _succeeded("nflverse-injuries", NOW, age_hours=2, run_id="old")
        runs = _replace_collector(_fresh_runs(NOW), "nflverse-injuries", [older, failed])
        result = _judge(_snapshot(runs=runs))
        self.assertFalse(result.ok)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("latest load failed", result.failures[0])
        self.assertIn("nflverse-injuries", result.failures[0])
        self.assertIn("age 2.0h", "\n".join(result.header))

    def test_succeeded_then_newer_partial_still_blocks(self) -> None:
        older = _succeeded("bettingpros-odds", NOW, age_hours=2, run_id="old")
        newer = _succeeded(
            "bettingpros-odds", NOW, age_hours=0.1, run_id="new", status="partial"
        )
        newer["as_of"] = _iso(NOW)
        newer["updated_at"] = _iso(NOW)
        runs = _replace_collector(_fresh_runs(NOW), "bettingpros-odds", [older, newer])
        result = _judge(_snapshot(runs=runs))
        self.assertFalse(result.ok)
        text = "\n".join(result.failures)
        self.assertIn("latest load partial", text)
        self.assertNotIn("stale", text)
        self.assertIn("age 2.0h (limit 26h) latest partial", "\n".join(result.header))

    def test_missing_status_blocks(self) -> None:
        row = _succeeded("nflverse-injuries", NOW, age_hours=1, run_id="blank")
        row.pop("status")
        runs = _replace_collector(_fresh_runs(NOW), "nflverse-injuries", [row])
        result = _judge(_snapshot(runs=runs))
        self.assertFalse(result.ok)
        self.assertTrue(any("status missing" in line for line in result.failures))

    def test_cache_written_today_with_four_day_depth_blocks(self) -> None:
        runs = _fresh_runs(NOW)
        for name in DEPTH_COLLECTORS:
            row = _succeeded(name, NOW, age_hours=96, run_id=f"{name}-old")
            row["as_of"] = _iso(NOW)
            row["updated_at"] = _iso(NOW)
            runs = _replace_collector(runs, name, [row])
        teams = sorted(TEAMS)
        games = _games(teams)
        injuries = _injury_rows(teams)
        depth = _depth_rows(teams)
        with tempfile.TemporaryDirectory() as tmp:
            day = datetime.now(timezone.utc).date().isoformat()
            cache = Path(tmp) / day / "depth_charts" / "_all.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(
                json.dumps(
                    {"data": [{"team_fd": "KC", "as_of": _iso(NOW), "updated_at": _iso(NOW)}]}
                ),
                encoding="utf-8",
            )
            self.assertLess(abs(time.time() - cache.stat().st_mtime), 120)
            with patch("nfl.gangstash.DATA_CACHE_DIR", Path(tmp)), patch(
                "nfl.gangstash_data.fetch_collector_runs",
                return_value=(runs, {"cache": str(cache), "cache_stale": False, "live": False}),
            ), patch(
                "nfl.gangstash_data.fetch_game_lines",
                return_value=(games, {"cache_stale": False}),
            ), patch(
                "nfl.gangstash_data.fetch_week_injuries",
                return_value=(injuries, {"cache_stale": False}),
            ), patch(
                "nfl.gangstash_data.fetch_depth_charts",
                return_value=(depth, {"cache_stale": False, "cache": str(cache)}),
            ), patch(
                "nfl.gangstash_data.fetch_player_stats_weekly",
                return_value=(_week_rows()["player_stats_weekly"], {"cache_stale": False}),
            ), patch(
                "nfl.gangstash_data.fetch_player_usage",
                return_value=(_week_rows()["player_usage"], {"cache_stale": False}),
            ), patch(
                "nfl.gangstash_data.fetch_snaps",
                return_value=(_week_rows()["snaps"], {"cache_stale": False}),
            ), patch(
                "nfl.gangstash_data.fetch_targets",
                return_value=(_week_rows()["targets"], {"cache_stale": False}),
            ):
                snapshot = load_gate_snapshot(SEASON, WEEK, refresh=False)
            result = evaluate_gate(snapshot, now=NOW, week=WEEK)
        self.assertFalse(result.ok)
        text = "\n".join(result.failures)
        self.assertIn("96.0h > 72h", text)
        self.assertIn("nflverse-depth-charts last succeeded", text)
        self.assertIn("nflverse-depth-charts-weekly last succeeded", text)
        self.assertNotIn("cache", text)
        self.assertNotIn(str(cache), text)
        self.assertEqual(len(result.failures), 2)

    def test_previous_week_missing_blocks(self) -> None:
        rows = _week_rows(week=2)
        result = _judge(_snapshot(week_rows=rows))
        self.assertFalse(result.ok)
        text = "\n".join(result.failures)
        self.assertIn("player_stats_weekly missing previous completed week 4", text)
        self.assertIn("player_usage missing previous completed week 4", text)
        self.assertIn("snaps missing previous completed week 4", text)
        self.assertIn("targets missing previous completed week 4", text)

    def test_week_1_skips_previous_week(self) -> None:
        teams = sorted(TEAMS)
        games = _games(teams)
        injuries = _injury_rows(teams)
        depth = _depth_rows(teams)
        for row in games + injuries + depth:
            row["week"] = 1
        snapshot = GateSnapshot(
            runs=_fresh_runs(NOW),
            games=games,
            injury_rows=injuries,
            depth_rows=depth,
            week_rows=None,
        )
        result = evaluate_gate(snapshot, now=NOW, week=1)
        self.assertTrue(result.ok, "\n".join(result.failures))
        self.assertIn("previous week n/a (week 1)", result.header)

    def test_limits_are_configurable_on_the_cli(self) -> None:
        args = parse_args(
            [
                "--lines-max-age-hours",
                "12",
                "--props-max-age-hours",
                "10",
                "--injuries-max-age-hours",
                "8",
                "--depth-max-age-hours",
                "48",
                "--running-max-minutes",
                "15",
                "--skip-gate",
                "--gate-only",
            ]
        )
        self.assertTrue(args.skip_gate)
        self.assertTrue(args.gate_only)
        self.assertTrue(gate_allows_stale(args))
        self.assertTrue(gate_allows_stale(parse_args(["--allow-stale"])))
        limits = limits_from_args(args)
        self.assertEqual(limits.lines_hours, 12)
        self.assertEqual(limits.props_hours, 10)
        self.assertEqual(limits.injuries_hours, 8)
        self.assertEqual(limits.depth_hours, 48)
        self.assertEqual(limits.running_minutes, 15)

    def test_report_header_lists_age_and_coverage(self) -> None:
        result = _judge(_snapshot())
        text = build_report(
            [],
            [],
            season=SEASON,
            week=WEEK,
            run_at="2026-10-10T17:00:00+00:00",
            draws=10000,
            efficiency="data",
            gate_header=list(result.header),
        )
        self.assertLess(text.index("season 2026"), text.index("age 1.0h"))
        self.assertLess(text.index("games 0"), text.index("coverage lines 32/32 teams"))
        self.assertLess(text.index("coverage depth 32/32 teams"), text.index("```"))

    def test_collector_runs_dataset_name(self) -> None:
        from nfl.gangstash_data import fetch_collector_runs

        with patch("nfl.gangstash_data.fetch_dataset", return_value=([], {})) as fetch:
            fetch_collector_runs(refresh=True)
        self.assertEqual(fetch.call_args.args[0], "collector_runs")


def _entries() -> list[PublishEntry]:
    player = Player(
        pid="qb",
        name="Patrick Mahomes",
        position="QB",
        salary=8000,
        team="KC",
        opponent="BUF",
        game="KC@BUF",
        fppg=None,
        injury="",
        roster_position="QB",
        objective=18.0,
    )
    return [PublishEntry(player, "00-1", "p1", "2026_05_KC_BUF", None, 8000)]


class PublishGateCliTest(unittest.TestCase):
    def _stale_lines(self) -> GateResult:
        runs = _replace_collector(
            _fresh_runs(NOW),
            "bettingpros-odds",
            [_succeeded("bettingpros-odds", NOW, age_hours=27, run_id="lines-27")],
        )
        return _judge(_snapshot(runs=runs))

    def test_stale_lines_exit_before_post(self) -> None:
        gate = self._stale_lines()
        err = io.StringIO()
        with patch("nfl.publish_projections.projections_key", return_value="sekrit"), patch(
            "nfl.publish_projections.load_slate", return_value=(SEASON, WEEK, _entries())
        ), patch(
            "nfl.publish_projections.enforce_publish_gate", return_value=gate
        ), patch("nfl.publish_projections.post_projection_rows") as post, patch(
            "nfl.publish_projections.maybe_sim"
        ) as sim, redirect_stderr(err):
            rc = main(["--sim", "0"])
        self.assertEqual(rc, 1)
        post.assert_not_called()
        sim.assert_not_called()
        self.assertIn("27.0h > 26h", err.getvalue())
        self.assertIn("bettingpros-odds", err.getvalue())
        self.assertNotIn("GATE WARNING", err.getvalue())

    def test_allow_stale_and_skip_gate_publish_with_warnings(self) -> None:
        gate = self._stale_lines()
        for flag in ("--allow-stale", "--skip-gate"):
            posted = {}

            def capture(rows, key, _posted=posted):
                _posted["rows"] = rows
                _posted["key"] = key
                return 1, 0

            err = io.StringIO()
            out = io.StringIO()
            with tempfile.TemporaryDirectory() as reports, patch(
                "nfl.publish_projections.projections_key", return_value="sekrit"
            ), patch(
                "nfl.publish_projections.load_slate",
                return_value=(SEASON, WEEK, _entries()),
            ), patch(
                "nfl.publish_projections.enforce_publish_gate", return_value=gate
            ), patch(
                "nfl.publish_projections.model_version", return_value="abc1234"
            ), patch(
                "nfl.publish_projections.post_projection_rows", capture
            ), patch(
                "nfl.publish_projections.fetch_game_lines",
                return_value=([], {"cache_stale": False}),
            ), patch("nfl.report.REPORTS_DIR", Path(reports)), redirect_stdout(
                out
            ), redirect_stderr(err):
                rc = main(["--report", "--sim", "0", flag])
                written = list(Path(reports).glob("*.md"))
                report = written[0].read_text(encoding="utf-8") if written else ""
            self.assertEqual(rc, 0, err.getvalue())
            self.assertEqual(posted.get("key"), "sekrit")
            self.assertTrue(posted.get("rows"))
            text = err.getvalue()
            self.assertIn("GATE WARNING:", text)
            self.assertIn("27.0h > 26h", text)
            self.assertIn("GATE WARNING: lines stale:", report)
            self.assertIn("27.0h > 26h", report)
            self.assertIn("coverage lines 32/32 teams", report)
            self.assertLess(report.index("season 2026"), report.index("GATE WARNING:"))
            self.assertLess(report.index("GATE WARNING:"), report.index("```"))

    def test_gate_only_blocks_and_names_nine_injury_teams(self) -> None:
        teams = sorted(TEAMS)
        missing = teams[:9]
        result = _judge(_snapshot(injury_teams=teams[9:]))
        line = next(item for item in result.failures if item.startswith("injuries missing"))
        self.assertEqual(line, "injuries missing 9 teams: " + ", ".join(missing))

        def fetch(*, on_date=None, season=None, week=None, refresh=False, cache_day=None):
            return _games(teams), {"cache_stale": False}

        err = io.StringIO()
        out = io.StringIO()
        with patch("nfl.publish_projections.fetch_game_lines", fetch), patch(
            "nfl.publish_projections.enforce_publish_gate", return_value=result
        ), patch("nfl.publish_projections.projections_key") as key, patch(
            "nfl.publish_projections.post_projection_rows"
        ) as post, patch("nfl.publish_projections.load_slate") as load, redirect_stdout(
            out
        ), redirect_stderr(err):
            rc = main(["--gate-only", "--season", str(SEASON), "--week", str(WEEK)])
        print("CASE (c) gate-only exit %s" % rc)
        print(err.getvalue().strip())
        self.assertEqual(rc, 1)
        key.assert_not_called()
        post.assert_not_called()
        load.assert_not_called()
        self.assertIn(line, err.getvalue())
        self.assertIn("coverage injuries 23/32 teams missing", out.getvalue())


if __name__ == "__main__":
    unittest.main()
