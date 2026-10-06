"""Game-projection aggregate, anytime TD fields, and the props suffix join."""

from __future__ import annotations

import json
import math
import random
import unittest
from unittest.mock import patch

from nfl.game_projections import (
    GAME_ROW_FIELDS,
    build_game_projection_rows,
    wire_team,
)
from nfl.players import Player
from nfl.publish_projections import (
    PROJECTIONS_URL,
    PublishEntry,
    PublishError,
    post_game_projection_rows,
    projection_rows,
)
from nfl.sim import TdTally, poisson_sample, simulate_games
from nfl.sim_efficiency import (
    PASS_TD_RATE,
    OpportunityCount,
    PlaceholderEfficiency,
    ReceivingLine,
)


ATL_NO = "89c7243e-cb87-4fa4-b2c7-068be7c16a68"


def _counted(*values, seed=0):
    tally = TdTally()
    rng = random.Random(seed)
    for value in values:
        tally.add(value, rng=rng)
    return tally


class _SeqRng:
    """Fixed uniforms so a Poisson sample is an exact integer."""

    def __init__(self, values):
        self._values = iter(values)

    def random(self):
        return next(self._values)


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

    def test_wire_team_codes_and_omits_missing_lines(self) -> None:
        player = Player(
            pid="qb",
            name="Trevor Lawrence",
            position="QB",
            salary=7000,
            team="JAC",
            opponent="LAR",
            game="LAR@JAC",
            fppg=None,
            injury="",
            roster_position="",
        )
        entry = PublishEntry(
            player, "00-1", "p1", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", None, 7000
        )
        draws = tuple(("g", "LAR", "JAC", 17.2, 24.4) for _ in range(4))
        rows = build_game_projection_rows(
            draws,
            season=2026,
            week=4,
            season_type="POST",
            run_at="2026-10-05T23:00:00+00:00",
            model_version="abc1234",
            entries=[entry],
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["season_type"], "POST")
        self.assertEqual(row["away_team"], "LA")
        self.assertEqual(row["home_team"], "JAX")
        self.assertEqual(
            row["home_wins"] + row["away_wins"] + row["ties"], row["n_draws"]
        )
        self.assertGreaterEqual(row["home_win_prob"], 0.0)
        self.assertLessEqual(row["home_win_prob"], 1.0)
        self.assertLessEqual(row["home_p10"], row["home_p50"])
        self.assertLessEqual(row["home_p50"], row["home_p90"])
        self.assertNotIn("consensus_home_line", row)
        self.assertNotIn("consensus_total", row)
        self.assertNotIn("home_cover_prob", row)
        self.assertNotIn("over_prob", row)

    def test_wsh_goes_out_as_was(self) -> None:
        self.assertEqual(wire_team("WSH"), "WAS")
        self.assertEqual(wire_team("WAS"), "WAS")
        self.assertEqual(wire_team("JAC"), "JAX")
        self.assertEqual(wire_team("LAR"), "LA")

    def test_game_rows_post_as_a_second_writer_request(self) -> None:
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

        def fake(url, body, headers=None, timeout=60, error_chars=300):
            seen["url"] = url
            seen["body"] = body
            seen["headers"] = dict(headers or {})
            return {
                "game_inserted": 1,
                "game_updated": 0,
                "game_upserted": 1,
            }, {}

        with patch("nfl.publish_projections.http_json_post", fake):
            inserted, updated, upserted = post_game_projection_rows(
                built, key="sekrit-writer"
            )
        self.assertEqual((inserted, updated, upserted), (1, 0, 1))
        self.assertEqual(seen["url"], PROJECTIONS_URL)
        self.assertNotIn("/rest/v1", seen["url"])
        self.assertNotIn("sekrit-writer", seen["url"])
        self.assertEqual(seen["headers"]["x-api-key"], "sekrit-writer")
        self.assertNotIn("apikey", seen["headers"])
        self.assertNotIn("Authorization", seen["headers"])
        self.assertEqual(list(seen["body"]), ["game_projections"])
        self.assertEqual(seen["body"]["game_projections"][0]["model"], "sim")
        self.assertEqual(seen["body"]["game_projections"][0]["home_team"], "NO")
        self.assertEqual(seen["body"]["game_projections"][0]["away_team"], "ATL")

    def test_game_rejection_keeps_the_body_in_the_error(self) -> None:
        from nfl.http import HttpError

        def fake(url, body, headers=None, timeout=60, error_chars=300):
            raise HttpError(
                'HTTP 400: {"errors":[{"source":"game_projections","row":0}]}'
            )

        with patch("nfl.publish_projections.http_json_post", fake):
            with self.assertRaises(PublishError) as caught:
                post_game_projection_rows([{"game_id": "x"}], key="sekrit-writer")
        text = str(caught.exception)
        self.assertIn("game_projections", text)
        self.assertNotIn("sekrit-writer", text)


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

        wr_tds = _counted(0.25, 0.25)
        result = SimResult(
            {"wr": Stats()},
            "data",
            _draws(),
            {"wr": wr_tds},
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
            self.assertEqual(body["game_projections"][0]["game_id"], ATL_NO)
            self.assertEqual(body["game_projections"][0]["model"], "sim")

        posted = {}

        def capture_players(rows, key):
            posted["players"] = rows
            posted["player_key"] = key
            return 1, 0

        def capture_games(rows, key):
            posted["games"] = rows
            posted["game_key"] = key
            return 1, 0, 1

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
            "nfl.publish_projections.post_game_projection_rows", capture_games
        ):
            rc = main(["--sim", "4", "--sim-mode", "team"])
        self.assertEqual(rc, 0)
        self.assertEqual(posted["player_key"], "writer")
        self.assertTrue(all(row.get("model") in {"board", "sim"} for row in posted["players"]))
        self.assertTrue(all("home_mean" not in row for row in posted["players"]))
        sim_row = next(row for row in posted["players"] if row["model"] == "sim")
        self.assertIn("anytime_td_prob", sim_row["inputs"])
        self.assertIn("td_mean", sim_row["inputs"])
        self.assertEqual(
            sim_row["inputs"]["td_0"]
            + sim_row["inputs"]["td_1"]
            + sim_row["inputs"]["td_2"]
            + sim_row["inputs"]["td_3plus"],
            sim_row["inputs"]["n_draws"],
        )
        self.assertAlmostEqual(
            sim_row["inputs"]["anytime_td_prob"],
            1.0 - sim_row["inputs"]["td_0"] / float(sim_row["inputs"]["n_draws"]),
        )
        self.assertNotIn("pass_td_mean", sim_row["inputs"])
        self.assertNotIn("total_td_0", sim_row["inputs"])
        self.assertEqual(posted["game_key"], "writer")
        self.assertEqual(posted["games"][0]["game_id"], ATL_NO)
        self.assertNotIn("anytime_td_prob", posted["games"][0])

    def test_game_rejection_does_not_unpost_players(self) -> None:
        import io
        from contextlib import redirect_stderr

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
        )
        entry = PublishEntry(player, "00-1", "p1", ATL_NO, None, 6000)

        class Stats:
            mean = 12.0
            p10 = 4.0
            p50 = 11.0
            p90 = 20.0

        result = SimResult({"wr": Stats()}, "data", _draws(), {"wr": _counted(0.2)})
        posted = {}

        def capture_players(rows, key):
            posted["n"] = len(rows)
            return 1, 0

        def boom(rows, key):
            raise PublishError(
                'HTTP 400: {"errors":[{"source":"game_projections","row":0}]}'
            )

        def load(_args, _today):
            return 2026, 4, [entry]

        err = io.StringIO()
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
            "nfl.publish_projections.post_game_projection_rows", boom
        ), redirect_stderr(err):
            rc = main(["--sim", "4", "--sim-mode", "team"])
        self.assertEqual(rc, 1)
        self.assertEqual(posted["n"], 2)
        self.assertIn("player rows already posted", err.getvalue())
        self.assertIn("game_projections", err.getvalue())


class AnytimeTdTest(unittest.TestCase):
    def test_poisson_sample_is_knuth_on_the_given_rng(self) -> None:
        # exp(-ln2) = 0.5. One uniform below that is 0; two steps that
        # cross it are 1. A zero rate does not read the RNG.
        self.assertEqual(poisson_sample(_SeqRng([0.4]), math.log(2.0)), 0)
        self.assertEqual(poisson_sample(_SeqRng([0.9, 0.4]), math.log(2.0)), 1)
        self.assertEqual(poisson_sample(_SeqRng([]), 0.0), 0)

    def test_bins_sum_to_n_and_anytime_matches_td_0(self) -> None:
        # ln2, uniform 0.4 -> 0; ln2, uniforms 0.9 then 0.4 -> 1; 0 -> 0.
        rate = math.log(2.0)
        tally = TdTally()
        tally.add(rate, rng=_SeqRng([0.4]))
        tally.add(rate, rng=_SeqRng([0.9, 0.4]))
        tally.add(0.0, rng=_SeqRng([]))
        fields = tally.inputs()
        self.assertEqual(
            fields["td_0"] + fields["td_1"] + fields["td_2"] + fields["td_3plus"],
            fields["n_draws"],
        )
        self.assertEqual(fields["n_draws"], 3)
        self.assertEqual(fields["td_0"], 2)
        self.assertEqual(fields["td_1"], 1)
        self.assertEqual(fields["td_2"], 0)
        self.assertEqual(fields["td_3plus"], 0)
        self.assertAlmostEqual(
            fields["anytime_td_prob"],
            1.0 - fields["td_0"] / float(fields["n_draws"]),
        )
        self.assertAlmostEqual(fields["td_mean"], round((2.0 * rate) / 3.0, 4))
        self.assertNotIn("pass_td_mean", fields)
        self.assertNotIn("total_td_0", fields)

    def test_constant_rate_anytime_near_one_minus_exp_mean(self) -> None:
        # PR #32 stored mean(1 - exp(-λ)). For a constant λ that equals
        # 1 - exp(-td_mean). Poisson counts should land on the same value
        # up to Monte Carlo noise. td_mean stays the expectation.
        rate = 0.599
        n = 8000
        tally = TdTally()
        rng = random.Random(11)
        for _ in range(n):
            tally.add(rate, rng=rng)
        fields = tally.inputs()
        self.assertEqual(
            fields["td_0"] + fields["td_1"] + fields["td_2"] + fields["td_3plus"],
            n,
        )
        self.assertEqual(fields["td_mean"], round(rate, 4))
        self.assertAlmostEqual(
            fields["anytime_td_prob"],
            1.0 - fields["td_0"] / float(n),
        )
        self.assertAlmostEqual(
            fields["anytime_td_prob"],
            1.0 - math.exp(-rate),
            delta=0.02,
        )

    def test_qb_total_is_rush_rec_sample_plus_pass_sample(self) -> None:
        # Each rate is ln2. Uniforms pick rush+rec = 0 and pass = 1,
        # so the total bin is 1. Means stay on the expectations.
        rate = math.log(2.0)
        tally = TdTally()
        tally.add(rate, rate, _SeqRng([0.4, 0.9, 0.4]))
        tally.add(0.0, 0.4, _SeqRng([0.1]))
        fields = tally.inputs()
        self.assertEqual(
            fields["td_0"] + fields["td_1"] + fields["td_2"] + fields["td_3plus"],
            fields["n_draws"],
        )
        self.assertEqual(
            fields["total_td_0"]
            + fields["total_td_1"]
            + fields["total_td_2"]
            + fields["total_td_3plus"],
            fields["n_draws"],
        )
        self.assertEqual(fields["td_0"], 2)
        self.assertEqual(fields["total_td_0"], 1)
        self.assertEqual(fields["total_td_1"], 1)
        self.assertAlmostEqual(fields["pass_td_mean"], round((rate + 0.4) / 2.0, 4))
        self.assertAlmostEqual(fields["total_td_mean"], round((2.0 * rate + 0.4) / 2.0, 4))
        self.assertAlmostEqual(fields["td_mean"], round(rate / 2.0, 4))

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
        # Side-RNG TD samples must not move the fantasy-point stream.
        # These floats are the seed=1, n=40 draws from before Poisson TDs.
        qb_stats = played.by_pid["qb"]
        wr_stats = played.by_pid["wr"]
        self.assertAlmostEqual(qb_stats.mean, 21.552073089173632, places=9)
        self.assertEqual(qb_stats.p10, 16.16861895393065)
        self.assertEqual(qb_stats.p50, 21.034573184830833)
        self.assertEqual(qb_stats.p90, 28.94698137094938)
        self.assertAlmostEqual(wr_stats.mean, 0.19906323307527735, places=9)
        self.assertEqual(wr_stats.p10, 0.14357708419984358)
        self.assertEqual(wr_stats.p50, 0.1990485470343669)
        self.assertEqual(wr_stats.p90, 0.2537641610938787)
        self.assertEqual(played.draws["qb"][0], 32.95480771291449)
        fields = played.td_tallies["qb"].inputs()
        self.assertEqual(fields["n_draws"], 40)
        self.assertEqual(
            fields["td_0"] + fields["td_1"] + fields["td_2"] + fields["td_3plus"],
            40,
        )
        self.assertEqual(
            fields["total_td_0"]
            + fields["total_td_1"]
            + fields["total_td_2"]
            + fields["total_td_3plus"],
            40,
        )
        self.assertAlmostEqual(
            fields["anytime_td_prob"],
            1.0 - fields["td_0"] / 40.0,
        )
        self.assertGreater(fields["td_mean"], 0.0)
        # Rush+rec only. A passer total near 1.5 would mean passing TDs leaked in.
        self.assertLess(fields["td_mean"], 0.5)
        self.assertGreater(fields["pass_td_mean"], 0.0)
        self.assertGreater(fields["total_td_mean"], fields["td_mean"])
        # No catcher history: the WR stays on the role-share path, which
        # has no TD count.
        self.assertNotIn("wr", played.td_tallies)

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
        self.assertAlmostEqual(eff.scoring_pass_tds(qb, qb_count), 1.5)
        bare = OpportunityCount(pass_attempts=30, rushes=4)
        self.assertAlmostEqual(eff.scoring_pass_tds(qb, bare), 30 * PASS_TD_RATE)
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
            td_by_pid={
                "wr": _counted(0.0, math.log(2.0)),
                "dst:NO": _counted(1.0, 1.0),
            },
        )
        sim = {row["player_name"]: row for row in rows if row["model"] == "sim"}
        board = {row["player_name"]: row for row in rows if row["model"] == "board"}
        self.assertNotIn("anytime_td_prob", board["Chris Olave"]["inputs"])
        self.assertNotIn("anytime_td_prob", sim["New Orleans Saints"]["inputs"])
        olave = sim["Chris Olave"]["inputs"]
        self.assertAlmostEqual(olave["td_mean"], round(math.log(2.0) / 2.0, 4))
        self.assertEqual(olave["td_0"] + olave["td_1"] + olave["td_2"] + olave["td_3plus"], olave["n_draws"])
        self.assertAlmostEqual(olave["anytime_td_prob"], 1.0 - olave["td_0"] / float(olave["n_draws"]))
        self.assertNotIn("total_td_mean", olave)
        flat = json.dumps(olave)
        self.assertIn("anytime_td_prob", flat)
        self.assertIn("td_mean", flat)
        self.assertIn("n_draws", flat)


if __name__ == "__main__":
    unittest.main()
