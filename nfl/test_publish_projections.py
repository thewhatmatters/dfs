"""Nightly projections publish. No network."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from nfl.backtest import players_from_depth_chart
from nfl.players import Player
from nfl.projections import week1_score
from nfl.publish_projections import (
    CHUNK_SIZE,
    PROJECTIONS_URL,
    DatasetRecord,
    ProjectionsKeyMissing,
    PublishEntry,
    RunCapture,
    StaleInputs,
    build_entries,
    build_manifest,
    chunk_rows,
    freshness_warnings,
    load_slate,
    main,
    maybe_sim,
    parse_args,
    post_projection_rows,
    _team_lines,
    projection_rows,
    resolve_nfl_week,
    select_input_run_ids,
    summarize,
    write_local,
    write_manifest,
)
from nfl.sim import simulate_games
from nfl.snaps import SnapWeekRow
from nfl.targets import TargetWeekRow


def _line(*, game_id="2026_03_KC_BUF") -> dict:
    return {
        "game_id": game_id,
        "season": 2026,
        "week": 3,
        "commence_time": "2026-09-20T17:00:00Z",
        "home_team_fd": "BUF",
        "away_team_fd": "KC",
        "spread": -3.5,
        "total": 47.5,
        "home_moneyline": -180,
        "away_moneyline": 155,
    }


def _depth(name: str, team: str, pos: str, rank: int, gsis: str | None) -> dict:
    return {
        "player_name": name,
        "team_fd": team,
        "pos_grp": "QB" if pos == "QB" else "3WR 1TE",
        "pos_abb": pos,
        "pos_rank": rank,
        "gsis_id": gsis,
        "player_id": gsis,
    }


class ChunkPostTest(unittest.TestCase):
    def test_chunks_at_5000(self) -> None:
        rows = [{"i": i} for i in range(CHUNK_SIZE + 1)]
        parts = chunk_rows(rows)
        self.assertEqual(len(parts), 2)
        self.assertEqual(len(parts[0]), CHUNK_SIZE)
        self.assertEqual(len(parts[1]), 1)
        self.assertEqual(len(chunk_rows([{"i": 1}] * CHUNK_SIZE)), 1)

    def test_post_keeps_key_in_header_and_sums_counts(self) -> None:
        captured: list[dict] = []

        def fake(url, body, headers=None, timeout=60):
            captured.append({"url": url, "body": body, "headers": dict(headers or {})})
            n = len(body["rows"])
            return {"inserted": n, "updated": 1 if n < CHUNK_SIZE else 0}, {}

        rows = [{"i": i} for i in range(CHUNK_SIZE + 1)]
        with patch("nfl.publish_projections.http_json_post", fake):
            inserted, updated = post_projection_rows(rows, key="sekrit-write-key")
        self.assertEqual(inserted, CHUNK_SIZE + 1)
        self.assertEqual(updated, 1)
        self.assertEqual(len(captured), 2)
        for call in captured:
            self.assertEqual(call["url"], PROJECTIONS_URL)
            self.assertNotIn("?", call["url"])
            self.assertNotIn("sekrit-write-key", call["url"])
            self.assertNotIn("api_key", call["url"])
            self.assertEqual(call["headers"]["x-api-key"], "sekrit-write-key")
            self.assertEqual(set(call["body"]), {"rows"})
            self.assertNotIn("sekrit-write-key", json.dumps(call["body"]))

    def test_url_guard_rejects_query_key(self) -> None:
        from nfl.publish_projections import PublishError, _post_chunk

        with self.assertRaises(PublishError):
            _post_chunk(PROJECTIONS_URL + "?api_key=sekrit", [{"a": 1}], "sekrit")


class PayloadTest(unittest.TestCase):
    def _entries(self) -> list[PublishEntry]:
        depth = [
            _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
            _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            _depth("Travis Kelce", "KC", "TE", 1, "00-0030506"),
        ]
        targets = [
            TargetWeekRow(
                player="Travis Kelce",
                team="KC",
                position="TE",
                week=2,
                targets=8,
                target_share=0.22,
                targets_avg=8.0,
                targets_total=8,
                source="gangstash",
                asof="2026-09-24",
            )
        ]
        csv_players = [
            Player(
                pid="fd-mahomes",
                name="Patrick Mahomes",
                position="QB",
                salary=9000,
                team="KC",
                opponent="BUF",
                game="KC@BUF",
                fppg=None,
                injury="",
                roster_position="QB",
            )
        ]
        return build_entries(
            [_line()],
            depth,
            target_rows=targets,
            csv_players=csv_players,
        )

    def test_board_shape_matches_week1_score(self) -> None:
        entries = self._entries()
        by_name = {e.player.name: e for e in entries}
        self.assertEqual(by_name["Patrick Mahomes"].gsis_id, "00-0033873")
        self.assertEqual(by_name["Patrick Mahomes"].salary, 9000)
        self.assertEqual(by_name["Patrick Mahomes"].fanduel_id, "fd-mahomes")
        self.assertIsNone(by_name["Kansas City Chiefs"].gsis_id)
        self.assertIsNone(by_name["Kansas City Chiefs"].salary)
        kelce = by_name["Travis Kelce"].player
        self.assertAlmostEqual(
            kelce.objective or 0,
            week1_score(22.0, 1, "TE", target_share=0.22),
        )
        mahomes = by_name["Patrick Mahomes"].player
        self.assertAlmostEqual(mahomes.objective or 0, week1_score(22.0, 1, "QB"))
        rows = projection_rows(
            entries,
            season=2026,
            week=3,
            season_type="REG",
            run_at="2026-09-25T04:00:00+00:00",
            model_version="abc1234",
        )
        self.assertTrue(all(r["model"] == "board" for r in rows))
        self.assertTrue(all(r["p10"] is None and r["p50"] is None and r["p90"] is None for r in rows))
        self.assertTrue(all(r["run_at"] == "2026-09-25T04:00:00+00:00" for r in rows))
        keyed = {r["player_name"]: r for r in rows}
        row = keyed["Patrick Mahomes"]
        for field in (
            "season",
            "week",
            "season_type",
            "run_at",
            "model",
            "model_version",
            "gsis_id",
            "player_id",
            "player_name",
            "team",
            "opponent",
            "position",
            "game_id",
            "salary",
            "mean",
            "p10",
            "p50",
            "p90",
            "inputs",
        ):
            self.assertIn(field, row)
        self.assertEqual(row["gsis_id"], "00-0033873")
        self.assertEqual(row["player_id"], "00-0033873")
        self.assertEqual(row["team"], "KC")
        self.assertEqual(row["opponent"], "BUF")
        self.assertEqual(row["position"], "QB")
        self.assertEqual(row["game_id"], "2026_03_KC_BUF")
        self.assertEqual(row["salary"], 9000)
        self.assertEqual(row["mean"], round(week1_score(22.0, 1, "QB"), 4))
        self.assertEqual(row["model_version"], "abc1234")
        self.assertEqual(row["inputs"]["sources"], "gs-depth")
        self.assertEqual(row["inputs"]["depth_rank"], 1)
        self.assertEqual(row["inputs"]["implied_total"], 22.0)
        self.assertEqual(row["inputs"]["fanduel_id"], "fd-mahomes")
        self.assertEqual(keyed["Travis Kelce"]["inputs"]["sources"], "gs-depth/gs-tgt")
        self.assertEqual(keyed["Travis Kelce"]["inputs"]["usage_factor"], 1.2)
        self.assertEqual(keyed["Kansas City Chiefs"]["position"], "D")
        self.assertIsNone(keyed["Kansas City Chiefs"]["gsis_id"])
        self.assertIn("vegas-dst", keyed["Kansas City Chiefs"]["inputs"]["sources"])
        text = summarize(rows)
        self.assertIn("board: 5 rows", text)
        self.assertIn("missing gsis_id 2", text)
        self.assertNotIn("sim:", text)

    def test_sim_rows_share_run_at(self) -> None:
        entries = self._entries()

        class Stats:
            def __init__(self) -> None:
                self.mean = 12.5
                self.p10 = 6.0
                self.p50 = 11.0
                self.p90 = 20.0

        sim = {e.player.pid: Stats() for e in entries}
        rows = projection_rows(
            entries,
            season=2026,
            week=3,
            season_type="REG",
            run_at="2026-09-25T04:00:00+00:00",
            model_version="abc1234",
            sim_by_pid=sim,
        )
        board = [r for r in rows if r["model"] == "board"]
        sims = [r for r in rows if r["model"] == "sim"]
        self.assertEqual(len(board), len(sims))
        self.assertEqual(sims[0]["p10"], 6.0)
        self.assertEqual(sims[0]["p50"], 11.0)
        self.assertEqual(sims[0]["p90"], 20.0)
        self.assertEqual(sims[0]["mean"], 12.5)
        self.assertTrue(all(r["run_at"] == board[0]["run_at"] for r in sims))
        self.assertIn("sim: 5 rows", summarize(rows))

    def test_sim_rows_name_the_efficiency_mode(self) -> None:
        import csv
        import io
        import tempfile
        from contextlib import redirect_stderr
        from pathlib import Path

        entries = self._entries()

        class Stats:
            def __init__(self) -> None:
                self.mean = 12.5
                self.p10 = 6.0
                self.p50 = 11.0
                self.p90 = 20.0

        sim = {e.player.pid: Stats() for e in entries}
        rows = projection_rows(
            entries,
            season=2026,
            week=3,
            season_type="REG",
            run_at="2026-09-25T04:00:00+00:00",
            model_version="abc1234",
            sim_by_pid=sim,
        )
        board = [r for r in rows if r["model"] == "board"]
        sims = [r for r in rows if r["model"] == "sim"]
        self.assertTrue(all("sim_efficiency" not in r["inputs"] for r in board))
        self.assertTrue(all(r["inputs"]["sim_efficiency"] == "data" for r in sims))

        def load(_args, _today, **_kwargs):
            return 2026, 3, entries

        with patch("nfl.publish_projections.load_slate", load), patch(
            "nfl.publish_projections.maybe_sim", return_value=(sim, "data")
        ), patch(
            "nfl.publish_projections.model_version", return_value="abc1234"
        ), patch("nfl.publish_projections.post_projection_rows") as post:
            with tempfile.TemporaryDirectory() as tmp:
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = main(
                        [
                            "--dry-run",
                            "--out-dir",
                            tmp,
                            "--sim",
                            "10",
                            "--sim-efficiency",
                            "data",
                        ]
                    )
                files = list(Path(tmp).iterdir())
                payload = json.loads(next(p for p in files if p.suffix == ".json").read_text())
                with next(p for p in files if p.suffix == ".csv").open(encoding="utf-8") as fh:
                    written = list(csv.DictReader(fh))
        post.assert_not_called()
        self.assertEqual(rc, 0)
        log = err.getvalue()
        self.assertIn("model_version=abc1234", log)
        self.assertIn("sim_efficiency=data", log)
        json_sims = [r for r in payload["rows"] if r["model"] == "sim"]
        json_board = [r for r in payload["rows"] if r["model"] == "board"]
        self.assertTrue(json_sims)
        self.assertTrue(all(r["inputs"]["sim_efficiency"] == "data" for r in json_sims))
        self.assertTrue(all("sim_efficiency" not in r["inputs"] for r in json_board))
        csv_sims = [r for r in written if r["model"] == "sim"]
        self.assertTrue(
            all(json.loads(r["inputs"])["sim_efficiency"] == "data" for r in csv_sims)
        )


class SimGuardTest(unittest.TestCase):
    def test_missing_sim_publishes_board_only(self) -> None:
        entry = build_entries(
            [_line()],
            [
                _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            ],
        )[0]
        with patch("nfl.publish_projections.load_simulate_games", return_value=None):
            by_pid, mode = maybe_sim([entry], 10, 1, season=2026, refresh=True)
        self.assertIsNone(by_pid)
        self.assertEqual(mode, "data")

    def test_present_sim_is_called_with_draws(self) -> None:
        entries = build_entries(
            [_line()],
            [
                _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            ],
        )

        class Result:
            def __init__(self) -> None:
                self.by_pid = {}
                self.game_draws = (("KC@BUF", "KC", "BUF", 21.0, 24.0),)

        seen: dict = {}

        from nfl.sim_inputs import SimInputs

        bundle = SimInputs()

        def fake(players, n, seed, inputs=None, efficiency=None):
            seen["n"] = n
            seen["seed"] = seed
            seen["count"] = len(players)
            seen["inputs"] = inputs
            seen["efficiency"] = efficiency
            return Result()

        with patch("nfl.publish_projections.load_simulate_games", return_value=fake), patch(
            "nfl.sim_feed.resolve_sim_inputs",
            return_value=(bundle, "sim inputs: test"),
        ):
            out = maybe_sim(entries, 25, 3, season=2026, refresh=True)
        by_pid, mode = out
        self.assertEqual(by_pid, {})
        self.assertEqual(mode, "data")
        self.assertEqual(out.game_draws, (("KC@BUF", "KC", "BUF", 21.0, 24.0),))
        self.assertIs(seen["inputs"], bundle)
        self.assertEqual(seen["n"], 25)
        self.assertEqual(seen["seed"], 3)
        self.assertEqual(seen["count"], len(entries))
        from nfl.sim_efficiency import DataEfficiency

        self.assertIsInstance(seen["efficiency"], DataEfficiency)

    def test_data_efficiency_is_passed_into_the_sim(self) -> None:
        from nfl.sim_efficiency import DataEfficiency
        from nfl.sim_inputs import SimInputs

        entries = build_entries(
            [_line()],
            [
                _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            ],
        )
        seen: dict = {}

        def fake(players, n, seed, inputs=None, efficiency=None):
            del players, n, seed, inputs
            seen["efficiency"] = efficiency

            class Result:
                by_pid: dict = {}

            return Result()

        with patch("nfl.publish_projections.load_simulate_games", return_value=fake), patch(
            "nfl.sim_feed.resolve_sim_inputs",
            return_value=(SimInputs(), "sim inputs: test"),
        ):
            maybe_sim(
                entries,
                10,
                1,
                season=2026,
                refresh=True,
                week=3,
                sim_efficiency="data",
            )
        self.assertIsInstance(seen["efficiency"], DataEfficiency)
        self.assertEqual(parse_args([]).sim_efficiency, "data")
        self.assertEqual(
            parse_args(["--sim-efficiency", "placeholder"]).sim_efficiency,
            "placeholder",
        )

    def test_missing_inputs_publish_the_board_and_do_not_raise(self) -> None:
        import io
        from contextlib import redirect_stderr

        from nfl.sim_efficiency import EFFICIENCY_FALLBACK_NOTE

        entries = build_entries(
            [_line()],
            [
                _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            ],
        )
        err = io.StringIO()
        def boom(*_a, **_k):
            raise AssertionError("sim should not run")

        with patch(
            "nfl.publish_projections.load_simulate_games",
            return_value=boom,
        ), patch(
            "nfl.sim_feed.resolve_sim_inputs",
            return_value=(None, "sim inputs: gangstash unavailable — role shares deterministic"),
        ), redirect_stderr(err):
            by_pid, mode = maybe_sim(entries, 10, 1, season=2026, refresh=True, week=3)
        self.assertIsNone(by_pid)
        self.assertEqual(mode, "placeholder")
        text = err.getvalue()
        self.assertIn(EFFICIENCY_FALLBACK_NOTE, text)
        self.assertIn("sim fell back to board; publishing board only", text)
        self.assertLess(
            text.index(EFFICIENCY_FALLBACK_NOTE),
            text.index("sim fell back to board"),
        )

    def test_stale_sim_inputs_exit(self) -> None:
        entries = build_entries(
            [_line()],
            [
                _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            ],
        )
        with patch(
            "nfl.publish_projections.load_simulate_games",
            return_value=lambda *a, **k: None,
        ), patch(
            "nfl.sim_feed.resolve_sim_inputs",
            return_value=(object(), "sim inputs: gangstash  stale cache"),
        ):
            with self.assertRaises(StaleInputs):
                maybe_sim(entries, 10, 1, season=2026, refresh=True)

    def test_sim_mean_matches_projection_source(self) -> None:
        from nfl.sim import apply_ilp_objective, simulate_games
        from nfl.sim_inputs import SimInputs

        entries = build_entries(
            [_line()],
            [
                _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            ],
        )
        bundle = SimInputs()
        players = [e.player for e in entries]
        with patch(
            "nfl.sim_feed.resolve_sim_inputs",
            return_value=(bundle, "sim inputs: test"),
        ):
            out = maybe_sim(entries, 40, 1, season=2026, refresh=True)
        by_pid, mode = out
        self.assertEqual(mode, "data")
        self.assertTrue(out.game_draws)
        direct = simulate_games(players, n=40, seed=1, inputs=bundle).by_pid
        adjusted = {
            pl.pid: pl
            for pl in apply_ilp_objective(
                players,
                "mean",
                sim_by_pid=by_pid,
                projection_source="sim",
            )
        }
        rows = projection_rows(
            entries,
            season=2026,
            week=3,
            season_type="REG",
            run_at="2026-09-25T04:00:00+00:00",
            model_version="abc",
            sim_by_pid=by_pid,
        )
        sims = {r["player_name"]: r for r in rows if r["model"] == "sim"}
        boards = {r["player_name"]: r for r in rows if r["model"] == "board"}
        self.assertEqual(set(sims), set(boards))
        for entry in entries:
            row = sims[entry.player.name]
            stats = direct[entry.player.pid]
            self.assertEqual(row["mean"], round(adjusted[entry.player.pid].objective, 4))
            self.assertEqual(row["mean"], round(stats.mean, 4))
            self.assertEqual(row["p10"], round(stats.p10, 4))
            self.assertEqual(row["p50"], round(stats.p50, 4))
            self.assertEqual(row["p90"], round(stats.p90, 4))
            self.assertIsNotNone(boards[entry.player.name]["mean"])
            self.assertIsNone(boards[entry.player.name]["p10"])


class ExitTest(unittest.TestCase):
    def test_missing_key_exits_before_fetch(self) -> None:
        with patch(
            "nfl.publish_projections.projections_key",
            side_effect=ProjectionsKeyMissing("GANGSTASH_PROJECTIONS_WRITER_KEY is not set"),
        ), patch("nfl.publish_projections.load_slate") as load, patch(
            "nfl.publish_projections.post_projection_rows"
        ) as post:
            rc = main([])
        self.assertEqual(rc, 1)
        load.assert_not_called()
        post.assert_not_called()

    def test_missing_read_key_names_the_gate(self) -> None:
        import io
        from contextlib import redirect_stderr

        from nfl.gangstash import GangstashDataKeyMissing

        err = io.StringIO()
        with patch(
            "nfl.publish_projections.load_slate",
            side_effect=GangstashDataKeyMissing("GANGSTASH_API_KEY is not set"),
        ), patch("nfl.publish_projections.post_projection_rows") as post, redirect_stderr(
            err
        ):
            rc = main(["--dry-run"])
        self.assertEqual(rc, 1)
        post.assert_not_called()
        text = err.getvalue()
        self.assertIn("publish projections:", text)
        self.assertIn("GANGSTASH_API_KEY", text)
        self.assertIn("game lines", text)
        self.assertIn("depth", text)
        self.assertIn("targets", text)
        self.assertIn("snaps", text)
        self.assertIn("props", text)
        self.assertIn("exits without posting", text)
        self.assertIn("--refresh", text)
        self.assertNotIn("oddsapi", text)
        self.assertNotIn("--lines-source", text)

    def test_fallback_header_prints_the_effective_mode(self) -> None:
        import io
        import tempfile
        from contextlib import redirect_stderr

        from nfl.sim_efficiency import EFFICIENCY_FALLBACK_NOTE

        entries = build_entries(
            [_line()],
            [
                _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            ],
        )

        def load(_args, _today, **_kwargs):
            return 2026, 3, entries

        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "nfl.publish_projections.load_slate", load
        ), patch(
            "nfl.sim_feed.resolve_sim_inputs",
            return_value=(
                None,
                "sim inputs: gangstash unavailable — role shares deterministic",
            ),
        ), redirect_stderr(err):
            rc = main(
                [
                    "--dry-run",
                    "--out-dir",
                    tmp,
                    "--sim",
                    "10",
                    "--sim-efficiency",
                    "data",
                ]
            )
        self.assertEqual(rc, 0)
        text = err.getvalue()
        self.assertLess(
            text.index(EFFICIENCY_FALLBACK_NOTE),
            text.index("sim_efficiency=placeholder"),
        )
        self.assertNotIn("sim_efficiency=data", text)

    def test_stale_week_exits_before_post(self) -> None:
        with patch("nfl.publish_projections.projections_key", return_value="sekrit"), patch(
            "nfl.publish_projections.load_slate",
            side_effect=StaleInputs("no game_lines for 2026 week 3"),
        ), patch("nfl.publish_projections.post_projection_rows") as post:
            rc = main([])
        self.assertEqual(rc, 1)
        post.assert_not_called()

    def test_resolve_empty_week_is_stale(self) -> None:
        def fetch(*, on_date=None, season=None, week=None, refresh=False, cache_day=None):
            if on_date is not None:
                return [], {"cache_stale": False}
            return [], {"cache_stale": False}

        with patch("nfl.publish_projections.fetch_game_lines", fetch):
            with self.assertRaises(StaleInputs):
                resolve_nfl_week(date(2026, 9, 23), refresh=True)

    def test_resolve_stale_cache_for_target_week(self) -> None:
        def fetch(*, on_date=None, season=None, week=None, refresh=False, cache_day=None):
            if season is not None:
                return [_line()], {"cache_stale": True}
            return [_line()], {"cache_stale": False}

        with patch("nfl.publish_projections.fetch_game_lines", fetch):
            with self.assertRaises(StaleInputs) as ctx:
                resolve_nfl_week(date(2026, 9, 23), refresh=False, season=2026, week=3)
        self.assertIn("stale", str(ctx.exception))

    def test_dry_run_writes_without_posting(self) -> None:
        entries = build_entries(
            [_line()],
            [
                _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                _depth("Josh Allen", "BUF", "QB", 1, None),
            ],
        )

        def load(args, today, **_kwargs):
            return 2026, 3, entries

        with patch("nfl.publish_projections.load_slate", load), patch(
            "nfl.publish_projections.post_projection_rows"
        ) as post, patch(
            "nfl.publish_projections.model_version", return_value="abc1234"
        ):
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as tmp:
                rc = main(["--dry-run", "--out-dir", tmp, "--sim", "0"])
                files = list(Path(tmp).iterdir())
                self.assertEqual(rc, 0)
                names = sorted(p.name for p in files)
                self.assertEqual(len(names), 2)
                json_file = next(p for p in files if p.suffix == ".json")
                payload = json.loads(json_file.read_text())
        post.assert_not_called()
        self.assertTrue(any(n.endswith(".csv") for n in names))
        self.assertTrue(any(n.endswith(".json") for n in names))
        self.assertIn("rows", payload)
        self.assertTrue(all(r["model"] == "board" for r in payload["rows"]))
        self.assertTrue(any(r["gsis_id"] is None for r in payload["rows"]))


class DepthFetchTest(unittest.TestCase):
    def test_unfiltered_400_falls_back_to_position(self) -> None:
        from nfl.gangstash import GangstashDataError
        from nfl.publish_projections import _load_depth

        def fetch(*, team=None, position=None, pos_grp=None, refresh=False, cache_day=None):
            if position is None:
                raise GangstashDataError("HTTP 400: pos_grp required")
            if position == "QB":
                return [_depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873")], {
                    "cache_stale": False
                }
            return [], {"cache_stale": False}

        with patch("nfl.publish_projections.fetch_depth_charts", fetch):
            rows, _meta = _load_depth(True)
        self.assertEqual(rows[0]["player_name"], "Patrick Mahomes")


class EmptyUsageTest(unittest.TestCase):
    def test_empty_targets_are_not_stale(self) -> None:
        from nfl.publish_projections import _optional_window

        self.assertEqual(_optional_window("targets", [], lambda rows: rows), [])


class WriteLocalTest(unittest.TestCase):
    def test_json_is_post_body(self) -> None:
        import tempfile
        from pathlib import Path

        rows = projection_rows(
            build_entries(
                [_line()],
                [
                    _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                    _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
                ],
            ),
            season=2026,
            week=3,
            season_type="REG",
            run_at="2026-09-25T04:00:00+00:00",
            model_version="abc",
        )
        with tempfile.TemporaryDirectory() as tmp:
            json_path, csv_path = write_local(rows, Path(tmp), "2026-w03")
            body = json.loads(json_path.read_text())
            text = csv_path.read_text()
        self.assertEqual(body["rows"][0]["player_name"], rows[0]["player_name"])
        self.assertIn("player_name", text)
        self.assertIn("gs-depth", text)


class ReportFlagTest(unittest.TestCase):
    def _entries(self):
        return build_entries(
            [_line()],
            [
                _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            ],
        )

    def test_dry_run_writes_report_and_sidecar(self) -> None:
        import io
        import tempfile
        from contextlib import redirect_stderr, redirect_stdout
        from pathlib import Path

        from nfl.publish_projections import SimResult

        entries = self._entries()

        class Stats:
            mean = 12.5
            p10 = 6.0
            p50 = 11.0
            p90 = 20.0

        draws = tuple(
            ("KC@BUF", "KC", "BUF", float(i), float(i) + 3) for i in range(1, 11)
        )
        result = SimResult(
            {entry.player.pid: Stats() for entry in entries},
            "data",
            draws,
        )

        def load(_args, _today, **_kwargs):
            return 2026, 3, entries

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as reports:
            out = io.StringIO()
            err = io.StringIO()
            with patch("nfl.publish_projections.load_slate", load), patch(
                "nfl.publish_projections.maybe_sim", return_value=result
            ), patch(
                "nfl.publish_projections.model_version", return_value="abc1234"
            ), patch("nfl.publish_projections.post_projection_rows") as post, patch(
                "nfl.report.REPORTS_DIR", Path(reports)
            ), redirect_stdout(out), redirect_stderr(err):
                rc = main(["--dry-run", "--report", "--sim", "10", "--out-dir", tmp])
            written = list(Path(reports).glob("*.md"))
            sidecar = list(Path(reports).glob("*-games.json"))
            self.assertEqual(len(written), 1)
            self.assertEqual(len(sidecar), 1)
            report_text = written[0].read_text(encoding="utf-8")
            body = json.loads(sidecar[0].read_text(encoding="utf-8"))
            report_path = str(written[0])
        post.assert_not_called()
        self.assertEqual(rc, 0)
        path = out.getvalue().strip().splitlines()[-1]
        self.assertEqual(path, report_path)
        self.assertIn("run_at", body)
        self.assertAlmostEqual(body["games"][0]["away_median"], 5.5)
        self.assertIn("draws 10", report_text)

    def test_failed_post_does_not_write_the_report(self) -> None:
        from nfl.publish_projections import PublishError, SimResult

        entries = self._entries()
        with patch("nfl.publish_projections.projections_key", return_value="sekrit"), patch(
            "nfl.publish_projections.load_slate", return_value=(2026, 3, entries)
        ), patch(
            "nfl.publish_projections.maybe_sim", return_value=SimResult({}, "data")
        ), patch(
            "nfl.publish_projections.post_projection_rows",
            side_effect=PublishError("nope"),
        ), patch("nfl.report.write_report") as write:
            rc = main(["--report", "--sim", "10"])
        self.assertEqual(rc, 1)
        write.assert_not_called()


def _no_ari_line() -> dict:
    return {
        "game_id": "2025_05_ARI_NO",
        "season": 2025,
        "week": 5,
        "commence_time": "2025-10-05T17:00:00Z",
        "home_team_fd": "NO",
        "away_team_fd": "ARI",
        "spread": -3.0,
        "total": 45.0,
        "home_moneyline": -160,
        "away_moneyline": 140,
    }


def _board_rows(depth: list[dict], **kwargs) -> list[dict]:
    entries = build_entries([_no_ari_line()], depth, **kwargs)
    return projection_rows(
        entries,
        season=2025,
        week=5,
        season_type="REG",
        run_at="2025-10-05T16:00:00+00:00",
        model_version="fixture",
    )


def _row_bytes(rows: list[dict]) -> dict[tuple, str]:
    out = {}
    for row in rows:
        if row.get("model") != "board":
            continue
        key = (row.get("player_name"), row.get("team"), row.get("position"))
        out[key] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return out


class DuplicateDepthPidTest(unittest.TestCase):
    """One player id on two depth rows is one pool row.

    Mocks the 2025 Taysom Hill chart (QB2 and TE2). No gangstash call.
    """

    def _depth(self) -> list[dict]:
        return [
            _depth("Derek Carr", "NO", "QB", 1, "pid-carr"),
            _depth("Taysom Hill", "NO", "QB", 2, "pid-hill"),
            _depth("Taysom Hill", "NO", "TE", 2, "pid-hill"),
            _depth("Alvin Kamara", "NO", "RB", 1, "pid-kamara"),
            _depth("Kyler Murray", "ARI", "QB", 1, "pid-kyler"),
        ]

    def _usage(self):
        targets = [
            TargetWeekRow(
                player="Taysom Hill",
                team="NO",
                position="TE",
                week=4,
                targets=4,
                target_share=0.11,
                targets_avg=4.0,
                targets_total=4,
                source="gangstash",
                asof="2025-10-01",
            ),
            TargetWeekRow(
                player="Alvin Kamara",
                team="NO",
                position="RB",
                week=4,
                targets=5,
                target_share=0.14,
                targets_avg=5.0,
                targets_total=5,
                source="gangstash",
                asof="2025-10-01",
            ),
        ]
        snaps = [
            SnapWeekRow(
                player="Taysom Hill",
                team="NO",
                position="TE",
                week=4,
                snaps=28,
                snap_share=0.42,
                snaps_avg=28.0,
                snaps_total=28,
                team_snap_pct=None,
                source="gangstash",
                asof="2025-10-01",
            ),
            SnapWeekRow(
                player="Alvin Kamara",
                team="NO",
                position="RB",
                week=4,
                snaps=45,
                snap_share=0.68,
                snaps_avg=45.0,
                snaps_total=45,
                team_snap_pct=None,
                source="gangstash",
                asof="2025-10-01",
            ),
        ]
        return targets, snaps

    def test_duplicate_pid_one_player_and_one_draw_array(self) -> None:
        n_draws = 40
        targets, snaps = self._usage()
        err = io.StringIO()
        with redirect_stderr(err):
            entries = build_entries(
                [_no_ari_line()],
                self._depth(),
                target_rows=targets,
                snap_rows=snaps,
            )
        hills = [entry for entry in entries if entry.player.pid == "pid-hill"]
        self.assertEqual(len(hills), 1)
        hill = hills[0].player
        self.assertEqual(hill.position, "TE")
        self.assertEqual(hill.depth_rank, 2)
        self.assertEqual(hill.target_share, 0.11)
        self.assertEqual(hill.snap_share, 0.42)
        self.assertEqual(
            [entry.player.pid for entry in entries].count("pid-hill"),
            1,
        )
        kamara = next(entry.player for entry in entries if entry.player.pid == "pid-kamara")
        self.assertEqual(kamara.target_share, 0.14)
        self.assertEqual(kamara.snap_share, 0.68)
        text = err.getvalue()
        self.assertIn(
            "depth collapse pid-hill positions=QB,TE chose=TE",
            text,
        )
        sim = simulate_games([entry.player for entry in entries], n=n_draws, seed=1)
        self.assertEqual(len(sim.draws["pid-hill"]), n_draws)
        self.assertEqual(len(sim.draws["pid-kamara"]), n_draws)
        self.assertEqual(len(sim.draws["pid-carr"]), n_draws)

        parsed = _team_lines([_no_ari_line()])
        by_team = {team: pair[0] for team, pair in parsed.items()}
        with redirect_stderr(io.StringIO()):
            pool = players_from_depth_chart(self._depth(), by_team)
        hill_rows = [pl for pl in pool if pl.pid == "pid-hill"]
        self.assertEqual(len(hill_rows), 1)
        self.assertEqual(hill_rows[0].position, "TE")
        pool_sim = simulate_games(pool, n=n_draws, seed=2)
        self.assertEqual(len(pool_sim.draws["pid-hill"]), n_draws)
        self.assertEqual(
            sum(1 for pl in pool if pl.pid == "pid-hill"),
            1,
        )

    def test_qb1_never_demoted(self) -> None:
        depth = [
            _depth("Taysom Hill", "NO", "TE", 1, "pid-hill"),
            _depth("Taysom Hill", "NO", "QB", 1, "pid-hill"),
            _depth("Kyler Murray", "ARI", "QB", 1, "pid-kyler"),
        ]
        csv = [
            Player(
                pid="fd-hill",
                name="Taysom Hill",
                position="TE",
                salary=5400,
                team="NO",
                opponent="ARI",
                game="ARI@NO",
                fppg=None,
                injury="",
                roster_position="TE",
            )
        ]
        err = io.StringIO()
        with redirect_stderr(err):
            bare = build_entries([_no_ari_line()], depth)
            listed = build_entries([_no_ari_line()], depth, csv_players=csv)
        for entries in (bare, listed):
            hills = [entry for entry in entries if entry.player.pid == "pid-hill"]
            self.assertEqual(len(hills), 1)
            self.assertEqual(hills[0].player.position, "QB")
            self.assertEqual(hills[0].player.depth_rank, 1)
        hill_entry = next(entry for entry in listed if entry.player.pid == "pid-hill")
        self.assertIsNone(hill_entry.salary)
        self.assertIsNone(hill_entry.fanduel_id)
        self.assertIn("depth collapse pid-hill positions=TE,QB chose=QB", err.getvalue())

    def test_fanduel_position_wins(self) -> None:
        depth = [
            _depth("Taysom Hill", "NO", "QB", 2, "pid-hill"),
            _depth("Taysom Hill", "NO", "TE", 4, "pid-hill"),
            _depth("Derek Carr", "NO", "QB", 1, "pid-carr"),
            _depth("Kyler Murray", "ARI", "QB", 1, "pid-kyler"),
        ]
        csv = [
            Player(
                pid="fd-hill",
                name="Taysom Hill",
                position="TE",
                salary=5400,
                team="NO",
                opponent="ARI",
                game="ARI@NO",
                fppg=None,
                injury="",
                roster_position="TE",
            )
        ]
        err = io.StringIO()
        with redirect_stderr(err):
            by_rank = build_entries([_no_ari_line()], depth)
            by_csv = build_entries([_no_ari_line()], depth, csv_players=csv)
        rank_hill = next(entry for entry in by_rank if entry.player.pid == "pid-hill")
        csv_hill = next(entry for entry in by_csv if entry.player.pid == "pid-hill")
        self.assertEqual(len([e for e in by_csv if e.player.pid == "pid-hill"]), 1)
        self.assertEqual(rank_hill.player.position, "QB")
        self.assertEqual(rank_hill.player.depth_rank, 2)
        self.assertEqual(csv_hill.player.position, "TE")
        self.assertEqual(csv_hill.player.depth_rank, 4)
        self.assertEqual(csv_hill.salary, 5400)
        self.assertEqual(csv_hill.fanduel_id, "fd-hill")
        self.assertIn("depth collapse pid-hill positions=QB,TE chose=TE", err.getvalue())
        self.assertIn("depth collapse pid-hill positions=QB,TE chose=QB", err.getvalue())

    def test_same_rank_tie_break(self) -> None:
        cases = (
            (("RB", 2, "WR", 2), "RB"),
            (("WR", 2, "TE", 2), "TE"),
            (("QB", 2, "WR", 2), "WR"),
            (("QB", 2, "RB", 2), "RB"),
        )
        for (left, left_rank, right, right_rank), chosen in cases:
            depth = [
                _depth("Dual Skill", "NO", left, left_rank, "pid-dual"),
                _depth("Dual Skill", "NO", right, right_rank, "pid-dual"),
                _depth("Kyler Murray", "ARI", "QB", 1, "pid-kyler"),
            ]
            with redirect_stderr(io.StringIO()):
                entries = build_entries([_no_ari_line()], depth)
            hit = [entry for entry in entries if entry.player.pid == "pid-dual"]
            self.assertEqual(len(hit), 1, chosen)
            self.assertEqual(hit[0].player.position, chosen)
            self.assertEqual(hit[0].player.depth_rank, 2)

    def test_other_players_board_rows_are_byte_identical(self) -> None:
        targets, snaps = self._usage()
        kept = [
            _depth("Derek Carr", "NO", "QB", 1, "pid-carr"),
            _depth("Taysom Hill", "NO", "TE", 2, "pid-hill"),
            _depth("Alvin Kamara", "NO", "RB", 1, "pid-kamara"),
            _depth("Kyler Murray", "ARI", "QB", 1, "pid-kyler"),
        ]
        both = [
            _depth("Derek Carr", "NO", "QB", 1, "pid-carr"),
            _depth("Taysom Hill", "NO", "QB", 2, "pid-hill"),
            _depth("Taysom Hill", "NO", "TE", 2, "pid-hill"),
            _depth("Alvin Kamara", "NO", "RB", 1, "pid-kamara"),
            _depth("Kyler Murray", "ARI", "QB", 1, "pid-kyler"),
        ]
        kwargs = {"target_rows": targets, "snap_rows": snaps}
        with redirect_stderr(io.StringIO()):
            baseline = _row_bytes(_board_rows(kept, **kwargs))
            collapsed = _row_bytes(_board_rows(both, **kwargs))
        self.assertEqual(collapsed, baseline)
        others = {key: value for key, value in collapsed.items() if key[0] != "Taysom Hill"}
        self.assertGreaterEqual(len(others), 4)
        for key, blob in others.items():
            self.assertEqual(blob, baseline[key])

    def test_missing_player_id_is_not_collapsed(self) -> None:
        depth = [
            _depth("Two Names", "NO", "QB", 2, None),
            _depth("Two Names", "NO", "TE", 2, None),
            _depth("Kyler Murray", "ARI", "QB", 1, "pid-kyler"),
        ]
        err = io.StringIO()
        with redirect_stderr(err):
            entries = build_entries([_no_ari_line()], depth)
        rows = [entry for entry in entries if entry.player.name == "Two Names"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({entry.player.position for entry in rows}, {"QB", "TE"})
        self.assertNotIn("depth collapse", err.getvalue())


class ProvenanceTest(unittest.TestCase):
    def _board(self):
        return [
            _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
            _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            _depth("Travis Kelce", "KC", "TE", 1, "00-0030506"),
        ]

    def _props(self):
        return [
            {
                "player_name": "Patrick Mahomes",
                "prop": "Passing Yards",
                "line": 275.5,
                "scraped_at": "2026-09-25T12:00:00Z",
            }
        ]

    def _meta(self, observed="2026-09-25T12:00:00+00:00", untimestamped=False):
        return {
            "cache": None,
            "cache_stale": False,
            "truncated": False,
            "live": True,
            "response_meta": {
                "observed_at": observed,
                "untimestamped": untimestamped,
                "row_count": 1,
                "timestamp_column": "captured_at",
            },
        }

    def test_default_means_match_current_sources(self) -> None:
        """Provenance does not change the numbers the current datasets produce."""
        lines = [_line()]
        depth = self._board()
        props = self._props()
        prop_meta = self._meta()
        before = build_entries(lines, depth)
        with patch("nfl.props.fetch_props", return_value=(props, prop_meta)):
            from nfl.props import ingest_slate_props
            from nfl.props import attach_props

            by_pid, _stats = ingest_slate_props([entry.player for entry in before])
        before_players = attach_props([entry.player for entry in before], by_pid)
        before_means = {
            player.name: round(float(player.objective or 0), 4) for player in before_players
        }

        def lines_fetch(**kwargs):
            return lines, self._meta("2026-09-25T11:00:00+00:00")

        def depth_fetch(**kwargs):
            return depth, self._meta()

        def empty_fetch(**kwargs):
            return [], self._meta()

        runs = [
            {
                "run_id": "11111111-1111-1111-1111-111111111111",
                "collector": "bettingpros-odds",
                "started_at": "2026-09-25T10:00:00Z",
                "finished_at": "2026-09-25T10:05:00Z",
                "status": "succeeded",
            },
            {
                "run_id": "22222222-2222-2222-2222-222222222222",
                "collector": "bettingpros-odds",
                "started_at": "2026-09-25T15:00:00Z",
                "finished_at": "2026-09-25T15:05:00Z",
                "status": "succeeded",
            },
            {
                "run_id": "33333333-3333-3333-3333-333333333333",
                "collector": "bettingpros-pbcs",
                "started_at": "2026-09-25T09:00:00Z",
                "finished_at": "2026-09-25T09:05:00Z",
                "status": "failed",
            },
        ]
        args = parse_args(["--season", "2026", "--week", "3", "--sim", "0"])
        as_of = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        with patch("nfl.publish_projections.fetch_game_lines", lines_fetch), patch(
            "nfl.publish_projections.fetch_depth_charts", depth_fetch
        ), patch("nfl.publish_projections.fetch_targets", empty_fetch), patch(
            "nfl.publish_projections.fetch_snaps", empty_fetch
        ), patch(
            "nfl.sim_feed.load_week_injuries",
            return_value=([], self._meta()),
        ), patch("nfl.props.fetch_props", return_value=(list(props), self._meta())), patch(
            "nfl.publish_projections.fetch_game_line_snapshots"
        ) as snapshots, patch(
            "nfl.publish_projections.fetch_props_snapshots"
        ) as prop_snaps, patch(
            "nfl.publish_projections.fetch_depth_charts_weekly"
        ) as weekly, patch(
            "nfl.publish_projections.fetch_injury_snapshots"
        ) as injuries, patch(
            "nfl.publish_projections.fetch_collector_runs",
            return_value=(runs, self._meta()),
        ):
            _season, _week, entries, capture = load_slate(
                args,
                date(2026, 9, 25),
                as_of=as_of,
                point_in_time=False,
            )
        snapshots.assert_not_called()
        prop_snaps.assert_not_called()
        weekly.assert_not_called()
        injuries.assert_not_called()
        after_means = {
            entry.player.name: round(float(entry.player.objective or 0), 4)
            for entry in entries
        }
        self.assertEqual(after_means, before_means)
        stamped = projection_rows(
            entries,
            season=2026,
            week=3,
            season_type="REG",
            run_at="2026-09-25T12:00:00+00:00",
            model_version="abc",
            as_of="2026-09-25T12:00:00+00:00",
            input_run_ids=capture.input_run_ids,
        )
        bare = projection_rows(
            entries,
            season=2026,
            week=3,
            season_type="REG",
            run_at="2026-09-25T12:00:00+00:00",
            model_version="abc",
        )
        self.assertEqual([row["mean"] for row in stamped], [row["mean"] for row in bare])
        self.assertTrue(all(row["as_of"] == "2026-09-25T12:00:00+00:00" for row in stamped))
        self.assertEqual(
            stamped[0]["input_run_ids"],
            ["11111111-1111-1111-1111-111111111111"],
        )
        self.assertIn("game_lines", capture.datasets)
        self.assertIn("injuries", capture.datasets)
        self.assertNotIn("game_line_snapshots", capture.datasets)
        self.assertNotIn("injury_snapshots", capture.datasets)

    def test_default_out_matches_the_main_injury_path(self) -> None:
        """No --as-of uses dataset=injuries and the same drop-and-promote as main."""
        from nfl.publish_projections import _apply_entry_injuries
        from nfl.props import attach_props, ingest_slate_props

        lines = [_line()]
        depth = self._board() + [_depth("Carson Wentz", "KC", "QB", 2, "00-0033102")]
        injury_rows = [
            {
                "season": 2026,
                "week": 3,
                "full_name": "Patrick Mahomes",
                "team_fd": "KC",
                "gsis_id": "00-0033873",
                "report_status": "Out",
            }
        ]
        before = build_entries(
            lines,
            depth,
            injury_rows=injury_rows,
            injury_season=2026,
            injury_week=3,
        )
        with patch("nfl.props.fetch_props", return_value=([], self._meta())):
            by_pid, _stats = ingest_slate_props([entry.player for entry in before])
        scored = attach_props([entry.player for entry in before], by_pid)
        by_scored = {player.pid: player for player in scored}
        before = [
            PublishEntry(
                player=by_scored[entry.player.pid],
                gsis_id=entry.gsis_id,
                player_id=entry.player_id,
                game_id=entry.game_id,
                fanduel_id=entry.fanduel_id,
                salary=entry.salary,
            )
            for entry in before
        ]
        before = _apply_entry_injuries(
            before,
            injury_rows,
            season=2026,
            week=3,
            csv_players=None,
        )
        before_means = {
            entry.player.name: round(float(entry.player.objective or 0), 4)
            for entry in before
        }
        runs = [
            {
                "run_id": "44444444-4444-4444-4444-444444444444",
                "collector": "nflverse-injuries",
                "started_at": "2026-09-25T11:00:00Z",
                "finished_at": "2026-09-25T11:10:00Z",
                "status": "succeeded",
            },
            {
                "run_id": "55555555-5555-5555-5555-555555555555",
                "collector": "nflverse-injuries",
                "started_at": "2026-09-25T17:00:00Z",
                "finished_at": "2026-09-25T18:00:00Z",
                "status": "succeeded",
            },
        ]
        args = parse_args(["--season", "2026", "--week", "3", "--sim", "0"])
        with patch(
            "nfl.publish_projections.fetch_game_lines",
            return_value=(lines, self._meta()),
        ), patch(
            "nfl.publish_projections.fetch_depth_charts",
            return_value=(depth, self._meta()),
        ), patch(
            "nfl.publish_projections.fetch_targets",
            return_value=([], self._meta()),
        ), patch(
            "nfl.publish_projections.fetch_snaps",
            return_value=([], self._meta()),
        ), patch(
            "nfl.sim_feed.load_week_injuries",
            return_value=(injury_rows, self._meta()),
        ) as week_injuries, patch(
            "nfl.props.fetch_props",
            return_value=([], self._meta()),
        ), patch(
            "nfl.publish_projections.fetch_injury_snapshots"
        ) as snapshots, patch(
            "nfl.publish_projections.fetch_collector_runs",
            return_value=(runs, self._meta()),
        ):
            _season, _week, entries, capture = load_slate(
                args,
                date(2026, 9, 25),
                as_of=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
                point_in_time=False,
            )
        week_injuries.assert_called_once()
        snapshots.assert_not_called()
        after_means = {
            entry.player.name: round(float(entry.player.objective or 0), 4)
            for entry in entries
        }
        self.assertEqual(after_means, before_means)
        mahomes = next(entry.player for entry in entries if entry.player.name == "Patrick Mahomes")
        wentz = next(entry.player for entry in entries if entry.player.name == "Carson Wentz")
        self.assertEqual(mahomes.injury, "O")
        self.assertEqual(mahomes.objective, 0.0)
        self.assertEqual(wentz.depth_rank, 1)
        self.assertEqual(
            capture.input_run_ids,
            ["44444444-4444-4444-4444-444444444444"],
        )
        self.assertIn("injuries", capture.datasets)

    def test_explicit_as_of_reads_snapshots_and_skips_baseline_injuries(self) -> None:
        lines = [_line()]
        depth = self._board()
        args = parse_args(["--season", "2026", "--week", "3", "--as-of", "2026-09-25T12:00:00"])
        as_of = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        calls: list[str] = []

        def snapshots(**kwargs):
            calls.append("lines")
            self.assertEqual(kwargs.get("as_of"), "2026-09-25T12:00:00+00:00")
            return [
                {
                    "game_id": "2026_03_KC_BUF",
                    "season": 2026,
                    "week": 3,
                    "kickoff_at": "2026-09-20T17:00:00Z",
                    "home_team_fd": "BUF",
                    "away_team_fd": "KC",
                    "book": "consensus",
                    "market": "spread",
                    "home_line": -3.5,
                    "captured_at": "2026-09-25T11:00:00Z",
                },
                {
                    "game_id": "2026_03_KC_BUF",
                    "season": 2026,
                    "week": 3,
                    "kickoff_at": "2026-09-20T17:00:00Z",
                    "home_team_fd": "BUF",
                    "away_team_fd": "KC",
                    "book": "consensus",
                    "market": "total",
                    "total": 47.5,
                    "captured_at": "2026-09-25T11:00:00Z",
                },
            ], self._meta("2026-09-25T11:00:00+00:00")

        def weekly(**kwargs):
            calls.append("depth")
            self.assertEqual(kwargs.get("as_of"), "2026-09-25T12:00:00+00:00")
            return depth, self._meta()

        def prop_snaps(**kwargs):
            calls.append("props")
            return [
                {
                    "player_name": "Patrick Mahomes",
                    "prop": "Passing Yards",
                    "line": 275.5,
                    "captured_at": "2026-09-25T11:30:00Z",
                }
            ], self._meta()

        def injury_snaps(**kwargs):
            calls.append("injuries")
            return [
                {
                    "season": 2026,
                    "week": 3,
                    "full_name": "Patrick Mahomes",
                    "team_fd": "KC",
                    "gsis_id": "00-0033873",
                    "report_status": "Out",
                    "is_baseline": True,
                    "captured_at": "2026-09-01T00:00:00Z",
                }
            ], {
                "cache_stale": False,
                "response_meta": {
                    "observed_at": "2026-09-01T00:00:00+00:00",
                    "untimestamped": False,
                    "includes_baseline": True,
                },
            }

        with patch("nfl.publish_projections.fetch_game_lines") as current_lines, patch(
            "nfl.publish_projections.fetch_game_line_snapshots", snapshots
        ), patch("nfl.publish_projections.fetch_depth_charts") as current_depth, patch(
            "nfl.publish_projections.fetch_depth_charts_weekly", weekly
        ), patch("nfl.publish_projections.fetch_targets", return_value=([], self._meta())), patch(
            "nfl.publish_projections.fetch_snaps", return_value=([], self._meta())
        ), patch("nfl.publish_projections.fetch_props_snapshots", prop_snaps), patch(
            "nfl.props.fetch_props"
        ) as current_props, patch(
            "nfl.publish_projections.fetch_injury_snapshots", injury_snaps
        ), patch(
            "nfl.sim_feed.load_week_injuries",
            side_effect=AssertionError("default injuries feed"),
        ), patch(
            "nfl.publish_projections.fetch_collector_runs",
            return_value=(
                [
                    {
                        "run_id": "44444444-4444-4444-4444-444444444444",
                        "collector": "nflverse-injuries",
                        "started_at": "2026-09-25T11:00:00Z",
                        "finished_at": "2026-09-25T11:40:00Z",
                        "status": "succeeded",
                    }
                ],
                self._meta(),
            ),
        ):
            _season, _week, entries, capture = load_slate(
                args,
                date(2026, 9, 25),
                as_of=as_of,
                point_in_time=True,
            )
        current_lines.assert_not_called()
        current_depth.assert_not_called()
        current_props.assert_not_called()
        self.assertEqual(calls, ["lines", "depth", "injuries", "props"])
        mahomes = next(entry.player for entry in entries if entry.player.name == "Patrick Mahomes")
        self.assertEqual(mahomes.depth_rank, 1)
        self.assertEqual(mahomes.injury, "")
        self.assertIsNone(capture.datasets["injury_snapshots"].observed_at)
        self.assertEqual(capture.datasets["injury_snapshots"].rows, [])
        direct = build_entries(lines, depth)
        with patch(
            "nfl.props.fetch_props",
            return_value=(self._props(), self._meta()),
        ):
            from nfl.props import attach_props, ingest_slate_props

            by_pid, _stats = ingest_slate_props([entry.player for entry in direct])
        direct_players = {
            player.name: player
            for player in attach_props([entry.player for entry in direct], by_pid)
        }
        pit = {entry.player.name: entry.player for entry in entries}
        self.assertEqual(
            round(float(pit["Patrick Mahomes"].objective or 0), 4),
            round(float(direct_players["Patrick Mahomes"].objective or 0), 4),
        )
        self.assertEqual(
            capture.input_run_ids,
            ["44444444-4444-4444-4444-444444444444"],
        )

    def test_real_inactive_at_as_of_changes_the_chart(self) -> None:
        depth = self._board()
        args = parse_args(["--season", "2026", "--week", "3", "--as-of", "2026-09-25T18:00:00Z"])

        def snapshots(**kwargs):
            row = _line()
            return [
                {
                    "game_id": row["game_id"],
                    "season": 2026,
                    "week": 3,
                    "kickoff_at": row["commence_time"],
                    "home_team_fd": "BUF",
                    "away_team_fd": "KC",
                    "book": "consensus",
                    "market": "spread",
                    "home_line": row["spread"],
                    "captured_at": "2026-09-25T16:00:00Z",
                },
                {
                    "game_id": row["game_id"],
                    "season": 2026,
                    "week": 3,
                    "kickoff_at": row["commence_time"],
                    "home_team_fd": "BUF",
                    "away_team_fd": "KC",
                    "book": "consensus",
                    "market": "total",
                    "total": row["total"],
                    "captured_at": "2026-09-25T16:00:00Z",
                },
            ], self._meta("2026-09-25T16:00:00+00:00")

        def injuries(**kwargs):
            return [
                {
                    "season": 2026,
                    "week": 3,
                    "full_name": "Patrick Mahomes",
                    "team_fd": "KC",
                    "gsis_id": "00-0033873",
                    "report_status": "Out",
                    "is_baseline": False,
                    "captured_at": "2026-09-25T17:00:00Z",
                }
            ], self._meta("2026-09-25T17:00:00+00:00")

        with patch("nfl.publish_projections.fetch_game_line_snapshots", snapshots), patch(
            "nfl.publish_projections.fetch_depth_charts_weekly",
            return_value=(depth, self._meta()),
        ), patch(
            "nfl.publish_projections.fetch_targets", return_value=([], self._meta())
        ), patch(
            "nfl.publish_projections.fetch_snaps", return_value=([], self._meta())
        ), patch(
            "nfl.publish_projections.fetch_props_snapshots",
            return_value=([], self._meta()),
        ), patch(
            "nfl.publish_projections.fetch_injury_snapshots", injuries
        ), patch(
            "nfl.sim_feed.load_week_injuries",
            side_effect=AssertionError("default injuries feed"),
        ), patch(
            "nfl.publish_projections.fetch_collector_runs",
            return_value=([], self._meta()),
        ):
            _season, _week, entries, capture = load_slate(
                args,
                date(2026, 9, 25),
                as_of=datetime(2026, 9, 25, 18, 0, tzinfo=timezone.utc),
                point_in_time=True,
            )
        mahomes = next(entry.player for entry in entries if entry.player.name == "Patrick Mahomes")
        self.assertEqual(mahomes.injury, "O")
        self.assertEqual(mahomes.objective, 0.0)
        self.assertIsNone(mahomes.depth_rank)
        self.assertEqual(
            capture.datasets["injury_snapshots"].observed_at,
            "2026-09-25T17:00:00+00:00",
        )
        self.assertEqual(len(capture.datasets["injury_snapshots"].rows), 1)

    def test_selects_latest_succeeded_run_before_as_of(self) -> None:
        as_of = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        ids = select_input_run_ids(
            [
                {
                    "run_id": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
                    "collector": "nflverse-targets",
                    "finished_at": "2026-09-25T11:00:00Z",
                    "status": "succeeded",
                },
                {
                    "run_id": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
                    "collector": "nflverse-targets",
                    "finished_at": "2026-09-25T13:00:00Z",
                    "status": "succeeded",
                },
                {
                    "run_id": "CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC",
                    "collector": "nflverse-snaps",
                    "finished_at": "2026-09-25T11:30:00Z",
                    "status": "partial",
                },
            ],
            as_of=as_of,
        )
        self.assertEqual(ids, ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"])

    def test_stale_and_untimestamped_warn_on_stderr_and_the_report(self) -> None:
        import io
        import tempfile
        from contextlib import redirect_stderr, redirect_stdout
        from pathlib import Path

        from nfl.report import build_report

        capture = RunCapture()
        old = (datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc) - timedelta(hours=40)).isoformat()
        capture.datasets["game_lines"] = DatasetRecord(
            rows=[{"game_id": "g"}],
            observed_at=old,
            untimestamped=False,
        )
        capture.datasets["depth_charts_weekly"] = DatasetRecord(
            rows=[{"player_name": "A", "chart_format": "nflverse_weekly"}],
            observed_at="2026-09-25T11:00:00+00:00",
            untimestamped=True,
        )
        now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        warnings = freshness_warnings(capture, now=now)
        self.assertEqual(len(warnings), 2)
        stale = next(line for line in warnings if "game_lines" in line)
        legacy = next(line for line in warnings if "depth_charts_weekly" in line)
        self.assertIn("36h", stale)
        self.assertIn("untimestamped", legacy)
        capture.line_rows = [{"game_id": "g", "home_team": "KC", "away_team": "BUF"}]
        text = build_report(
            [],
            [],
            season=2026,
            week=3,
            run_at="2026-09-25T12:00:00+00:00",
            draws=0,
            efficiency="data",
            notes=warnings,
        )
        self.assertTrue(text.strip().endswith("stale inputs: " + "; ".join(warnings)))
        entries = build_entries(
            [_line()],
            [
                _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            ],
        )

        def load(_args, _today, **_kwargs):
            return 2026, 3, entries, capture

        with tempfile.TemporaryDirectory() as tmp, patch(
            "nfl.publish_projections.load_slate", load
        ), patch("nfl.publish_projections.maybe_sim", return_value=(None, "data")), patch(
            "nfl.publish_projections.model_version", return_value="abc"
        ), patch("nfl.report.REPORTS_DIR", Path(tmp)):
            err = io.StringIO()
            out = io.StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                rc = main(
                    ["--dry-run", "--report", "--sim", "0", "--out-dir", tmp, "--season", "2026", "--week", "3"]
                )
            self.assertEqual(rc, 0)
            self.assertIn("stale inputs:", err.getvalue())
            self.assertIn("untimestamped", err.getvalue())
            report = next(Path(tmp).glob("*.md")).read_text(encoding="utf-8")
            self.assertIn("stale inputs:", report)
            manifest = json.loads(next(Path(tmp).glob("*.manifest.json")).read_text(encoding="utf-8"))
            for key in (
                "git_sha",
                "dirty",
                "seed",
                "draws",
                "as_of",
                "cli_args",
                "datasets",
                "input_run_ids",
                "python",
            ):
                self.assertIn(key, manifest)
            self.assertIn("game_lines", manifest["datasets"])
            self.assertIn("sha256", manifest["datasets"]["game_lines"])
            self.assertEqual(manifest["datasets"]["game_lines"]["row_count"], 1)
            self.assertTrue(manifest["datasets"]["depth_charts_weekly"]["untimestamped"])
            self.assertEqual(manifest["draws"], 0)
            self.assertEqual(manifest["seed"], 1)
            self.assertFalse(manifest["point_in_time"])
            written = write_manifest(
                build_manifest(capture, seed=1, draws=0, as_of=manifest["as_of"], cli_args=["--dry-run"]),
                season=2026,
                week=3,
                run_at="2026-09-25T12:00:00+00:00",
                dest=Path(tmp) / "again",
            )
            self.assertTrue(written.name.endswith(".manifest.json"))


if __name__ == "__main__":
    unittest.main()
