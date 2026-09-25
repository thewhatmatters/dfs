"""Layer 4 efficiency: shrinkage, opponent clamp, week cutoff. No network."""

from __future__ import annotations

import json
import random
import unittest
from pathlib import Path

from nfl.players import Player
from nfl.projections import week1_score
from nfl.sim import (
    LEAGUE_PLAYS,
    LEAGUE_NEUTRAL_SECONDS_PER_PLAY,
    LEAGUE_SECONDS_PER_PLAY,
    PACE_CLAMP,
    offense_plays_mu,
    simulate_games,
)
from nfl.sim_efficiency import (
    COMBINED_CLAMP,
    OPP_CLAMP,
    PRIOR_CARRIES,
    PRIOR_TARGETS,
    YARD_TILT_MAX,
    YARDS_PER_RUSH,
    DataEfficiency,
    PlaceholderEfficiency,
    build_efficiency,
    position_rates,
    shrink,
)
from nfl.sim_inputs import (
    CarryWeek,
    PlayerWeek,
    SimInputs,
    SnapWeek,
    TargetWeek,
    TeamStat,
    player_week_from_row,
    team_stat_from_row,
)


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("pid", "p"),
        name=kw.get("name", "Player"),
        position=kw.get("position", "WR"),
        salary=6000,
        team=kw.get("team", "DET"),
        opponent=kw.get("opponent", "NO"),
        game="NO@DET",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=24.0,
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


def _week(week: int, **kw) -> PlayerWeek:
    return PlayerWeek(
        season=2026,
        week=week,
        position=kw.get("position", "WR"),
        player_name=kw.get("player_name", "Amon-Ra St. Brown"),
        team_fd=kw.get("team_fd", "DET"),
        targets=kw.get("targets", 0.0),
        receptions=kw.get("receptions", 0.0),
        receiving_yards=kw.get("receiving_yards", 0.0),
        receiving_tds=kw.get("receiving_tds", 0.0),
        carries=kw.get("carries", 0.0),
        rushing_yards=kw.get("rushing_yards", 0.0),
        rushing_tds=kw.get("rushing_tds", 0.0),
        pass_attempts=kw.get("pass_attempts", 0.0),
        passing_yards=kw.get("passing_yards", 0.0),
        passing_tds=kw.get("passing_tds", 0.0),
        receiving_air_yards=kw.get("receiving_air_yards"),
        target_share=kw.get("target_share"),
        air_yards_share=kw.get("air_yards_share"),
        red_zone_targets=kw.get("red_zone_targets"),
        red_zone_carries=kw.get("red_zone_carries"),
        goal_line_carries=kw.get("goal_line_carries"),
        gsis_id=kw.get("gsis_id"),
        player_id=kw.get("player_id"),
    )


class ShrinkageTest(unittest.TestCase):
    def test_zero_sample_is_the_prior(self) -> None:
        self.assertAlmostEqual(shrink(1.0, 0, 0.64, PRIOR_TARGETS), 0.64)

    def test_equal_weight_splits_the_difference(self) -> None:
        self.assertAlmostEqual(shrink(1.0, 25, 0.64, 25), (25 * 1.0 + 25 * 0.64) / 50)

    def test_large_sample_reaches_the_observation(self) -> None:
        self.assertAlmostEqual(shrink(1.0, 1_000_000, 0.64, 25), 1.0, places=4)

    def test_no_history_is_the_position_prior(self) -> None:
        eff = DataEfficiency(SimInputs())
        player = _pl(name="Nobody", position="WR")
        rates = eff.rates_for(player, "WR")
        self.assertEqual(rates, position_rates("WR"))
        self.assertAlmostEqual(rates["yards_per_target"], 0.64 * 12.0)

    def test_history_moves_off_the_prior_and_stays_shrunk(self) -> None:
        hot = _week(1, targets=25, receptions=25, receiving_yards=400)
        eff = DataEfficiency(SimInputs(player_weeks=(hot,)))
        rates = eff.rates_for(_pl(name="Amon-Ra St. Brown"), "WR")
        prior = position_rates("WR")["catch_rate"]
        self.assertGreater(rates["catch_rate"], prior)
        self.assertLess(rates["catch_rate"], 1.0)

    def test_one_week_of_carries_stays_near_the_prior(self) -> None:
        hot = _week(
            1,
            position="RB",
            player_name="Derrick Henry",
            team_fd="BAL",
            carries=20,
            rushing_yards=130,
            rushing_tds=2,
        )
        eff = DataEfficiency(SimInputs(player_weeks=(hot,)), before_week=2)
        rates = eff.rates_for(
            _pl(name="Derrick Henry", team="BAL", position="RB"),
            "RB",
        )
        ypc = rates["yards_per_carry"]
        observed = 130 / 20
        prior = YARDS_PER_RUSH["RB"]
        self.assertGreater(ypc, prior)
        self.assertLess(abs(ypc - prior), abs(ypc - observed))
        self.assertLess(ypc, 5.2)
        # 20 carries against an 80-carry prior is one fifth of the blend.
        self.assertLess((20.0 / (20.0 + PRIOR_CARRIES)), 0.25)


class OpponentClampTest(unittest.TestCase):
    def _defense(self, **kw) -> TeamStat:
        fields = dict(
            team_fd="NO",
            side="defense",
            week=1,
            pass_n=10_000,
            rush_n=10_000,
            n=20_000,
        )
        fields.update(kw)
        return TeamStat(**fields)

    def test_extreme_pass_epa_clamps_at_fifteen_percent(self) -> None:
        eff = DataEfficiency(
            SimInputs(
                team_weeks=(self._defense(pass_epa_per_play=8.0, pass_success_rate=0.90),)
            ),
            before_week=2,
        )
        self.assertAlmostEqual(eff.pass_multiplier("NO"), 1.0 + OPP_CLAMP, places=4)

    def test_missing_defense_is_one(self) -> None:
        eff = DataEfficiency(SimInputs(), before_week=2)
        self.assertAlmostEqual(eff.pass_multiplier("NO"), 1.0)
        self.assertAlmostEqual(eff.rush_multiplier("NO"), 1.0)

    def test_small_sample_stays_inside_the_clamp(self) -> None:
        eff = DataEfficiency(
            SimInputs(team_weeks=(self._defense(pass_epa_per_play=8.0, pass_n=1, n=1),)),
            before_week=2,
        )
        mult = eff.pass_multiplier("NO")
        self.assertGreater(mult, 1.0)
        self.assertLess(mult, 1.0 + OPP_CLAMP)

    def test_yardage_tilt_stays_inside_eight_percent(self) -> None:
        off = TeamStat(
            team_fd="DET",
            side="offense",
            week=1,
            pass_epa_per_play=4.0,
            rush_epa_per_play=-3.0,
            pass_n=800,
            rush_n=800,
            n=1600,
        )
        eff = DataEfficiency(SimInputs(team_weeks=(off,)), before_week=2)
        scale = eff.pass_anchor_scale("DET", None)
        self.assertAlmostEqual(scale, 1.0 + YARD_TILT_MAX, places=4)

    def test_yards_allowed_hook_is_ignored_when_absent(self) -> None:
        base = self._defense(rush_epa_per_play=0.10)
        hooked = self._defense(rush_epa_per_play=0.10, yards_per_carry_allowed=6.5)
        plain = DataEfficiency(SimInputs(team_weeks=(base,)), before_week=2)
        with_hook = DataEfficiency(SimInputs(team_weeks=(hooked,)), before_week=2)
        self.assertGreater(with_hook.rush_multiplier("NO"), plain.rush_multiplier("NO"))
        self.assertLessEqual(with_hook.rush_multiplier("NO"), 1.0 + OPP_CLAMP)

    def test_optional_team_volume_does_not_tilt_the_anchor(self) -> None:
        row = team_stat_from_row(
            {
                "team_fd": "DET",
                "side": "offense",
                "week": 1,
                "plays_per_game": 70,
                "seconds_per_play": 27,
                "neutral_plays_per_game": 62,
                "neutral_seconds_per_play": 29,
            }
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertAlmostEqual(row.plays_per_game or 0, 70)
        self.assertIsNone(row.air_yards_per_attempt_allowed)
        eff = DataEfficiency(SimInputs(team_weeks=(row,)), before_week=2)
        self.assertAlmostEqual(eff.pass_anchor_scale("DET", None), 1.0)

    def test_red_zone_nudge_is_clamped(self) -> None:
        off = TeamStat(
            team_fd="DET",
            side="offense",
            week=1,
            red_zone_td_rate=0.99,
            n=5000,
        )
        de = TeamStat(
            team_fd="NO",
            side="defense",
            week=1,
            red_zone_td_rate=0.99,
            n=5000,
        )
        eff = DataEfficiency(SimInputs(team_weeks=(off, de)), before_week=2)
        self.assertAlmostEqual(eff.td_multiplier("DET", "NO"), 1.0 + 0.15, places=4)

    def test_rush_complement_and_opponent_do_not_stack(self) -> None:
        eff = DataEfficiency(
            SimInputs(
                team_weeks=(
                    self._defense(rush_epa_per_play=8.0, rush_success_rate=0.90),
                )
            ),
            before_week=2,
        )
        self.assertAlmostEqual(eff.rush_multiplier("NO"), 1.0 + OPP_CLAMP, places=4)
        # Pass tilt 0.92 → rush complement 1.08. 1.08 × 1.15 = 1.242.
        scale = eff.rush_budget_scale("DET", "NO", 0.92)
        self.assertAlmostEqual(scale, 1.0 + COMBINED_CLAMP, places=4)
        self.assertLess(scale, 1.08 * 1.15)


class RushBudgetTest(unittest.TestCase):
    def test_empty_history_budget_is_the_prior_yards(self) -> None:
        rb = _pl(pid="rb", name="Justice Hill", team="BAL", position="RB", opponent="CLE")
        eff = DataEfficiency(SimInputs())
        got = eff.allocate_rush(
            [(rb, 10.0)],
            team="BAL",
            opponent="CLE",
            pass_tilt=1.0,
        )
        self.assertEqual(got[rb.pid][0], 10.0 * YARDS_PER_RUSH["RB"])
        self.assertEqual(got[rb.pid][1], 10.0 * 0.025)

    def test_hot_ypc_redistributes_and_does_not_add(self) -> None:
        henry = _pl(
            pid="henry",
            name="Derrick Henry",
            team="BAL",
            position="RB",
            opponent="CLE",
        )
        other = _pl(
            pid="hill",
            name="Justice Hill",
            team="BAL",
            position="RB",
            opponent="CLE",
        )
        hot = _week(
            1,
            position="RB",
            player_name="Derrick Henry",
            team_fd="BAL",
            carries=22,
            rushing_yards=143,
            rushing_tds=2,
        )
        defense = TeamStat(
            team_fd="CLE",
            side="defense",
            week=1,
            rush_epa_per_play=8.0,
            rush_success_rate=0.90,
            rush_n=10_000,
            n=10_000,
        )
        eff = DataEfficiency(
            SimInputs(player_weeks=(hot,), team_weeks=(defense,)),
            before_week=2,
        )
        henry_rushes = 18.0
        other_rushes = 6.0
        got = eff.allocate_rush(
            [(henry, henry_rushes), (other, other_rushes)],
            team="BAL",
            opponent="CLE",
            pass_tilt=0.92,
        )
        prior = YARDS_PER_RUSH["RB"]
        scale = eff.rush_budget_scale("BAL", "CLE", 0.92)
        budget = (henry_rushes + other_rushes) * prior * scale
        self.assertAlmostEqual(got["henry"][0] + got["hill"][0], budget, places=4)
        self.assertAlmostEqual(scale, 1.0 + COMBINED_CLAMP, places=4)
        # The bellcow is the larger share, and still under the unanchored stack.
        self.assertGreater(got["henry"][0], henry_rushes * prior * scale * 0.5)
        self.assertLess(got["henry"][0], henry_rushes * (143 / 22) * 1.15)
        prior_share = henry_rushes * prior / ((henry_rushes + other_rushes) * prior)
        self.assertGreater(got["henry"][0], prior_share * budget)


class WeekScopeTest(unittest.TestCase):
    def test_target_week_does_not_leak_into_the_rate(self) -> None:
        early = _week(1, targets=4, receptions=1, receiving_yards=12)
        late = _week(2, targets=20, receptions=20, receiving_yards=400)
        held = DataEfficiency(
            SimInputs(player_weeks=(early, late)),
            before_week=2,
        )
        leaked = DataEfficiency(SimInputs(player_weeks=(early, late)))
        player = _pl(name="Amon-Ra St. Brown")
        held_rate = held.rates_for(player, "WR")["catch_rate"]
        leaked_rate = leaked.rates_for(player, "WR")["catch_rate"]
        self.assertLess(held_rate, 0.62)
        # One week is a small share of the prior, so the leaked week moves
        # the rate without jumping most of the way to the observation.
        self.assertGreater(leaked_rate, held_rate + 0.08)
        self.assertLess(leaked_rate, 0.85)

    def test_season_board_is_ignored_when_the_week_is_set(self) -> None:
        season = TeamStat(
            team_fd="DET",
            side="offense",
            pass_epa_per_play=4.0,
            rush_epa_per_play=-3.0,
            pass_n=800,
            rush_n=800,
            n=1600,
        )
        eff = DataEfficiency(SimInputs(team_stats=(season,)), before_week=2)
        self.assertAlmostEqual(eff.pass_anchor_scale("DET", None), 1.0)

    def test_optional_player_column_absent_does_not_change_the_rate(self) -> None:
        plain = player_week_from_row(
            {
                "player_name": "Amon-Ra St. Brown",
                "team_fd": "DET",
                "position": "WR",
                "week": 1,
                "season": 2026,
                "targets": 10,
                "receptions": 7,
                "receiving_yards": 80,
            }
        )
        aired = player_week_from_row(
            {
                "player_name": "Amon-Ra St. Brown",
                "team_fd": "DET",
                "position": "WR",
                "week": 1,
                "season": 2026,
                "targets": 10,
                "receptions": 7,
                "receiving_yards": 80,
                "receiving_air_yards": 150,
            }
        )
        assert plain is not None and aired is not None
        self.assertIsNone(plain.receiving_air_yards)
        self.assertIsNone(plain.wopr)
        self.assertIsNone(plain.goal_line_carries)
        player = _pl(name="Amon-Ra St. Brown")
        without = DataEfficiency(SimInputs(player_weeks=(plain,))).rates_for(player, "WR")
        with_air = DataEfficiency(SimInputs(player_weeks=(aired,))).rates_for(player, "WR")
        self.assertGreater(with_air["yards_per_target"], without["yards_per_target"])


class FallbackTest(unittest.TestCase):
    def test_empty_inputs_match_the_placeholder_draws(self) -> None:
        players = [
            _pl(pid="qb", name="Jared Goff", position="QB", salary=8000),
            _pl(pid="wr", name="Amon-Ra St. Brown", position="WR"),
        ]
        placeholder = simulate_games(
            players, n=40, seed=1, efficiency=PlaceholderEfficiency()
        )
        data = simulate_games(players, n=40, seed=1, efficiency=DataEfficiency())
        self.assertEqual(placeholder.draws["qb"], data.draws["qb"])
        self.assertEqual(placeholder.draws["wr"], data.draws["wr"])

    def test_prior_rates_match_placeholder_on_the_opportunity_path(self) -> None:
        target = TargetWeek(
            season=2026,
            week=1,
            position="WR",
            player_name="Amon-Ra St. Brown",
            team_fd="DET",
            targets=8,
            target_share=0.28,
            team_targets=30,
            team_pass_attempts=34,
        )
        inputs = SimInputs(targets=(target,))
        players = [
            _pl(pid="qb", name="Jared Goff", position="QB", salary=8000),
            _pl(pid="wr", name="Amon-Ra St. Brown", position="WR"),
        ]
        placeholder = simulate_games(
            players,
            n=25,
            seed=3,
            inputs=inputs,
            efficiency=build_efficiency("placeholder", inputs, before_week=2),
        )
        data = simulate_games(
            players,
            n=25,
            seed=3,
            inputs=inputs,
            efficiency=build_efficiency("data", inputs, before_week=2),
        )
        self.assertEqual(placeholder.draws["qb"], data.draws["qb"])
        self.assertEqual(placeholder.draws["wr"], data.draws["wr"])

    def test_receiving_line_without_history_matches_the_placeholder_rng(self) -> None:
        player = _pl()
        left = random.Random(1)
        right = random.Random(1)
        a = PlaceholderEfficiency().receiving_line(left, "WR", 8)
        b = DataEfficiency().receiving_line(right, "WR", 8, player=player)
        self.assertAlmostEqual(a.rec_yd, b.rec_yd, places=6)
        self.assertAlmostEqual(a.receptions, b.receptions, places=6)
        self.assertAlmostEqual(a.rec_td, b.rec_td, places=6)

    def test_omitted_mode_is_placeholder(self) -> None:
        self.assertIsInstance(build_efficiency(None, None), PlaceholderEfficiency)


class UsageHookTest(unittest.TestCase):
    def test_player_week_maps_usage_aliases_and_negative_air(self) -> None:
        row = player_week_from_row(
            {
                "gsis_id": "00-1",
                "team": "DET",
                "week": 1,
                "position": "WR",
                "rz_targets": 3,
                "gl_carries": 2,
                "receiving_air_yards": -12,
            }
        )
        old = player_week_from_row(
            {
                "player_name": "Amon-Ra St. Brown",
                "team_fd": "DET",
                "week": 1,
                "red_zone_targets": 4,
                "goal_line_carries": 1,
                "red_zone_carries": 5,
            }
        )
        assert row is not None and old is not None
        self.assertEqual(row.player_name, "")
        self.assertEqual(row.red_zone_targets, 3)
        self.assertEqual(row.goal_line_carries, 2)
        self.assertAlmostEqual(row.receiving_air_yards or 0, -12)
        self.assertEqual(old.red_zone_targets, 4)
        self.assertEqual(old.goal_line_carries, 1)
        self.assertEqual(old.red_zone_carries, 5)

    def test_goal_line_share_replaces_the_raw_td_rate(self) -> None:
        player = _pl(name="Derrick Henry", team="BAL", position="RB")
        common = dict(
            position="RB",
            player_name="Derrick Henry",
            team_fd="BAL",
            carries=20,
            rushing_yards=90,
            rushing_tds=2,
        )
        raw = DataEfficiency(
            SimInputs(player_weeks=(_week(1, **common),)),
            before_week=2,
        ).rates_for(player, "RB")
        even = DataEfficiency(
            SimInputs(
                player_weeks=(_week(1, goal_line_carries=20 * 0.08, **common),)
            ),
            before_week=2,
        ).rates_for(player, "RB")
        high = DataEfficiency(
            SimInputs(
                player_weeks=(_week(1, goal_line_carries=4, **common),)
            ),
            before_week=2,
        ).rates_for(player, "RB")
        prior = position_rates("RB")["rush_td_per_carry"]
        self.assertAlmostEqual(even["rush_td_per_carry"], prior, places=4)
        self.assertGreater(high["rush_td_per_carry"], prior)
        self.assertLessEqual(high["rush_td_per_carry"], prior * 2.0)
        self.assertGreater(raw["rush_td_per_carry"], high["rush_td_per_carry"])

    def test_red_zone_carry_share_is_used_when_goal_line_is_absent(self) -> None:
        player = _pl(name="Derrick Henry", team="BAL", position="RB")
        common = dict(
            position="RB",
            player_name="Derrick Henry",
            team_fd="BAL",
            carries=20,
            rushing_tds=2,
        )
        even = DataEfficiency(
            SimInputs(player_weeks=(_week(1, red_zone_carries=20 * 0.15, **common),)),
            before_week=2,
        ).rates_for(player, "RB")
        both = DataEfficiency(
            SimInputs(
                player_weeks=(
                    _week(1, red_zone_carries=10, goal_line_carries=20 * 0.08, **common),
                )
            ),
            before_week=2,
        ).rates_for(player, "RB")
        prior = position_rates("RB")["rush_td_per_carry"]
        self.assertAlmostEqual(even["rush_td_per_carry"], prior, places=4)
        self.assertAlmostEqual(both["rush_td_per_carry"], prior, places=4)

    def test_red_zone_target_share_sets_the_receiving_td_rate(self) -> None:
        player = _pl(name="Amon-Ra St. Brown")
        even = DataEfficiency(
            SimInputs(
                player_weeks=(
                    _week(1, targets=10, receiving_tds=2, red_zone_targets=10 * 0.12),
                )
            )
        ).rates_for(player, "WR")
        high = DataEfficiency(
            SimInputs(
                player_weeks=(
                    _week(1, targets=10, receiving_tds=2, red_zone_targets=4),
                )
            )
        ).rates_for(player, "WR")
        prior = position_rates("WR")["rec_td_per_target"]
        self.assertAlmostEqual(even["rec_td_per_target"], prior, places=4)
        self.assertGreater(high["rec_td_per_target"], prior)
        self.assertLessEqual(high["rec_td_per_target"], prior * 2.0)

    def test_air_yards_and_shares_move_yards_per_target(self) -> None:
        player = _pl(name="Amon-Ra St. Brown")
        base = dict(targets=10, receptions=7, receiving_yards=80)
        plain = DataEfficiency(
            SimInputs(player_weeks=(_week(1, **base),))
        ).rates_for(player, "WR")
        negative = DataEfficiency(
            SimInputs(player_weeks=(_week(1, receiving_air_yards=-20, **base),))
        ).rates_for(player, "WR")
        shared = DataEfficiency(
            SimInputs(
                player_weeks=(
                    _week(1, target_share=0.25, air_yards_share=0.40, **base),
                )
            )
        ).rates_for(player, "WR")
        self.assertLess(negative["yards_per_target"], plain["yards_per_target"])
        self.assertGreater(shared["yards_per_target"], plain["yards_per_target"])

    def test_team_stat_maps_live_names_and_pace(self) -> None:
        row = team_stat_from_row(
            {
                "team_fd": "NO",
                "side": "defense",
                "yards_per_carry": 5.2,
                "yards_per_dropback": 7.1,
                "sack_rate": 6.5,
                "pace_plays": 140,
                "pace_games": 2,
            }
        )
        assert row is not None
        self.assertAlmostEqual(row.yards_per_carry or 0, 5.2)
        self.assertAlmostEqual(row.yards_per_carry_allowed or 0, 5.2)
        self.assertAlmostEqual(row.yards_per_dropback or 0, 7.1)
        self.assertAlmostEqual(row.yards_per_dropback_allowed or 0, 7.1)
        self.assertAlmostEqual(row.sack_rate or 0, 0.065)
        self.assertAlmostEqual(row.plays_per_game or 0, 70)
        self.assertIsNone(row.seconds_per_play)

    def test_yards_allowed_stay_inside_both_clamps(self) -> None:
        defense = TeamStat(
            team_fd="NO",
            side="defense",
            week=1,
            yards_per_carry_allowed=6.5,
            yards_per_dropback_allowed=8.0,
            pass_n=800,
            rush_n=800,
            n=1600,
        )
        eff = DataEfficiency(SimInputs(team_weeks=(defense,)), before_week=2)
        self.assertGreater(eff.pass_multiplier("NO"), 1.0)
        self.assertLessEqual(eff.pass_multiplier("NO"), 1.0 + OPP_CLAMP)
        self.assertGreater(eff.rush_multiplier("NO"), 1.0)
        self.assertLessEqual(eff.rush_multiplier("NO"), 1.0 + OPP_CLAMP)
        scale = eff.rush_budget_scale("DET", "NO", 0.92)
        self.assertAlmostEqual(scale, 1.0 + COMBINED_CLAMP, places=4)
        self.assertLess(scale, 1.242)

    def test_plays_and_pace_blend_and_pace_is_clamped(self) -> None:
        plays_only = TeamStat(
            team_fd="DET",
            side="offense",
            plays_per_game=70,
            pace_games=1,
        )
        defense_plays = TeamStat(
            team_fd="NO",
            side="defense",
            plays_per_game=56,
            pace_games=1,
        )
        off_plays = (70 + 63 * 4) / 5
        def_plays = (56 + 63 * 4) / 5
        self.assertAlmostEqual(offense_plays_mu(plays_only), off_plays)
        self.assertAlmostEqual(
            offense_plays_mu(plays_only, defense_plays),
            (off_plays + def_plays) / 2,
        )
        league = TeamStat(
            team_fd="DET",
            side="offense",
            pass_n=35,
            rush_n=25,
            seconds_per_play=LEAGUE_SECONDS_PER_PLAY,
            neutral_seconds_per_play=LEAGUE_NEUTRAL_SECONDS_PER_PLAY,
        )
        self.assertAlmostEqual(offense_plays_mu(league), LEAGUE_PLAYS)
        fast = TeamStat(
            team_fd="DET",
            side="offense",
            plays_per_game=70,
            pace_games=1,
            seconds_per_play=10,
        )
        pace_cap = LEAGUE_PLAYS * (1.0 + PACE_CLAMP)
        self.assertAlmostEqual(offense_plays_mu(fast), (off_plays + pace_cap) / 2)
        slow = TeamStat(team_fd="DET", side="offense", seconds_per_play=100, pace_games=1)
        self.assertAlmostEqual(offense_plays_mu(slow), LEAGUE_PLAYS * (1.0 - PACE_CLAMP))
        counted = TeamStat(team_fd="DET", side="offense", pass_n=35, rush_n=25)
        self.assertAlmostEqual(offense_plays_mu(counted), 60)
        named = team_stat_from_row(
            {
                "timed_plays": 50,
                "team_fd": "DET",
                "side": "offense",
                "play_seconds": 1490,
            }
        )
        assert named is not None
        self.assertAlmostEqual(named.seconds_per_play or 0, 1490 / 50)
        explicit = team_stat_from_row(
            {
                "team_fd": "DET",
                "seconds_per_play": 29.8,
                "play_seconds": 1,
                "timed_plays": 1,
            }
        )
        assert explicit is not None
        self.assertAlmostEqual(explicit.seconds_per_play or 0, 29.8)


def _skill(pid, name, position, team, opponent, **kw):
    return _pl(
        pid=pid,
        name=name,
        position=position,
        team=team,
        opponent=opponent,
        game="DET@GB",
        total=46.0,
        spread=-3.0 if team == "GB" else 3.0,
        implied_total=21.5 if team == "GB" else 24.5,
        implied_opp=24.5 if team == "GB" else 21.5,
        depth_rank=1,
        salary=kw.pop("salary", 7000),
        **kw,
    )


def _pw(week, name, team, position, **kw):
    return _week(week, player_name=name, team_fd=team, position=position, **kw)


def regression_slate():
    """Two-team slate with weeks 1–2 of usage, air yards, and opponent rates.

    Captured against the pre-speed ``DataEfficiency`` (n=40, seed=11,
    before_week=3). The speed rewrite must match these draws.
    """
    players = [
        _skill("det-qb", "Jared Goff", "QB", "DET", "GB", salary=8000, prop_pass_yds=255.5),
        _skill(
            "det-rb",
            "Jahmyr Gibbs",
            "RB",
            "DET",
            "GB",
            salary=9000,
            prop_rush_yds=72.5,
        ),
        _skill("det-wr", "Amon-Ra St. Brown", "WR", "DET", "GB", salary=8500),
        _skill("det-te", "Sam LaPorta", "TE", "DET", "GB", salary=6400),
        _skill("gb-qb", "Jordan Love", "QB", "GB", "DET", salary=7800, prop_pass_yds=240.5),
        _skill("gb-rb", "Josh Jacobs", "RB", "GB", "DET", salary=7600, prop_rush_yds=68.5),
        _skill("gb-wr", "Romeo Doubs", "WR", "GB", "DET", salary=5600),
        _skill("gb-dst", "Packers", "D", "GB", "DET", salary=4000),
    ]
    weeks = []
    targets = []
    carries = []
    snaps = []
    counting = {
        "Jared Goff": dict(
            position="QB",
            team="DET",
            pass_attempts=34,
            completions=23,
            passing_yards=255,
            passing_tds=2,
            interceptions=1,
        ),
        "Jahmyr Gibbs": dict(
            position="RB",
            team="DET",
            carries=15,
            rushing_yards=72,
            rushing_tds=1,
            targets=5,
            receptions=4,
            receiving_yards=28,
            receiving_tds=0,
            receiving_air_yards=12,
            target_share=0.14,
            red_zone_targets=1,
            red_zone_carries=3,
            goal_line_carries=2,
        ),
        "Amon-Ra St. Brown": dict(
            position="WR",
            team="DET",
            targets=10,
            receptions=8,
            receiving_yards=96,
            receiving_tds=1,
            receiving_air_yards=78,
            target_share=0.28,
            air_yards_share=0.32,
            red_zone_targets=2,
        ),
        "Sam LaPorta": dict(
            position="TE",
            team="DET",
            targets=6,
            receptions=4,
            receiving_yards=44,
            receiving_tds=0,
            receiving_air_yards=31,
            target_share=0.16,
            red_zone_targets=1,
        ),
        "Jordan Love": dict(
            position="QB",
            team="GB",
            pass_attempts=32,
            completions=21,
            passing_yards=248,
            passing_tds=1,
            interceptions=1,
        ),
        "Josh Jacobs": dict(
            position="RB",
            team="GB",
            carries=18,
            rushing_yards=81,
            rushing_tds=1,
            targets=3,
            receptions=2,
            receiving_yards=14,
            receiving_air_yards=6,
            target_share=0.09,
            red_zone_carries=4,
            goal_line_carries=2,
        ),
        "Romeo Doubs": dict(
            position="WR",
            team="GB",
            targets=7,
            receptions=4,
            receiving_yards=61,
            receiving_tds=1,
            receiving_air_yards=70,
            target_share=0.22,
            air_yards_share=0.30,
            red_zone_targets=1,
        ),
    }
    for name, base in counting.items():
        team = base["team"]
        position = base["position"]
        for week in (1, 2):
            bumped = dict(base)
            if position == "QB":
                bumped["pass_attempts"] = base["pass_attempts"] + week
                bumped["passing_yards"] = base["passing_yards"] + 10 * week
            else:
                bumped["targets"] = base.get("targets", 0) + week
                bumped["receiving_yards"] = base.get("receiving_yards", 0) + 5 * week
            weeks.append(_pw(week, name, team, position, **{k: v for k, v in bumped.items() if k not in {"team", "position"}}))
            if position in {"WR", "TE", "RB"}:
                targets.append(
                    TargetWeek(
                        season=2026,
                        week=week,
                        position=position,
                        player_name=name,
                        team_fd=team,
                        targets=float(bumped.get("targets") or 0),
                        target_share=base.get("target_share"),
                        team_targets=36.0,
                        team_pass_attempts=34.0,
                    )
                )
            if position == "RB":
                carries.append(
                    CarryWeek(
                        season=2026,
                        week=week,
                        player_name=name,
                        team_fd=team,
                        carries=float(base["carries"]),
                        position="RB",
                    )
                )
            if position in {"WR", "TE", "RB", "QB"}:
                snaps.append(
                    SnapWeek(
                        season=2026,
                        week=week,
                        position=position,
                        player_name=name,
                        team_fd=team,
                        offense_pct=0.85 if position != "QB" else 1.0,
                    )
                )
    def side(team, side_name, **kw):
        return TeamStat(
            team_fd=team,
            side=side_name,
            week=1,
            pass_n=420,
            rush_n=280,
            n=700,
            **kw,
        )

    team_rows = (
        side("DET", "offense", pass_epa_per_play=0.14, rush_epa_per_play=0.04, pass_rate=0.57, yards_per_carry=4.6, yards_per_dropback=6.5, plays_per_game=65, pace_games=2),
        side("DET", "defense", pass_epa_per_play=-0.02, rush_epa_per_play=0.01, yards_per_carry_allowed=4.1, yards_per_dropback_allowed=5.8, sack_rate=0.072),
        side("GB", "offense", pass_epa_per_play=0.08, rush_epa_per_play=0.06, pass_rate=0.55, yards_per_carry=4.4, yards_per_dropback=6.2, plays_per_game=62, pace_games=2),
        side("GB", "defense", pass_epa_per_play=0.06, rush_epa_per_play=-0.03, yards_per_carry_allowed=4.5, yards_per_dropback_allowed=6.4, sack_rate=0.058),
    )
    inputs = SimInputs(
        player_weeks=tuple(weeks),
        team_weeks=team_rows,
        team_stats=team_rows,
        targets=tuple(targets),
        carries=tuple(carries),
        snaps=tuple(snaps),
    )
    return players, inputs


def regression_stats(n: int = 40, seed: int = 11) -> dict:
    players, inputs = regression_slate()
    sim = simulate_games(
        players,
        n=n,
        seed=seed,
        inputs=inputs,
        efficiency=DataEfficiency(inputs, before_week=3),
    )
    out = {}
    for pl in players:
        stats = sim.by_pid[pl.pid]
        out[pl.pid] = {
            "mean": stats.mean,
            "p10": stats.p10,
            "p50": stats.p50,
            "p90": stats.p90,
            "source": stats.source,
        }
    return out


class EfficiencyRegressionTest(unittest.TestCase):
    def test_same_seed_matches_the_fixture(self) -> None:
        path = Path(__file__).resolve().parent / "testdata" / "efficiency_regression.json"
        expected = json.loads(path.read_text(encoding="utf-8"))
        got = regression_stats(n=int(expected["n"]), seed=int(expected["seed"]))
        self.assertEqual(set(got), set(expected["players"]))
        for pid, row in expected["players"].items():
            live = got[pid]
            self.assertEqual(live["source"], row["source"], pid)
            for key in ("mean", "p10", "p50", "p90"):
                self.assertAlmostEqual(live[key], row[key], places=6, msg=f"{pid} {key}")
