"""OurLads NFL depth parse, name normalize, FanDuel join, choke ids."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nfl.choke import depth_id, line
from nfl.depth import EspnDepthError, attach_depth_ranks, depth_index
from nfl.ourlads import (
    DepthError,
    DepthRow,
    display_name,
    ingest_slate_depth,
    load_depth_csv,
    match_key,
    norm_name,
    parse_offense_depth,
    require_keys,
    skill_pos,
    team_url,
)
from nfl.players import Player
from nfl.projections import week1_score
from nfl.teams import UnmappedTeam

# Trimmed NFL offense + defense tables (Player 1–5). Defense must not join.
FIXTURE = """
<html><body>
<div id="ctl00_phContent_DepWrapper" class="table-responsive dc-dt dt-KC">
<table class="table table-bordered">
<thead><tr><th>Pos</th><th>No.</th><th>Player 1</th><th>No</th>
<th>Player 2</th><th>No</th><th>Player 3</th><th>No</th>
<th>Player 4</th><th>No</th><th>Player 5</th></tr></thead>
<tbody>
<tr><td>LWR</td><td>2</td><td>Thornton, Tyquan SF24</td><td>13</td>
<td>Allen, Cyrus 26/5</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>RWR</td><td>1</td><td>Worthy, Xavier 24/1</td><td>81</td>
<td>Remigio, Nikko CF23</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>SWR</td><td>4</td><td>Rice, Rashee 23/2</td><td>11</td>
<td>Royals, Jalen 25/4</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>LT</td><td>71</td><td>Simmons, Josh 25/1 O</td><td></td><td></td>
<td></td><td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>TE</td><td>87</td><td>KELCE, TRAVIS 13/3</td><td>83</td>
<td>Gray, Noah 21/5</td><td>88</td><td>Briningstool, Jake CF25</td>
<td></td><td></td><td></td><td></td></tr>
<tr><td>QB</td><td>15</td><td>MAHOMES, PATRICK 17/1</td><td>6</td>
<td>Fields, Justin T/NYJ</td><td>19</td><td>Nussmeier, Garrett 26/7</td>
<td></td><td></td><td></td><td></td></tr>
<tr><td>RB</td><td>9</td><td>Walker, Kenneth U/Sea</td><td>10</td>
<td>Johnson, Emmett 26/5</td><td>24</td><td>Smith, Brashard 25/7</td>
<td></td><td></td><td></td><td></td></tr>
<tr><td>FB</td><td>47</td><td>VanSumeren, Ben SF26</td><td></td><td></td>
<td></td><td></td><td></td><td></td><td></td><td></td></tr>
</tbody></table>
</div>
<table class="table table-bordered">
<thead><tr><th>Pos</th><th>No.</th><th>Player 1</th><th>No</th>
<th>Player 2</th><th>No</th><th>Player 3</th></tr></thead>
<tbody>
<tr><td>LDE</td><td>56</td><td>Karlaftis, George 22/1</td><td>51</td>
<td>Thomas, R Mason 26/2</td><td></td><td></td></tr>
</tbody></table>
</body></html>
"""

JAX_NAMES = """
<html><body>
<table class="table table-bordered">
<thead><tr><th>Pos</th><th>No.</th><th>Player 1</th><th>No</th>
<th>Player 2</th><th>No</th><th>Player 3</th></tr></thead>
<tbody>
<tr><td>LWR</td><td>7</td><td>Thomas Jr., Brian 24/1</td><td>19</td>
<td>Cameron, Josh 26/6</td><td></td><td></td></tr>
<tr><td>QB</td><td>16</td><td>Lawrence, Trevor 21/1</td><td>10</td>
<td>Ewers, Quinn T/Mia</td><td></td><td></td></tr>
<tr><td>RB</td><td>33</td><td>Tuten, Bhayshul 25/4</td><td>24</td>
<td>Rodriguez Jr., Chris U/Was</td><td>5</td>
<td>Allen Jr., LeQuint 25/7</td></tr>
</tbody></table>
</body></html>
"""

WAS_NAMES = """
<html><body>
<table class="table table-bordered">
<thead><tr><th>Pos</th><th>No.</th><th>Player 1</th><th>No</th>
<th>Player 2</th></tr></thead>
<tbody>
<tr><td>LWR</td><td>17</td><td>MCLAURIN, TERRY 19/3</td><td>14</td>
<td>Williams, Antonio 26/3</td></tr>
<tr><td>SWR</td><td>3</td><td>DIGGS, STEFON CC/NE</td><td></td><td></td></tr>
<tr><td>QB</td><td>5</td><td>Daniels, Jayden 24/1</td><td>8</td>
<td>MARIOTA, MARCUS U/Phi</td></tr>
</tbody></table>
</body></html>
"""


def _p(name: str, team: str = "KC", pos: str = "QB") -> Player:
    return Player(
        pid=name,
        name=name,
        position=pos,
        salary=8000,
        team=team,
        opponent="JAC",
        game="JAC@KC",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=24.0,
    )


class ParseTest(unittest.TestCase):
    def test_skill_pos(self):
        self.assertEqual(skill_pos("LWR"), "WR")
        self.assertEqual(skill_pos("RWR"), "WR")
        self.assertEqual(skill_pos("SWR"), "WR")
        self.assertEqual(skill_pos("TE"), "TE")
        self.assertEqual(skill_pos("QB"), "QB")
        self.assertEqual(skill_pos("RB"), "RB")
        self.assertIsNone(skill_pos("LT"))
        self.assertIsNone(skill_pos("FB"))
        self.assertIsNone(skill_pos("LDE"))

    def test_names_nfl_tails(self):
        self.assertEqual(display_name("MAHOMES, PATRICK 17/1"), "Patrick Mahomes")
        self.assertEqual(display_name("KELCE, TRAVIS 13/3"), "Travis Kelce")
        self.assertEqual(display_name("Walker, Kenneth U/Sea"), "Kenneth Walker")
        self.assertEqual(display_name("Fields, Justin T/NYJ"), "Justin Fields")
        self.assertEqual(display_name("Thomas Jr., Brian 24/1"), "Brian Thomas Jr.")
        self.assertEqual(display_name("Rodriguez Jr., Chris U/Was"), "Chris Rodriguez Jr.")
        self.assertEqual(display_name("Simmons, Josh 25/1 O"), "Josh Simmons")
        self.assertEqual(display_name("DIGGS, STEFON CC/NE"), "Stefon Diggs")
        self.assertEqual(display_name("MCLAURIN, TERRY 19/3"), "Terry McLaurin")
        self.assertEqual(norm_name("MAHOMES, PATRICK 17/1"), "patrick mahomes")
        self.assertEqual(match_key("Thomas Jr., Brian 24/1"), match_key("Brian Thomas"))
        self.assertEqual(match_key("MCLAURIN, TERRY 19/3"), match_key("Terry McLaurin"))

    def test_offense_rows_drop_ol_fb_defense(self):
        rows = parse_offense_depth(FIXTURE)
        qb = [(p, r, n) for p, r, n in rows if p == "QB"]
        self.assertEqual(qb[0], ("QB", 1, "Patrick Mahomes"))
        self.assertEqual(qb[1][2], "Justin Fields")
        wr1 = [n for p, r, n in rows if p == "WR" and r == 1]
        self.assertIn("Tyquan Thornton", wr1)
        self.assertIn("Xavier Worthy", wr1)
        self.assertIn("Rashee Rice", wr1)
        names = [n for _, _, n in rows]
        self.assertFalse(any("Simmons" in n for n in names))
        self.assertFalse(any("VanSumeren" in n for n in names))
        self.assertFalse(any("Karlaftis" in n for n in names))
        self.assertIn("Travis Kelce", names)

    def test_jax_was_jr_and_caps(self):
        jax = parse_offense_depth(JAX_NAMES)
        self.assertIn(("WR", 1, "Brian Thomas Jr."), jax)
        self.assertIn(("RB", 2, "Chris Rodriguez Jr."), jax)
        was = parse_offense_depth(WAS_NAMES)
        self.assertIn(("WR", 1, "Terry McLaurin"), was)
        self.assertIn(("WR", 1, "Stefon Diggs"), was)
        self.assertIn(("QB", 2, "Marcus Mariota"), was)

    def test_missing_player1_header(self):
        with self.assertRaises(DepthError):
            parse_offense_depth("<html><body>no table</body></html>")


class TeamKeyTest(unittest.TestCase):
    def test_jac_jax_was_not_wsh_ari_arz(self):
        keys = require_keys({"JAC", "WAS", "ARI", "KC", "JAX", "WSH"})
        self.assertEqual(keys["JAC"], "JAX")
        self.assertEqual(keys["WAS"], "WAS")
        self.assertEqual(keys["ARI"], "ARZ")
        self.assertEqual(keys["KC"], "KC")
        self.assertNotIn("WSH", keys)
        self.assertNotIn("JAX", keys)
        self.assertEqual(team_url("JAX"), "https://www.ourlads.com/nfldepthcharts/depthchart/JAX")
        self.assertEqual(team_url("WAS"), "https://www.ourlads.com/nfldepthcharts/depthchart/WAS")
        self.assertEqual(team_url("ARZ"), "https://www.ourlads.com/nfldepthcharts/depthchart/ARZ")

    def test_unmapped_fails_loud(self):
        with self.assertRaises(UnmappedTeam) as ctx:
            require_keys({"ZZZ"})
        self.assertIn("ZZZ", str(ctx.exception))


class JoinTest(unittest.TestCase):
    def test_join_and_prior(self):
        parsed = parse_offense_depth(FIXTURE)
        depth = [DepthRow("KC", p, r, n, "u", "t") for p, r, n in parsed]
        players = [
            _p("Patrick Mahomes", pos="QB"),
            _p("Rashee Rice", pos="WR"),
            _p("Walk On", pos="WR"),
            _p("Chiefs", pos="D"),
        ]
        attached, stats = attach_depth_ranks(players, depth, source="ourlads")
        by = {p.name: p.depth_rank for p in attached}
        src = {p.name: p.depth_source for p in attached}
        self.assertEqual(by["Patrick Mahomes"], 1)
        self.assertEqual(by["Rashee Rice"], 1)
        self.assertIsNone(by["Walk On"])
        self.assertIsNone(by["Chiefs"])
        self.assertEqual(src["Patrick Mahomes"], "ourlads")
        self.assertIsNone(src["Walk On"])
        self.assertEqual(stats["matched"], 2)
        starter = week1_score(24.0, 1, "QB")
        unlisted = week1_score(24.0, None, "WR")
        self.assertGreater(starter, unlisted)

    def test_jr_join(self):
        parsed = parse_offense_depth(JAX_NAMES)
        depth = [DepthRow("JAC", p, r, n, "u", "t") for p, r, n in parsed]
        players = [_p("Brian Thomas Jr.", team="JAC", pos="WR")]
        attached, _stats = attach_depth_ranks(players, depth)
        self.assertEqual(attached[0].depth_rank, 1)

    def test_depth_index_best_rank(self):
        rows = [
            DepthRow("KC", "WR", 2, "Rashee Rice", "u", "t"),
            DepthRow("KC", "WR", 1, "Rashee Rice", "u", "t"),
        ]
        idx = depth_index(rows)
        self.assertEqual(idx[("KC", match_key("Rashee Rice"))][0], 1)


class CsvRoundtripTest(unittest.TestCase):
    def test_write_load(self):
        parsed = parse_offense_depth(FIXTURE)
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "ourlads"
            cache.mkdir()
            (cache / "KC.html").write_text(FIXTURE, encoding="utf-8")
            dest = Path(td) / "depth.csv"
            import nfl.ourlads as ol

            old_cache = ol.CACHE_DIR
            try:
                ol.CACHE_DIR = cache
                rows = ingest_slate_depth({"KC"}, refresh=False, out_csv=dest)
            finally:
                ol.CACHE_DIR = old_cache
            self.assertTrue(dest.is_file())
            loaded = load_depth_csv(dest)
            self.assertEqual(len(loaded), len(rows))
            self.assertTrue(any(r.name == "Patrick Mahomes" and r.team == "KC" for r in loaded))


class ChokeIdTest(unittest.TestCase):
    def test_line_prefix(self):
        self.assertEqual(line("DEPTH_OURLADS", "x"), "choke DEPTH_OURLADS: x")

    def test_depth_ids(self):
        self.assertEqual(depth_id(UnmappedTeam("ZZZ")), "DEPTH_JOIN")
        self.assertEqual(depth_id(DepthError("index")), "DEPTH_OURLADS")
        self.assertEqual(depth_id(EspnDepthError("403")), "DEPTH_ESPN")
        from nfl.depth import GangstashDepthKeyMissing

        self.assertEqual(depth_id(GangstashDepthKeyMissing("no key")), "DEPTH_GANGSTASH_KEY")


if __name__ == "__main__":
    unittest.main()
