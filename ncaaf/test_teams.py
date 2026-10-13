"""Sep 26 2026 FanDuel slate abbrevs join to distinct CFBD / Odds names."""

from __future__ import annotations

import unittest

from ncaaf.choke import depth_id, line, lines_id
from ncaaf.depth import NAME_OVERRIDES
from ncaaf.lines import parse_cfbd_games, parse_odds_games
from ncaaf.ourlads import OURLADS_SLUG, require_slugs
from ncaaf.teams import BY_CFBD, BY_ODDS, TEAMS, UnmappedTeam, require_mapped

# Confirmed against one OurLads NCAA index fetch (slug → id present).
OURLADS_NEW = {
    "COLO": "colorado",
    "MIZZ": "missouri",
    "SCAR": "south-carolina",
    "TCU": "tcu",
    "UF": "florida",
    "UGA": "georgia",
    "USC": "usc",
    "UTAH": "utah",
    "WVU": "west-virginia",
}

# AWAY@HOME. FanDuel code -> (CFBD school, Odds API full name).
SLATE_2026_09_26 = {
    "COLO": ("Colorado", "Colorado Buffaloes"),
    "BAY": ("Baylor", "Baylor Bears"),
    "ILL": ("Illinois", "Illinois Fighting Illini"),
    "OSU": ("Ohio State", "Ohio State Buckeyes"),
    "IOWA": ("Iowa", "Iowa Hawkeyes"),
    "MICH": ("Michigan", "Michigan Wolverines"),
    "MISS": ("Ole Miss", "Ole Miss Rebels"),
    "UF": ("Florida", "Florida Gators"),
    "MIZZ": ("Missouri", "Missouri Tigers"),
    "MSST": ("Mississippi State", "Mississippi State Bulldogs"),
    "OKST": ("Oklahoma State", "Oklahoma State Cowboys"),
    "WVU": ("West Virginia", "West Virginia Mountaineers"),
    "ORE": ("Oregon", "Oregon Ducks"),
    "USC": ("USC", "USC Trojans"),
    "OU": ("Oklahoma", "Oklahoma Sooners"),
    "UGA": ("Georgia", "Georgia Bulldogs"),
    "SCAR": ("South Carolina", "South Carolina Gamecocks"),
    "BAMA": ("Alabama", "Alabama Crimson Tide"),
    "TCU": ("TCU", "TCU Horned Frogs"),
    "UCF": ("UCF", "UCF Knights"),
    "TEX": ("Texas", "Texas Longhorns"),
    "TENN": ("Tennessee", "Tennessee Volunteers"),
    "TXAM": ("Texas A&M", "Texas A&M Aggies"),
    "LSU": ("LSU", "LSU Tigers"),
    "UTAH": ("Utah", "Utah Utes"),
    "ISU": ("Iowa State", "Iowa State Cyclones"),
    "WAKE": ("Wake Forest", "Wake Forest Demon Deacons"),
    "LOU": ("Louisville", "Louisville Cardinals"),
}

GAMES = (
    ("COLO@BAY", "COLO", "BAY"),
    ("ILL@OSU", "ILL", "OSU"),
    ("IOWA@MICH", "IOWA", "MICH"),
    ("MISS@UF", "MISS", "UF"),
    ("MIZZ@MSST", "MIZZ", "MSST"),
    ("OKST@WVU", "OKST", "WVU"),
    ("ORE@USC", "ORE", "USC"),
    ("OU@UGA", "OU", "UGA"),
    ("SCAR@BAMA", "SCAR", "BAMA"),
    ("TCU@UCF", "TCU", "UCF"),
    ("TEX@TENN", "TEX", "TENN"),
    ("TXAM@LSU", "TXAM", "LSU"),
    ("UTAH@ISU", "UTAH", "ISU"),
    ("WAKE@LOU", "WAKE", "LOU"),
)


def _odds_payload():
    rows = []
    for _game, away, home in GAMES:
        away_name = SLATE_2026_09_26[away][1]
        home_name = SLATE_2026_09_26[home][1]
        rows.append(
            {
                "home_team": home_name,
                "away_team": away_name,
                "bookmakers": [
                    {
                        "key": "fanduel",
                        "markets": [
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {"name": home_name, "point": -3.5},
                                    {"name": away_name, "point": 3.5},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "point": 51.5},
                                    {"name": "Under", "point": 51.5},
                                ],
                            },
                        ],
                    }
                ],
            }
        )
    return rows


def _cfbd_payload():
    rows = []
    for _game, away, home in GAMES:
        away_name = SLATE_2026_09_26[away][0]
        home_name = SLATE_2026_09_26[home][0]
        rows.append(
            {
                "homeTeam": home_name,
                "awayTeam": away_name,
                "lines": [
                    {
                        "provider": "consensus",
                        "spread": -3.5,
                        "formattedSpread": home_name + " -3.5",
                        "overUnder": 51.5,
                    }
                ],
            }
        )
    return rows


class Sep26SlateTeamsTest(unittest.TestCase):
    def test_twenty_eight_codes_map_to_distinct_teams(self):
        self.assertEqual(len(SLATE_2026_09_26), 28)
        self.assertEqual(len(GAMES), 14)
        try:
            refs = require_mapped(set(SLATE_2026_09_26))
        except UnmappedTeam as exc:
            self.fail(line(lines_id(exc), str(exc)))
        self.assertEqual(lines_id(UnmappedTeam("probe")), "LINES_JOIN")
        self.assertEqual(set(refs), set(SLATE_2026_09_26))
        schools = []
        odds_names = []
        for code, (school, odds_name) in SLATE_2026_09_26.items():
            ref = refs[code]
            self.assertEqual(ref.fd, code)
            self.assertEqual(ref.cfbd, school)
            self.assertEqual(ref.odds, (odds_name,))
            self.assertIs(TEAMS[code], ref)
            schools.append(school)
            odds_names.append(odds_name)
        self.assertEqual(len(set(schools)), 28)
        self.assertEqual(len(set(odds_names)), 28)
        self.assertNotEqual(refs["USC"].cfbd, refs["SCAR"].cfbd)
        self.assertNotEqual(refs["USC"].odds, refs["SCAR"].odds)
        self.assertEqual(refs["MISS"].cfbd, "Ole Miss")
        self.assertEqual(refs["MSST"].cfbd, "Mississippi State")
        self.assertNotEqual(refs["UF"].cfbd, refs["UCF"].cfbd)
        self.assertNotEqual(refs["OKST"].cfbd, refs["OU"].cfbd)
        self.assertNotEqual(refs["MIZZ"].cfbd, TEAMS["MOST"].cfbd)
        self.assertNotEqual(refs["TEX"].cfbd, TEAMS["TXST"].cfbd)
        self.assertNotEqual(refs["TEX"].cfbd, refs["TXAM"].cfbd)

    def test_odds_and_cfbd_join_skip_lines_join(self):
        codes = {code for _game, away, home in GAMES for code in (away, home)}
        self.assertEqual(codes, set(SLATE_2026_09_26))
        try:
            odds = parse_odds_games(_odds_payload(), list(GAMES))
            cfbd = parse_cfbd_games(_cfbd_payload(), list(GAMES))
        except UnmappedTeam as exc:
            self.fail(line(lines_id(exc), str(exc)))
        self.assertEqual(set(odds), codes)
        self.assertEqual(set(cfbd), codes)
        for game, away, home in GAMES:
            self.assertEqual(odds[home].game, game)
            self.assertEqual(odds[away].away_fd, away)
            self.assertEqual(odds[home].home_fd, home)
            self.assertEqual(cfbd[home].home_fd, home)
            self.assertEqual(cfbd[away].away_fd, away)
            self.assertAlmostEqual(odds[home].implied_home, 27.5)
            self.assertAlmostEqual(cfbd[away].implied_away, 24.0)

    def test_slate_codes_in_every_team_lookup(self):
        """Every FanDuel-code table that can choke must cover the 28 codes."""
        codes = set(SLATE_2026_09_26)
        self.assertEqual(len(codes), 28)
        tables = {
            "TEAMS": set(TEAMS),
            "OURLADS_SLUG": set(OURLADS_SLUG),
            "BY_CFBD": {ref.fd for ref in BY_CFBD.values()},
            "BY_ODDS": {ref.fd for ref in BY_ODDS.values()},
        }
        for name, keys in tables.items():
            missing = sorted(codes - keys)
            self.assertEqual(missing, [], name)
        try:
            slugs = require_slugs(codes)
            refs = require_mapped(codes)
        except UnmappedTeam as exc:
            self.fail(
                line(depth_id(exc), str(exc))
                + " / "
                + line(lines_id(exc), str(exc))
            )
        self.assertEqual(set(slugs), codes)
        self.assertEqual(len(set(slugs.values())), 28)
        self.assertEqual(len(set(OURLADS_SLUG.values())), len(OURLADS_SLUG))
        self.assertEqual(set(refs), codes)
        for code, slug in OURLADS_NEW.items():
            self.assertEqual(OURLADS_SLUG[code], slug)
        self.assertNotEqual(slugs["USC"], slugs["SCAR"])
        self.assertNotEqual(slugs["UF"], slugs["UCF"])
        self.assertNotEqual(slugs["MIZZ"], OURLADS_SLUG["MOST"])
        self.assertNotEqual(slugs["OKST"], slugs["OU"])
        self.assertNotEqual(slugs["TEX"], OURLADS_SLUG["TXST"])
        self.assertNotEqual(slugs["TEX"], slugs["TXAM"])
        self.assertEqual(OURLADS_SLUG["MOST"], "missouri-state")
        self.assertEqual(OURLADS_SLUG["UCF"], "central-florida")
        self.assertEqual(OURLADS_SLUG["TXST"], "texas-state")
        self.assertEqual(OURLADS_SLUG["TXAM"], "texas-am")
        # Sparse player aliases. Missing teams do not choke DEPTH_JOIN.
        self.assertIsInstance(NAME_OVERRIDES, dict)
        for (team, _key), nick in NAME_OVERRIDES.items():
            self.assertIn(team, TEAMS)
            self.assertTrue(nick)


if __name__ == "__main__":
    unittest.main()
