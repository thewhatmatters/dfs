"""Nightly projections publish. No network."""

from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import patch

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
    post_projection_rows,
    projection_rows,
    resolve_nfl_week,
    summarize,
    write_local,
)
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
            self.assertIsNone(maybe_sim([entry], 10, 1))

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

        seen: dict = {}

        def fake(players, n, seed):
            seen["n"] = n
            seen["seed"] = seed
            seen["count"] = len(players)
            return Result()

        with patch("nfl.publish_projections.load_simulate_games", return_value=fake):
            out = maybe_sim(entries, 25, 3)
        self.assertEqual(out, {})
        self.assertEqual(seen, {"n": 25, "seed": 3, "count": len(entries)})


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


if __name__ == "__main__":
    unittest.main()
