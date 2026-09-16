"""Implied-total formula, spread sign, and FanDuel join."""

from __future__ import annotations

import unittest

from ncaaf.lines import (
    LinesError,
    implied_totals,
    parse_cfbd_games,
    parse_fanduel_game,
    parse_simple_games,
)
from ncaaf.projections import VALUE_EPS, week1_score
from ncaaf.teams import UnmappedTeam, lookup_cfbd, lookup_odds, require_mapped


class ImpliedTotalsTest(unittest.TestCase):
    def test_home_underdog(self):
        # CFBD: home BC, away VT, spread +4.5, formatted "Virginia Tech -4.5", total 57.5
        home, away = implied_totals(57.5, 4.5)
        self.assertAlmostEqual(home, 26.5)
        self.assertAlmostEqual(away, 31.0)

    def test_home_favorite(self):
        home, away = implied_totals(60.5, -29.5)
        self.assertAlmostEqual(home, 45.0)
        self.assertAlmostEqual(away, 15.5)
        self.assertAlmostEqual(home + away, 60.5)
        self.assertAlmostEqual(home - away, 29.5)

    def test_value_term_cannot_invert_half_point(self):
        high_expensive = week1_score(45.0, 12_000, depth_rank=1)
        low_cheap = week1_score(44.5, 4_000, depth_rank=1)
        self.assertGreater(high_expensive, low_cheap)
        self.assertLess(VALUE_EPS, 0.5)


class JoinTest(unittest.TestCase):
    def test_game_column(self):
        self.assertEqual(parse_fanduel_game("TXST@TEX"), ("TXST", "TEX"))

    def test_unmapped_fails_loud(self):
        with self.assertRaises(UnmappedTeam):
            require_mapped({"TEX", "ZZZ"})

    def test_slate_134050_mapped(self):
        refs = require_mapped(
            {
                "ARIZ",
                "ASU",
                "BYU",
                "CAL",
                "GT",
                "ILL",
                "IOWA",
                "ISU",
                "MINN",
                "MSST",
                "OKST",
                "OU",
                "PITT",
                "PUR",
                "SYR",
                "TENN",
                "TTU",
                "UCF",
                "UK",
                "WAKE",
                "BAMA",
                "DUKE",
                "MICH",
                "ORE",
                "ORST",
                "OSU",
                "TEX",
                "TXAM",
            }
        )
        self.assertEqual(refs["ARIZ"].cfbd, "Arizona")
        self.assertEqual(refs["OU"].cfbd, "Oklahoma")
        self.assertEqual(refs["TEX"].cfbd, "Texas")
        self.assertEqual(refs["TXAM"].cfbd, "Texas A&M")
        self.assertEqual(lookup_odds("Arizona Wildcats").fd, "ARIZ")
        self.assertEqual(lookup_odds("BYU Cougars").fd, "BYU")
        self.assertEqual(lookup_odds("UCF Knights").fd, "UCF")
        self.assertEqual(lookup_cfbd("Georgia Tech").fd, "GT")
        self.assertEqual(lookup_cfbd("Mississippi State").fd, "MSST")

    def test_simple_json_join(self):
        slate = [("TXST@TEX", "TXST", "TEX"), ("BALL@OSU", "BALL", "OSU")]
        payload = [
            {"away": "TXST", "home": "TEX", "spread": -29.5, "total": 60.5},
            {"away": "BALL", "home": "OSU", "spread": -50.5, "total": 56.5},
        ]
        by_team = parse_simple_games(payload, slate)
        self.assertAlmostEqual(by_team["TEX"].implied_home, 45.0)
        self.assertAlmostEqual(by_team["TXST"].implied_away, 15.5)
        self.assertEqual(by_team["TEX"].home_spread, -29.5)
        self.assertEqual(by_team["OSU"].spread_for("BALL"), 50.5)

    def test_missing_game_fails(self):
        slate = [("TXST@TEX", "TXST", "TEX")]
        with self.assertRaises(LinesError):
            parse_simple_games([], slate)

    def test_cfbd_consensus_and_formatted_spread(self):
        slate = [("BAY@AUB", "BAY", "AUB")]
        payload = [
            {
                "homeTeam": "Auburn",
                "awayTeam": "Baylor",
                "lines": [
                    {
                        "provider": "consensus",
                        "spread": -7.5,
                        "formattedSpread": "Auburn -7.5",
                        "overUnder": 59.5,
                        "homeMoneyline": -320,
                        "awayMoneyline": 260,
                    }
                ],
            }
        ]
        by_team = parse_cfbd_games(payload, slate)
        self.assertAlmostEqual(by_team["AUB"].implied_home, 33.5)
        self.assertAlmostEqual(by_team["BAY"].implied_away, 26.0)

    def test_cfbd_sign_mismatch_fails(self):
        slate = [("BAY@AUB", "BAY", "AUB")]
        payload = [
            {
                "homeTeam": "Auburn",
                "awayTeam": "Baylor",
                "lines": [
                    {
                        "provider": "consensus",
                        "spread": 7.5,
                        "formattedSpread": "Auburn -7.5",
                        "overUnder": 59.5,
                    }
                ],
            }
        ]
        with self.assertRaises(LinesError):
            parse_cfbd_games(payload, slate)


class IlpMinTeamsTest(unittest.TestCase):
    def test_min_three_teams_when_two_would_score_more(self):
        from dataclasses import replace as dc_replace

        from ncaaf.players import Player
        from ncaaf.rules import FANDUEL_NCAAF
        from ncaaf.solver import solve_ilp

        def P(pid, pos, team, obj, salary=4000):
            return Player(
                pid=pid,
                name=pid,
                position=pos,
                salary=salary,
                team=team,
                opponent="X",
                game=f"{team}@X",
                fppg=None,
                injury="",
                roster_position="",
                objective=obj,
            )

        pool = [
            P("t1qb", "QB", "T1", 50),
            P("t1rb1", "RB", "T1", 50),
            P("t1rb2", "RB", "T1", 50),
            P("t1wr1", "WR", "T1", 50),
            P("t2rb", "RB", "T2", 49),
            P("t2wr1", "WR", "T2", 49),
            P("t2wr2", "WR", "T2", 49),
            P("t3wr", "WR", "T3", 1),
        ]
        lineup = solve_ilp(
            pool,
            dc_replace(
                FANDUEL_NCAAF,
                salary_floor=0,
                superflex_eligible=frozenset({"QB", "RB", "WR", "TE"}),
            ),
        )
        self.assertGreaterEqual(len(lineup.teams), 3)
        self.assertIn("T3", lineup.teams)


if __name__ == "__main__":
    unittest.main()
