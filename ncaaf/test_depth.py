"""OurLads depth parse, name normalize, FanDuel join."""

from __future__ import annotations

import unittest

from ncaaf.depth import attach_depth_ranks, depth_index
from ncaaf.ourlads import (
    DepthRow,
    display_name,
    match_key,
    norm_name,
    parse_index_slugs,
    parse_offense_depth,
    skill_pos,
)
from ncaaf.players import Player
from ncaaf.projections import week1_score


FIXTURE = """
<html><body>
<table class="table">
<thead><tr><th>Pos</th><th>No.</th><th>Player 1</th><th>No</th>
<th>Player 2</th><th>No</th><th>Player 3</th></tr></thead>
<tbody>
<tr><td>WR-X</td><td>8</td><td>Coleman, Cam JR/TR</td><td>18</td>
<td>Berkhalter, Sterling RS SR/TR</td><td>13</td><td>Brown, Kohen FR</td></tr>
<tr><td>WR-Z</td><td>1</td><td>Wingo, Ryan JR</td><td>2</td>
<td>Bishop Jr., Jermaine FR</td><td></td><td></td></tr>
<tr><td>LT</td><td>70</td><td>Goosby, Trevor RS JR</td><td></td><td></td><td></td><td></td></tr>
<tr><td>TE-Y</td><td>80</td><td>Shannon, Spencer RS JR</td><td>81</td>
<td>Masunas, Michael RS SR/TR</td><td></td><td></td></tr>
<tr><td>QB</td><td>16</td><td>Manning, Arch RS JR</td><td>10</td>
<td>Lacey Jr., Karle RS FR</td><td>7</td><td>Bell, Dia FR</td></tr>
<tr><td>RB</td><td>9</td><td>Smothers, Hollywood RS JR/TR</td><td>5</td>
<td>Brown, Raleek RS SR/TR</td><td></td><td></td></tr>
</tbody></table>
</body></html>
"""

INDEX = """
<a href="depth-chart.aspx?s=texas&id=92016">Texas</a>
<a href="depth-chart.aspx?s=ohio-state&id=91533">Ohio State</a>
<a href="/ncaa-football-depth-charts/depth-chart/texas-state/92821">Texas State</a>
"""


def _p(name: str, team: str = "TEX", pos: str = "QB") -> Player:
    return Player(
        pid=name,
        name=name,
        position=pos,
        salary=8000,
        team=team,
        opponent="TXST",
        game="TXST@TEX",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=45.0,
    )


class ParseTest(unittest.TestCase):
    def test_index_slugs(self):
        slugs = parse_index_slugs(INDEX)
        self.assertEqual(slugs["texas"], "92016")
        self.assertEqual(slugs["ohio-state"], "91533")
        self.assertEqual(slugs["texas-state"], "92821")

    def test_skill_pos(self):
        self.assertEqual(skill_pos("WR-H"), "WR")
        self.assertEqual(skill_pos("TE-Y"), "TE")
        self.assertIsNone(skill_pos("LT"))

    def test_names(self):
        self.assertEqual(display_name("Manning, Arch RS JR"), "Arch Manning")
        self.assertEqual(display_name("Bishop Jr., Jermaine FR"), "Jermaine Bishop Jr.")
        self.assertEqual(norm_name("Manning, Arch RS JR"), "arch manning")
        self.assertEqual(match_key("Jabari Smith Jr."), match_key("Jabari Smith"))

    def test_offense_rows(self):
        rows = parse_offense_depth(FIXTURE)
        qb = [(p, r, n) for p, r, n in rows if p == "QB"]
        self.assertEqual(qb[0], ("QB", 1, "Arch Manning"))
        self.assertEqual(qb[1][2], "Karle Lacey Jr.")
        wr1 = [n for p, r, n in rows if p == "WR" and r == 1]
        self.assertIn("Cam Coleman", wr1)
        self.assertIn("Ryan Wingo", wr1)
        self.assertFalse(any("Goosby" in n for _, _, n in rows))


class JoinTest(unittest.TestCase):
    def test_join_and_prior(self):
        parsed = parse_offense_depth(FIXTURE)
        depth = [
            DepthRow("TEX", p, r, n, "u", "t") for p, r, n in parsed
        ]
        players = [
            _p("Arch Manning", pos="QB"),
            _p("Ryan Wingo", pos="WR"),
            _p("Walk On", pos="WR"),
        ]
        attached, stats = attach_depth_ranks(players, depth)
        by = {p.name: p.depth_rank for p in attached}
        self.assertEqual(by["Arch Manning"], 1)
        self.assertEqual(by["Ryan Wingo"], 1)
        self.assertIsNone(by["Walk On"])
        self.assertEqual(stats["matched"], 2)
        starter = week1_score(45.0, 8000, 1)
        unlisted = week1_score(45.0, 4000, None)
        self.assertGreater(starter, unlisted)


if __name__ == "__main__":
    unittest.main()
