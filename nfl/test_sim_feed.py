"""Gangstash → SimInputs. Fetch functions are patched. No network."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from nfl.gangstash import GangstashDataKeyMissing
from nfl.players import Player
from nfl.projections import week1_score
from nfl.sim import pearson, simulate_games
from nfl.sim_feed import UNAVAILABLE_NOTE, resolve_sim_inputs
from nfl.sim_inputs import load_sim_inputs

FIXTURE = Path(__file__).resolve().parent / "testdata" / "sim_layers.json"


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("pid", "p"),
        name=kw.get("name", "P"),
        position=kw.get("position", "WR"),
        salary=5000,
        team="DET",
        opponent="NO",
        game="NO@DET",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=28.0,
        implied_opp=22.0,
        depth_rank=1,
        total=50.0,
        spread=-6.0,
    )
    fields.update(kw)
    fields["objective"] = week1_score(
        fields["implied_total"],
        depth_rank=fields.get("depth_rank"),
        position=fields["position"],
    )
    allowed = Player.__dataclass_fields__
    return Player(**{k: v for k, v in fields.items() if k in allowed})


class SimFeedTest(unittest.TestCase):
    def _payload(self) -> dict:
        return json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_file_override_does_not_fetch(self) -> None:
        with patch("nfl.sim_feed.fetch_team_stats", side_effect=AssertionError("network")), patch(
            "nfl.sim_feed.fetch_targets", side_effect=AssertionError("network")
        ), patch("nfl.sim_feed.fetch_snaps", side_effect=AssertionError("network")), patch(
            "nfl.sim_feed.fetch_team_stats_weekly", side_effect=AssertionError("network")
        ), patch("nfl.gangstash.http_json", side_effect=AssertionError("network")):
            inputs, note = resolve_sim_inputs(
                path=str(FIXTURE),
                season=2025,
                weeks=[1, 2, 3, 4],
            )
        self.assertIn("file", note)
        self.assertEqual(len(inputs.targets), len(load_sim_inputs(FIXTURE).targets))

    def test_feed_uses_sums_weekly_rates_and_rb_snaps(self) -> None:
        payload = self._payload()
        weekly = [
            {
                "team_fd": "DET",
                "side": "offense",
                "week": 1,
                "neutral_pass_rate": 0.70,
                "pass_rate": 0.72,
                "proe": 0.04,
            },
            {
                "team_fd": "DET",
                "side": "offense",
                "week": 2,
                "neutral_pass_rate": 0.50,
                "pass_rate": 0.52,
                "proe": 0.02,
            },
        ]
        snaps = [
            {
                "season": 2025,
                "week": 1,
                "position": "RB",
                "player_name": "Jahmyr Gibbs",
                "team_fd": "DET",
                "offense_snaps": 45,
                "offense_pct": 0.72,
                "gsis_id": "00-0038542",
            },
            {
                "season": 2025,
                "week": 1,
                "position": "WR",
                "player_name": "Amon-Ra St. Brown",
                "team_fd": "DET",
                "offense_snaps": 50,
                "offense_pct": 0.90,
            },
        ]
        with patch(
            "nfl.sim_feed.fetch_team_stats",
            return_value=(payload["team_stats"], {"live": False, "cache_stale": False}),
        ) as stats, patch(
            "nfl.sim_feed.fetch_team_stats_weekly",
            return_value=(weekly, {"live": False, "cache_stale": True}),
        ) as weekly_fetch, patch(
            "nfl.sim_feed.fetch_targets",
            return_value=(payload["targets"], {"live": False, "cache_stale": False}),
        ), patch(
            "nfl.sim_feed.fetch_snaps",
            return_value=(snaps, {"live": False, "cache_stale": False}),
        ), patch(
            "nfl.sim_feed.fetch_player_stats_weekly",
            return_value=([], {"cache_stale": False}),
        ), patch("nfl.gangstash.http_json", side_effect=AssertionError("network")):
            inputs, note = resolve_sim_inputs(
                path=None,
                season=2025,
                weeks=[1, 2, 3, 4],
                refresh_targets=True,
            )
        stats.assert_called_once()
        self.assertEqual(stats.call_args.kwargs["season"], 2025)
        self.assertFalse(stats.call_args.kwargs["refresh"])
        weekly_fetch.assert_called_once()
        self.assertEqual(weekly_fetch.call_args.kwargs["weeks"], [1, 2, 3, 4])
        self.assertIn("team_stats", note)
        self.assertIn("stale cache", note)
        det = next(
            row
            for row in inputs.team_stats
            if row.team_fd == "DET" and row.is_offense
        )
        # (560 - 48^2/400) / 399
        self.assertAlmostEqual(det.epa_variance(), (560 - (48.0 ** 2) / 400) / 399, places=6)
        self.assertAlmostEqual(det.neutral_pass_rate, 0.60, places=6)
        self.assertAlmostEqual(det.proe, 0.03, places=6)
        self.assertTrue(any(row.team_fd == "DET" and row.is_defense for row in inputs.team_stats))
        names = {row.player_name for row in inputs.targets}
        self.assertIn("Amon-Ra St. Brown", names)
        self.assertIn("Jameson Williams", names)
        self.assertEqual(len(inputs.snaps), 1)
        self.assertEqual(inputs.snaps[0].player_name, "Jahmyr Gibbs")
        self.assertAlmostEqual(inputs.snaps[0].offense_pct, 0.72, places=6)

        qb = _pl(pid="qb", name="Jared Goff", position="QB", salary=8000)
        wr1 = _pl(pid="wr1", name="Amon-Ra St. Brown", position="WR", depth_rank=1)
        wr2 = _pl(pid="wr2", name="Jameson Williams", position="WR", depth_rank=2)
        gs = simulate_games([qb, wr1, wr2], n=2500, seed=1, inputs=inputs)
        self.assertGreater(pearson(list(gs.draws["qb"]), list(gs.draws["wr1"])), 0.15)
        self.assertGreater(pearson(list(gs.draws["qb"]), list(gs.draws["wr2"])), 0.15)
        self.assertLess(pearson(list(gs.draws["wr1"]), list(gs.draws["wr2"])), -0.05)
        self.assertEqual(qb.objective, week1_score(28.0, 1, "QB"))

    def test_missing_key_falls_back(self) -> None:
        missing = GangstashDataKeyMissing("GANGSTASH_API_KEY is not set")
        with patch("nfl.sim_feed.fetch_team_stats", side_effect=missing), patch(
            "nfl.sim_feed.fetch_team_stats_weekly", side_effect=missing
        ), patch("nfl.sim_feed.fetch_targets", side_effect=missing), patch(
            "nfl.sim_feed.fetch_snaps", side_effect=missing
        ), patch(
            "nfl.sim_feed.fetch_player_stats_weekly", side_effect=missing
        ), patch("nfl.gangstash.http_json", side_effect=AssertionError("network")):
            inputs, note = resolve_sim_inputs(path=None, season=2026, weeks=[1, 2])
        self.assertIsNone(inputs)
        self.assertEqual(note, UNAVAILABLE_NOTE)
        self.assertIn("role shares deterministic", note)

    def test_no_week_window_skips_weekly(self) -> None:
        payload = self._payload()
        with patch(
            "nfl.sim_feed.fetch_team_stats",
            return_value=(payload["team_stats"], {"cache_stale": False}),
        ), patch("nfl.sim_feed.fetch_team_stats_weekly") as weekly, patch(
            "nfl.sim_feed.fetch_targets",
            return_value=(payload["targets"], {"cache_stale": False}),
        ), patch(
            "nfl.sim_feed.fetch_snaps",
            return_value=([], {"cache_stale": False}),
        ), patch(
            "nfl.sim_feed.fetch_player_stats_weekly",
            return_value=([], {"cache_stale": False}),
        ):
            inputs, _note = resolve_sim_inputs(path=None, season=2025, weeks=None)
        weekly.assert_not_called()
        det = next(row for row in inputs.team_stats if row.team_fd == "DET" and row.is_offense)
        self.assertAlmostEqual(det.neutral_pass_rate, 0.58, places=6)

    def test_jax_team_fd_maps_to_jac(self) -> None:
        row = {
            "team_fd": "JAX",
            "side": "offense",
            "neutral_pass_rate": 0.57,
            "n": 100,
            "epa_sum": 10,
            "epa_sq_sum": 200,
        }
        with patch(
            "nfl.sim_feed.fetch_team_stats",
            return_value=([row], {"cache_stale": False}),
        ), patch(
            "nfl.sim_feed.fetch_targets",
            return_value=([], {"cache_stale": False}),
        ), patch(
            "nfl.sim_feed.fetch_snaps",
            return_value=([], {"cache_stale": False}),
        ), patch(
            "nfl.sim_feed.fetch_player_stats_weekly",
            return_value=([], {"cache_stale": False}),
        ):
            inputs, _note = resolve_sim_inputs(path=None, season=2026, weeks=None)
        self.assertEqual(inputs.team_stats[0].team_fd, "JAC")

    def test_weekly_scope_pools_prior_weeks_and_skips_the_season_board(self) -> None:
        season = [
            {
                "team_fd": "DET",
                "side": "offense",
                "n": 400,
                "epa_sum": 800,
                "epa_sq_sum": 5000,
                "epa_var": 99,
                "pass_rate": 0.9,
            }
        ]
        weekly = [
            {
                "team_fd": "DET",
                "side": "offense",
                "week": 1,
                "n": 60,
                "epa_sum": 6,
                "epa_sq_sum": 30,
                "pass_rate": 0.55,
                "pass_n": 35,
                "rush_n": 25,
            },
            {
                "team_fd": "DET",
                "side": "offense",
                "week": 2,
                "n": 70,
                "epa_sum": 70,
                "epa_sq_sum": 200,
                "pass_rate": 0.80,
                "pass_n": 40,
                "rush_n": 30,
            },
        ]
        with patch(
            "nfl.sim_feed.fetch_team_stats",
            return_value=(season, {"cache_stale": False}),
        ) as stats, patch(
            "nfl.sim_feed.fetch_team_stats_weekly",
            return_value=(weekly, {"cache_stale": False}),
        ), patch(
            "nfl.sim_feed.fetch_targets",
            return_value=([], {"cache_stale": False}),
        ), patch(
            "nfl.sim_feed.fetch_snaps",
            return_value=([], {"cache_stale": False}),
        ), patch(
            "nfl.sim_feed.fetch_player_stats_weekly",
            return_value=([], {"cache_stale": False}),
        ), patch("nfl.gangstash.http_json", side_effect=AssertionError("network")):
            inputs, note = resolve_sim_inputs(
                path=None,
                season=2026,
                weeks=[1],
                team_stats_scope="weekly",
            )
        stats.assert_not_called()
        self.assertIn("weekly", note)
        det = next(row for row in inputs.team_stats if row.team_fd == "DET" and row.is_offense)
        self.assertEqual(det.n, 60)
        self.assertAlmostEqual(det.pass_rate or 0, 0.55, places=6)
        self.assertAlmostEqual(det.epa_variance() or 0, (30 - (6.0 ** 2) / 60) / 59, places=6)
        self.assertEqual(len(inputs.team_weeks), 1)
        self.assertEqual(inputs.team_weeks[0].week, 1)

    def test_week_1_weekly_scope_has_no_team_board(self) -> None:
        with patch("nfl.sim_feed.fetch_team_stats") as stats, patch(
            "nfl.sim_feed.fetch_team_stats_weekly"
        ) as weekly, patch("nfl.gangstash.http_json", side_effect=AssertionError("network")):
            inputs, note = resolve_sim_inputs(
                path=None,
                season=2026,
                weeks=[],
                team_stats_scope="weekly",
            )
        stats.assert_not_called()
        weekly.assert_not_called()
        self.assertIsNone(inputs)
        self.assertEqual(note, UNAVAILABLE_NOTE)


if __name__ == "__main__":
    unittest.main()
