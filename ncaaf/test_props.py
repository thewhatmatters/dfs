"""Odds API player-prop parse, FD points, overlay vs implied fallback."""

from __future__ import annotations

import unittest

from ncaaf.players import Player
from ncaaf.projections import week1_score
from ncaaf.props import PlayerProp, attach_props, parse_event_props


PAYLOAD = {
    "bookmakers": [
        {
            "key": "draftkings",
            "markets": [
                {
                    "key": "player_pass_yds",
                    "outcomes": [
                        {"name": "Over", "description": "Arch Manning", "point": 240.5},
                        {"name": "Under", "description": "Arch Manning", "point": 240.5},
                    ],
                }
            ],
        },
        {
            "key": "fanduel",
            "markets": [
                {
                    "key": "player_pass_yds",
                    "outcomes": [
                        {"name": "Over", "description": "Arch Manning", "point": 250.5},
                    ],
                },
                {
                    "key": "player_pass_tds",
                    "outcomes": [
                        {"name": "Over", "description": "Arch Manning", "point": 1.5},
                    ],
                },
                {
                    "key": "player_rush_yds",
                    "outcomes": [
                        {"name": "Over", "description": "Arch Manning", "point": 25.5},
                    ],
                },
            ],
        },
    ]
}


def _p(name: str, pos: str, team: str = "TEX", salary: int = 11000) -> Player:
    return Player(
        pid=name,
        name=name,
        position=pos,
        salary=salary,
        team=team,
        opponent="TXST",
        game="TXST@TEX",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=45.0,
        depth_rank=1,
    )


class ParseTest(unittest.TestCase):
    def test_prefers_fanduel_then_fills(self):
        props = {p.name: p for p in parse_event_props(PAYLOAD)}
        # stored as match_key
        man = props["arch manning"]
        self.assertAlmostEqual(man.pass_yds, 250.5)
        self.assertAlmostEqual(man.pass_tds, 1.5)
        self.assertAlmostEqual(man.rush_yds, 25.5)
        fd = man.fd_points()
        assert fd is not None
        # 250.5*0.04 + 1.5*4 + 25.5*0.1 = 10.02 + 6 + 2.55
        self.assertAlmostEqual(fd, 18.57, places=2)


class OverlayTest(unittest.TestCase):
    def test_prop_qb_beats_cheap_te_on_same_implied(self):
        qb = _p("Arch Manning", "QB", salary=11000)
        te = _p("Spencer Shannon", "TE", salary=4000)
        prop = PlayerProp(
            name="arch manning",
            pass_yds=250.5,
            pass_tds=1.5,
            rush_yds=25.5,
            book="fanduel",
        )
        attached = attach_props([qb, te], {qb.pid: prop})
        by = {p.name: p for p in attached}
        self.assertIsNotNone(by["Arch Manning"].prop_fd)
        self.assertIsNone(by["Spencer Shannon"].prop_fd)
        self.assertEqual(by["Arch Manning"].prop_status, "props")
        self.assertEqual(by["Spencer Shannon"].prop_status, "no_market")
        self.assertGreater(by["Arch Manning"].projection, by["Spencer Shannon"].projection)

    def test_unmatched_last_name_on_same_game(self):
        a = _p("John Smith", "WR")
        b = _p("Jack Smith", "WR")
        other = _p("Walk On", "WR", team="OSU")
        attached = attach_props(
            [a, b, other],
            {},
            unmatched=[{"name": "j smith", "home": "TEX", "away": "TXST"}],
        )
        by = {p.name: p.prop_status for p in attached}
        self.assertEqual(by["John Smith"], "unmatched")
        self.assertEqual(by["Jack Smith"], "unmatched")
        self.assertEqual(by["Walk On"], "no_market")

    def test_fallback_qb_share_beats_te(self):
        qb = week1_score(45.0, 11000, depth_rank=1, position="QB")
        te = week1_score(45.0, 4000, depth_rank=1, position="TE")
        self.assertGreater(qb, te)


if __name__ == "__main__":
    unittest.main()
