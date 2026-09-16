"""Named choke ids for ingest failures."""

from __future__ import annotations

import unittest

from ncaaf.choke import depth_id, line, lines_id, mix_id, props_id
from ncaaf.lines import LinesError, LinesKeyMissing
from ncaaf.mix import MixError
from ncaaf.ourlads import DepthError
from ncaaf.props import PropsError, PropsKeyMissing
from ncaaf.teams import UnmappedTeam


class ChokeIdTest(unittest.TestCase):
    def test_line_prefix(self):
        self.assertEqual(line("LINES_KEY", "no key"), "choke LINES_KEY: no key")

    def test_lines_key(self):
        self.assertEqual(lines_id(LinesKeyMissing("x")), "LINES_KEY")

    def test_lines_cfbd_vs_odds(self):
        self.assertEqual(lines_id(LinesError("CFBD /lines 500")), "LINES_CFBD")
        self.assertEqual(lines_id(LinesError("Odds API timeout")), "LINES_ODDS")

    def test_join(self):
        self.assertEqual(lines_id(UnmappedTeam("ZZZ")), "LINES_JOIN")
        self.assertEqual(depth_id(UnmappedTeam("ZZZ")), "DEPTH_JOIN")

    def test_depth_and_props(self):
        self.assertEqual(depth_id(DepthError("index")), "DEPTH_OURLADS")
        self.assertEqual(props_id(PropsKeyMissing("no")), "PROPS_ODDS_KEY")
        self.assertEqual(props_id(PropsError("http")), "PROPS_ODDS")
        self.assertEqual(mix_id(MixError("CFBD mix 500")), "MIX_CFBD")
        self.assertEqual(mix_id(MixError("HTTP 401 key rejected")), "MIX_AUTH")


if __name__ == "__main__":
    unittest.main()
