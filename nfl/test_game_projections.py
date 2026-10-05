"""Game-projection aggregate, anytime TD fields, and the props suffix join."""

from __future__ import annotations

import json
import math
import unittest
from unittest.mock import patch

from nfl.game_projections import (
    GAME_ROW_FIELDS,
    ON_CONFLICT,
    build_game_projection_rows,
    post_game_projection_rows,
)
from nfl.players import Player
from nfl.publish_projections import PublishEntry, projection_rows
from nfl.sim import anytime_td_summary, simulate_games
from nfl.sim_efficiency import OpportunityCount, PlaceholderEfficiency, ReceivingLine


ATL_NO = "89c7243e-cb87-4fa4-b2c7-068be7c16a68"


def _draws():
    # (game, away, home, away_pts, home_pts)
    return (
        ("ATL@NO", "ATL", "NO", 10.0, 20.0),
        ("ATL@NO", "ATL", "NO", 14.0, 17.0),
        ("ATL@NO", "ATL", "NO", 21.0, 21.0),
        ("ATL@NO", "ATL", "NO", 24.0, 13.0),
    )


def _consensus_row(**extra):
    row = {
        "book": "consensus",
        "game_id": ATL_NO,
        "home_team_fd": "NO",
        "away_team_fd": "ATL",
        "spread": -3.0,
        "total": 37.0,
    }
    row.update(extra)
    return row


class GameAggregateTest(unittest.TestCase):
    def test_tiny_draw_set_matches_cover_and_total(self) -> None:
        rows = build_game_projection_rows(
            _draws(),
            season=2026,
            week=4,
            season_type="REG",
            run_at="2026-10-05T23:00:00+00:00",
            model_version="abc1234",
            line_rows=[
                _consensus_row(),
                {
                    "book": "draftkings",
                    "game_id": ATL_NO,
                    "home_team_fd": "NO",
                    "away_team_fd": "ATL",
                    "spread": -10.0,
                    "total": 51.0,
                },
            ],
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(list(row), list(GAME_ROW_FIELDS))
        self.assertNotIn("id", row)
        self.assertNotIn("created_at", row)
        self.assertEqual(row["model"], "sim")
        self.assertEqual(row["game_id"], ATL_NO)
        self.assertEqual(row["home_team"], "NO")
        self.assertEqual(row["away_team"], "ATL")
        self.assertEqual(row["season"], 2026)
        self.assertEqual(row["week"], 4)
        self.assertEqual(row["n_draws"], 4)
        self.assertAlmostEqual(row["home_mean"], 17.75)
        self.assertAlmostEqual(row["away_mean"], 17.25)
        # sorted home 13, 17, 20, 21 — same percentile as the sim
        self.assertAlmostEqual(row["home_p10"], 14.2)
        self.assertAlmostEqual(row["home_p50"], 18.5)
        self.assertAlmostEqual(row["home_p90"], 20.7)
        self.assertAlmostEqual(row["away_p10"], 11.2)
        self.assertAlmostEqual(row["away_p50"], 17.5)
        self.assertAlmostEqual(row["away_p90"], 23.1)
        self.assertEqual(row["home_wins"], 2)
        self.assertEqual(row["away_wins"], 1)
        self.assertEqual(row["ties"], 1)
        self.assertAlmostEqual(row["home_win_prob"], 0.5)
        self.assertAlmostEqual(row["away_win_prob"], 0.25)
        self.assertAlmostEqual(row["consensus_home_line"], -3.0)
        self.assertAlmostEqual(row["consensus_total"], 37.0)
        # margins vs -3: cover, push, away, away
        self.assertAlmostEqual(row["home_cover_prob"], 0.25)
        self.assertAlmostEqual(row["away_cover_prob"], 0.5)
        self.assertAlmostEqual(row["push_cover_prob"], 0.25)
        # totals 30, 31, 42, 37 vs 37
        self.assertAlmostEqual(row["over_prob"], 0.25)
        self.assertAlmostEqual(row["under_prob"], 0.5)
        self.assertAlmostEqual(row["push_total_prob"], 0.25)

    def test_half_point_total_does_not_push(self) -> None:
        rows = build_game_projection_rows(
            _draws(),
            season=2026,
            week=4,
            season_type="REG",
            run_at="2026-10-05T23:00:00+00:00",
            model_version="abc1234",
            line_rows=[_consensus_row(total=37.5, spread=-3.5)],
        )
        row = rows[0]
        self.assertAlmostEqual(row["push_total_prob"], 0.0)
        self.assertAlmostEqual(row["push_cover_prob"], 0.0)
        self.assertAlmostEqual(row["consensus_home_line"], -3.5)

    def test_non_uuid_game_id_is_not_a_row(self) -> None:
        rows = build_game_projection_rows(
            _draws(),
            season=2026,
            week=4,
            season_type="REG",
            run_at="t",
            model_version="v",
            line_rows=[_consensus_row(game_id="2026_04_ATL_NO")],
        )
        self.assertEqual(rows, [])

    def test_upsert_uses_postgrest_not_the_player_function(self) -> None:
        built = build_game_projection_rows(
            _draws(),
            season=2026,
            week=4,
            season_type="REG",
            run_at="2026-10-05T23:00:00+00:00",
            model_version="abc1234",
            line_rows=[_consensus_row()],
        )
        seen = {}

        def fake(url, body, headers=None, timeout=60):
            seen["url"] = url
            seen["body"] = body
            seen["headers"] = dict(headers or {})
            return body, {}

        with patch("nfl.game_projections.http_json_post", fake):
            n = post_game_projection_rows(built, key="sekrit-role")
        self.assertEqual(n, 1)
        self.assertIn("/rest/v1/nfl_game_projections", seen["url"])
        self.assertNotIn("/functions/v1/projections", seen["url"])
        self.assertIn("on_conflict=" + ON_CONFLICT, seen["url"])
        self.assertNotIn("sekrit-role", seen["url"])
        self.assertEqual(seen["headers"]["apikey"], "sekrit-role")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer sekrit-role")
        self.assertIn("resolution=merge-duplicates", seen["headers"]["Prefer"])
        self.assertIsInstance(seen["body"], list)
        self.assertEqual(seen["body"][0]["model"], "sim")
        self.assertNotIn("rows", seen["body"][0])


class PublishWireTest(unittest.TestCase):
    def test_team_mode_writes_games_beside_player_rows(self) -> None:
        import tempfile
        from pathlib import Path

        from nfl.publish_projections import SimResult, main

        player = Player(
            pid="wr",
            name="Chris Olave",
            position="WR",
            salary=6000,
            team="NO",
            opponent="ATL",
            game="ATL@NO",
            fppg=None,
            injury="",
            roster_position="",
            objective=10.0,
            spread=-3.0,
            total=37.0,
        )
        entry = PublishEntry(player, "00-1", "p1", ATL_NO, None, 6000)

        class Stats:
            mean = 12.0
            p10 = 4.0
            p50 = 11.0
            p90 = 20.0

        result = SimResult(
            {"wr": Stats()},
            "data",
            _draws(),
            {"wr": (0.25, 0.25)},
        )

        def load(_args, _today):
            return 2026, 4, [entry]

        with tempfile.TemporaryDirectory() as tmp:
            with patch("nfl.publish_projections.load_slate", load), patch(
                "nfl.publish_projections.maybe_sim", return_value=result
            ), patch(
                "nfl.publish_projections.model_version", return_value="abc1234"
            ), patch(
                "nfl.publish_projections.fetch_game_lines",
                return_value=([_consensus_row()], {"cache_stale": False}),
            ), patch("nfl.publish_projections.post_projection_rows") as post:
                rc = main(
                    ["--dry-run", "--sim", "4", "--sim-mode", "team", "--out-dir", tmp]
                )
                written = list(Path(tmp).glob("*-game-projections.json"))
            self.assertEqual(rc, 0)
            post.assert_not_called()
            self.assertEqual(len(written), 1)
            body = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertEqual(body["rows"][0]["game_id"], ATL_NO)
            self.assertEqual(body["rows"][0]["model"], "sim")

        posted = {}

        def capture_players(rows, key):
            posted["players"] = rows
            posted["player_key"] = key
            return 1, 0

        def capture_games(rows, key):
            posted["games"] = rows
            posted["game_key"] = key
            return len(rows)

        with patch("nfl.publish_projections.projections_key", return_value="writer"), patch(
            "nfl.publish_projections.load_slate", load
        ), patch(
            "nfl.publish_projections.maybe_sim", return_value=result
        ), patch(
            "nfl.publish_projections.model_version", return_value="abc1234"
        ), patch(
            "nfl.publish_projections.fetch_game_lines",
            return_value=([_consensus_row()], {"cache_stale": False}),
        ), patch(
            "nfl.publish_projections.post_projection_rows", capture_players
        ), patch(
            "nfl.game_projections.service_role_key", return_value="role-key"
        ), patch(
            "nfl.game_projections.post_game_projection_rows", capture_games
        ):
            rc = main(["--sim", "4", "--sim-mode", "team"])
        self.assertEqual(rc, 0)
        self.assertEqual(posted["player_key"], "writer")
        self.assertTrue(all(row.get("model") in {"board", "sim"} for row in posted["players"]))
        self.assertTrue(all("home_mean" not in row for row in posted["players"]))
        sim_row = next(row for row in posted["players"] if row["model"] == "sim")
        self.assertIn("anytime_td_prob", sim_row["inputs"])
        self.assertIn("td_mean", sim_row["inputs"])
        self.assertEqual(posted["game_key"], "role-key")
        self.assertEqual(posted["games"][0]["game_id"], ATL_NO)
        self.assertNotIn("anytime_td_prob", posted["games"][0])


class AnytimeTdTest(unittest.TestCase):
    def test_summary_is_mean_of_poisson_at_least_one(self) -> None:
        counts = (0.0, 0.0, math.log(2.0))  # 1 - exp(-ln2) = 0.5
        prob, mean = anytime_td_summary(counts)
        self.assertAlmostEqual(mean, math.log(2.0) / 3.0)
        self.assertAlmostEqual(prob, 0.5 / 3.0)

    def test_simulate_games_keeps_one_td_count_per_draw(self) -> None:
        qb = Player(
            pid="qb",
            name="Patrick Mahomes",
            position="QB",
            salary=8000,
            team="KC",
            opponent="NO",
            game="KC@NO",
            fppg=None,
            injury="",
            roster_position="",
            implied_total=24.0,
            spread=-3.0,
            total=47.0,
        )
        wr = Player(
            pid="wr",
            name="Chris Olave",
            position="WR",
            salary=6000,
            team="NO",
            opponent="KC",
            game="KC@NO",
            fppg=None,
            injury="",
            roster_position="",
            implied_total=22.0,
            spread=3.0,
            total=47.0,
        )
        played = simulate_games([qb, wr], n=40, seed=1)
        self.assertEqual(len(played.game_draws), 40)
        self.assertEqual(len(played.td_draws["qb"]), 40)
        self.assertTrue(all(value >= 0 for value in played.td_draws["qb"]))
        qb_mean = sum(played.td_draws["qb"]) / 40.0
        self.assertGreater(qb_mean, 0.0)
        # Rush TDs only. A passer total near 1.5 would mean passing TDs leaked in.
        self.assertLess(qb_mean, 0.5)
        # No catcher history: the WR stays on the role-share path, which
        # has no TD count. Passing TDs are not copied onto the QB series.
        self.assertNotIn("wr", played.td_draws)

    def test_scoring_tds_ignore_passing_touchdowns(self) -> None:
        eff = PlaceholderEfficiency()
        qb = Player(
            pid="qb",
            name="Patrick Mahomes",
            position="QB",
            salary=8000,
            team="KC",
            opponent="NO",
            game="KC@NO",
            fppg=None,
            injury="",
            roster_position="",
        )
        caught = ReceivingLine(targets=8, receptions=5, rec_yd=60, rec_td=1.5)
        qb_count = OpportunityCount(
            pass_attempts=30,
            rushes=4,
            team_receiving=(caught,),
        )
        self.assertAlmostEqual(eff.scoring_tds(qb, qb_count), 4 * 0.035)
        wr = Player(
            pid="wr",
            name="Chris Olave",
            position="WR",
            salary=6000,
            team="NO",
            opponent="KC",
            game="KC@NO",
            fppg=None,
            injury="",
            roster_position="",
        )
        wr_count = OpportunityCount(
            rushes=2,
            receiving=ReceivingLine(targets=8, receptions=5, rec_yd=60, rec_td=0.4),
        )
        self.assertAlmostEqual(eff.scoring_tds(wr, wr_count), 2 * 0.025 + 0.4)

    def test_sim_rows_store_anytime_fields_on_inputs(self) -> None:
        player = Player(
            pid="wr",
            name="Chris Olave",
            position="WR",
            salary=6000,
            team="NO",
            opponent="ATL",
            game="ATL@NO",
            fppg=None,
            injury="",
            roster_position="",
            objective=10.0,
        )
        dst = Player(
            pid="dst:NO",
            name="New Orleans Saints",
            position="D",
            salary=3000,
            team="NO",
            opponent="ATL",
            game="ATL@NO",
            fppg=None,
            injury="",
            roster_position="",
            objective=7.0,
        )
        entries = [
            PublishEntry(player, "00-1", "p1", ATL_NO, None, 6000),
            PublishEntry(dst, None, None, ATL_NO, None, 3000),
        ]

        class Stats:
            mean = 12.0
            p10 = 4.0
            p50 = 11.0
            p90 = 20.0

        rows = projection_rows(
            entries,
            season=2026,
            week=4,
            season_type="REG",
            run_at="2026-10-05T23:00:00+00:00",
            model_version="abc1234",
            sim_by_pid={"wr": Stats(), "dst:NO": Stats()},
            td_by_pid={"wr": (0.0, math.log(2.0)), "dst:NO": (1.0, 1.0)},
        )
        sim = {row["player_name"]: row for row in rows if row["model"] == "sim"}
        board = {row["player_name"]: row for row in rows if row["model"] == "board"}
        self.assertNotIn("anytime_td_prob", board["Chris Olave"]["inputs"])
        self.assertNotIn("anytime_td_prob", sim["New Orleans Saints"]["inputs"])
        self.assertAlmostEqual(sim["Chris Olave"]["inputs"]["td_mean"], round(math.log(2.0) / 2.0, 4))
        self.assertAlmostEqual(
            sim["Chris Olave"]["inputs"]["anytime_td_prob"],
            round(0.5 / 2.0, 4),
        )
        flat = json.dumps(sim["Chris Olave"]["inputs"])
        self.assertIn("anytime_td_prob", flat)
        self.assertIn("td_mean", flat)


if __name__ == "__main__":
    unittest.main()
