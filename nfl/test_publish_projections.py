"""Nightly projections publish. No network."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr
from datetime import date
from unittest.mock import patch

from nfl.backtest import players_from_depth_chart
from nfl.players import Player
from nfl.projections import week1_score
from nfl.publish_projections import (
    CHUNK_SIZE,
    PROJECTIONS_URL,
    ProjectionsKeyMissing,
    PublishEntry,
    StaleInputs,
    build_entries,
    chunk_rows,
    main,
    maybe_sim,
    parse_args,
    post_projection_rows,
    _team_lines,
    projection_rows,
    resolve_nfl_week,
    summarize,
    write_local,
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

        def load(_args, _today):
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
        self.assertEqual(parse_args([]).sim_mode, "off")
        self.assertEqual(parse_args(["--sim-mode", "team"]).sim_mode, "team")
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

        def load(_args, _today):
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

        def load(args, today):
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

        def load(_args, _today):
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


class SimModePublishTest(unittest.TestCase):
    def test_team_mode_missing_constants_do_not_raise(self) -> None:
        import nfl.sim_team as sim_team
        from nfl.sim import simulate_games

        player = Player(
            pid="warn-qb",
            name="Warn QB",
            position="QB",
            salary=8000,
            team="DET",
            opponent="GB",
            game="GB@DET",
            fppg=None,
            injury="",
            roster_position="QB",
            implied_total=22.0,
            implied_opp=22.0,
            total=44.0,
            spread=0.0,
            depth_rank=1,
        )
        original = sim_team.TEAM_SPREAD_SD
        err = io.StringIO()
        try:
            sim_team.TEAM_SPREAD_SD = None
            with redirect_stderr(err):
                played = simulate_games([player], n=4, seed=1, sim_mode="team")
        finally:
            sim_team.TEAM_SPREAD_SD = original
        self.assertEqual(len(played.draws[player.pid]), 4)
        self.assertIn("fitted constants missing", err.getvalue())

    def test_unknown_sim_mode_exits_before_the_slate(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--dry-run", "--sim-mode", "rates"])
        self.assertEqual(code, 1)
        self.assertIn("unknown sim mode", err.getvalue())


def _without_run_at(rows):
    cleaned = []
    for row in rows or []:
        copy = dict(row)
        copy.pop("run_at", None)
        cleaned.append(copy)
    return cleaned


class SlateCsvPublishTest(unittest.TestCase):
    def _entries(self):
        return build_entries(
            [_line()],
            [
                _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
                _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
            ],
        )

    def _run(self, extra, slate_dir=None):
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        from nfl.publish_projections import SimResult

        entries = self._entries()
        posted = {}

        def capture(rows, key):
            posted["rows"] = json.loads(json.dumps(rows))
            return 2, 0

        def load(_args, _today):
            return 2026, 3, entries

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as reports:
            out = io.StringIO()
            err = io.StringIO()
            patches = [
                patch("nfl.publish_projections.projections_key", return_value="sekrit"),
                patch("nfl.publish_projections.load_slate", load),
                patch(
                    "nfl.publish_projections.maybe_sim",
                    return_value=SimResult(None, "data"),
                ),
                patch("nfl.publish_projections.model_version", return_value="abc1234"),
                patch("nfl.publish_projections.post_projection_rows", capture),
                patch("nfl.report.REPORTS_DIR", Path(reports)),
            ]
            if slate_dir is not None:
                patches.append(patch("nfl.report.DATA_DIR", Path(slate_dir)))
            from contextlib import ExitStack

            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                stack.enter_context(redirect_stdout(out))
                stack.enter_context(redirect_stderr(err))
                rc = main(["--report", "--sim", "0", "--out-dir", tmp, *extra])
            written = list(Path(reports).glob("*.md"))
            report = written[0].read_text(encoding="utf-8") if written else ""
        return rc, posted.get("rows"), report, err.getvalue()

    def test_unusable_slate_posts_the_full_week_and_exits_0(self) -> None:
        import tempfile
        from pathlib import Path

        baseline_rc, baseline, baseline_report, _err = self._run([])
        self.assertEqual(baseline_rc, 0)
        self.assertTrue(baseline)
        self.assertNotIn("warning:", baseline_report)
        baseline_body = _without_run_at(baseline)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            empty = folder / "empty.csv"
            empty.write_text("", encoding="utf-8")
            malformed = folder / "bad.csv"
            malformed.write_text("nope\n", encoding="utf-8")
            zero = folder / "zero.csv"
            zero.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game\n"
                "9,QB,Van Jefferson,4500,TEN,NO,TEN@NO\n",
                encoding="utf-8",
            )
            bad_auto = folder / "auto"
            bad_auto.mkdir()
            (bad_auto / "FanDuel-NFL-bogus-players-list.csv").write_text(
                "Id,Position,Nickname,Salary,Team\n1,QB,X,1,KC\n",
                encoding="utf-8",
            )
            specs = [
                (["--slate-csv", str(folder / "missing.csv")], None),
                (["--slate-csv", str(empty)], None),
                (["--slate-csv", str(malformed)], None),
                (["--slate-csv", str(zero)], None),
                (["--slate-csv", "auto"], bad_auto),
            ]
            for extra, slate_dir in specs:
                rc, posted, report, err = self._run(extra, slate_dir=slate_dir)
                self.assertEqual(rc, 0, err)
                self.assertEqual(_without_run_at(posted), baseline_body)
                self.assertIn("warning:", report)
                self.assertNotIn("publish projections:", err)

    def test_slate_filter_does_not_change_posted_rows(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slate.csv"
            path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game\n"
                "1,QB,Patrick Mahomes,9000,KC,BUF,KC@BUF\n",
                encoding="utf-8",
            )
            base_rc, baseline, _base_report, _err = self._run([])
            rc, posted, report, err = self._run(["--slate-csv", str(path)])
        self.assertEqual(base_rc, 0)
        self.assertEqual(rc, 0, err)
        self.assertEqual(_without_run_at(posted), _without_run_at(baseline))
        names = {row.get("player_name") for row in posted}
        self.assertIn("Josh Allen", names)
        self.assertIn("Patrick Mahomes", names)
        mahomes = next(row for row in posted if row.get("player_name") == "Patrick Mahomes")
        self.assertIsNone(mahomes.get("salary"))
        self.assertIn("Patrick Mahomes", report)
        self.assertNotIn("Josh Allen", report)
        self.assertIn("9,000", report)


if __name__ == "__main__":
    unittest.main()
