"""Per-player notes: starter mark, missing props, run volume, blowout script."""

from __future__ import annotations

import unittest

from ncaaf.explain import (
    explain_player,
    lineup_script_notes,
    picker_name,
    player_note,
)
from ncaaf.players import Player
from ncaaf.solver import Lineup


def _p(
    name: str,
    pos: str,
    *,
    team: str = "TEX",
    opponent: str = "TXST",
    salary: int = 8000,
    depth_rank: int | None = 1,
    implied_total: float | None = 44.6,
    implied_opp: float | None = 15.1,
    spread: float | None = -29.5,
    prop_fd: float | None = None,
    prop_pass_yds: float | None = None,
    prop_pass_tds: float | None = None,
    prop_rush_yds: float | None = None,
    prop_rec_yds: float | None = None,
    prop_receptions: float | None = None,
    prop_book: str | None = None,
    prop_status: str | None = None,
    fppg: float | None = None,
    target_share: float | None = None,
    rush_share: float | None = None,
) -> Player:
    return Player(
        pid=name,
        name=name,
        position=pos,
        salary=salary,
        team=team,
        opponent=opponent,
        game=f"{opponent}@{team}",
        fppg=fppg,
        injury="",
        roster_position="",
        spread=spread,
        implied_total=implied_total,
        implied_opp=implied_opp,
        depth_rank=depth_rank,
        prop_fd=prop_fd,
        prop_pass_yds=prop_pass_yds,
        prop_pass_tds=prop_pass_tds,
        prop_rush_yds=prop_rush_yds,
        prop_rec_yds=prop_rec_yds,
        prop_receptions=prop_receptions,
        prop_book=prop_book,
        prop_status=prop_status,
        target_share=target_share,
        rush_share=rush_share,
    )


class StarterMarkTest(unittest.TestCase):
    def test_rank1_starter_flag_and_bang(self):
        p = _p("Arch Manning", "QB")
        info = explain_player(p)
        self.assertTrue(info["starter"])
        self.assertIn("OurLads starter", info["note"])
        self.assertEqual(picker_name(p.name, info["starter"]), "Arch Manning (*)")
        self.assertNotIn("100% snaps", info["note"].lower())
        self.assertNotIn("snap %", info["note"].lower())

    def test_d2_and_unlisted(self):
        d2 = explain_player(_p("Backup", "RB", depth_rank=2))
        self.assertFalse(d2["starter"])
        self.assertIn("d2", d2["note"].lower())
        self.assertEqual(picker_name("Backup", d2["starter"]), "Backup")
        un = explain_player(_p("Walk On", "WR", depth_rank=None))
        self.assertFalse(un["starter"])
        self.assertIn("OurLads unlisted — tiny prior", un["note"])


class MissingPropsTest(unittest.TestCase):
    def test_note_says_no_player_props(self):
        p = _p(
            "Rico Scott",
            "WR",
            team="BAMA",
            opponent="ECU",
            prop_status="no_market",
        )
        note = player_note(p)
        self.assertIn("no player props", note.lower())
        self.assertIn("0.22 share", note)
        self.assertEqual(explain_player(p)["prop_status"], "no_market")

    def test_usage_in_note(self):
        p = _p("Cooper Perry", "WR", prop_status="no_market", target_share=0.087)
        note = player_note(p)
        self.assertIn("usage pass", note.lower())
        self.assertNotIn("starter prior", note.lower())

    def test_unmatched_labeled(self):
        p = _p("CJ Baxter", "RB", prop_status="unmatched", depth_rank=1)
        note = player_note(p)
        self.assertIn("no player props", note.lower())
        self.assertIn("unmatched", note.lower())
        self.assertEqual(explain_player(p)["prop_status"], "unmatched")


class PropsAndScriptTest(unittest.TestCase):
    def test_qb_props_list_lines_and_book(self):
        qb = _p(
            "Marcel Reed",
            "QB",
            team="TXAM",
            opponent="MOST",
            spread=-40.5,
            implied_total=47.0,
            implied_opp=6.5,
            prop_fd=23.57,
            prop_pass_yds=245.5,
            prop_pass_tds=2.5,
            prop_rush_yds=37.5,
            prop_book="fanduel",
            prop_status="props",
        )
        info = explain_player(qb)
        self.assertEqual(info["prop_status"], "props")
        self.assertIn("Odds props (FanDuel)", info["note"])
        self.assertIn("±20% tilt on implied", info["note"])
        self.assertIn("245.5 pass yds", info["note"])
        self.assertIn("2.5 pass TDs", info["note"])
        self.assertIn("37.5 rush yds", info["note"])
        self.assertTrue(info["starter"])

    def test_rb_rush_vs_pass_mentions_run(self):
        qb = _p(
            "Team QB",
            "QB",
            team="AAA",
            opponent="BBB",
            spread=-3.0,
            implied_total=30.0,
            implied_opp=27.0,
            prop_fd=8.4,
            prop_pass_yds=210,
            prop_book="fanduel",
            prop_status="props",
        )
        rb = _p(
            "Team RB",
            "RB",
            team="AAA",
            opponent="BBB",
            spread=-3.0,
            implied_total=30.0,
            implied_opp=27.0,
            prop_fd=9.05,
            prop_rush_yds=90.5,
            prop_book="fanduel",
            prop_status="props",
        )
        note = player_note(rb, pool=[qb, rb], lineup=[rb])
        self.assertRegex(note.lower(), r"run|rush")
        self.assertIn("90.5", note)
        self.assertIn("210", note)
        self.assertIn("run volume", note.lower())

    def test_blowout_spread_without_scheme(self):
        rb = _p(
            "Blowout RB",
            "RB",
            team="OSU",
            opponent="BALL",
            spread=-28.0,
            implied_total=40.0,
            implied_opp=12.0,
            prop_status="no_market",
        )
        note = player_note(rb)
        self.assertIn("28", note)
        self.assertIn("favorite", note.lower())
        self.assertNotIn("run-heavy", note.lower())
        self.assertNotIn("air raid", note.lower())
        self.assertNotIn("spread offense", note.lower())
        self.assertNotIn("100% snaps", note.lower())
        self.assertIn("featured back recedes", note.lower())

    def test_wr_starter_second_half_sit_and_haircut(self):
        wr = _p(
            "Blowout WR",
            "WR",
            team="BAMA",
            opponent="ECU",
            spread=-28.0,
            implied_total=40.0,
            implied_opp=12.0,
            prop_status="no_market",
        )
        info = explain_player(wr)
        note = info["note"].lower()
        self.assertIn("second-half sit", note)
        self.assertIn("reduced starter usage", note)
        self.assertIn("score haircut", note)
        self.assertLess(info["script_mult"], 1.0)
        self.assertNotIn("run-heavy", note)
        te = explain_player(
            _p(
                "Blowout TE",
                "TE",
                spread=-28.0,
                implied_total=40.0,
                implied_opp=12.0,
                prop_status="no_market",
            )
        )
        self.assertIn("second-half sit", te["note"].lower())
        self.assertLess(te["script_mult"], 1.0)
        d2 = explain_player(
            _p(
                "WR2",
                "WR",
                spread=-28.0,
                implied_total=40.0,
                implied_opp=12.0,
                depth_rank=2,
                prop_status="no_market",
            )
        )
        self.assertIn("second-half sit", d2["note"].lower())
        self.assertNotIn("reduced starter usage", d2["note"].lower())
        self.assertGreater(d2["script_mult"], info["script_mult"])

    def test_dog_wr_throws_does_not_sit(self):
        wr = _p(
            "Dog WR",
            "WR",
            spread=28.0,
            implied_total=40.0,
            implied_opp=12.0,
            prop_status="no_market",
        )
        info = explain_player(wr)
        self.assertIn("throw to keep up", info["note"].lower())
        self.assertNotIn("second-half sit", info["note"].lower())
        self.assertGreaterEqual(info["script_mult"], 1.0)

    def test_props_still_get_script_mult(self):
        wr = _p(
            "Prop WR",
            "WR",
            spread=-50.25,
            implied_total=53.1,
            implied_opp=2.9,
            prop_fd=11.5,
            prop_rec_yds=87.5,
            prop_book="fanduel",
            prop_status="props",
        )
        info = explain_player(wr)
        self.assertLess(info["script_mult"], 1.0)
        self.assertIn("second-half sit", info["note"].lower())
        self.assertIn("score haircut", info["note"].lower())


class LineupJsonTest(unittest.TestCase):
    def test_picker_and_slots_share_note_and_starter(self):
        qb = _p(
            "Arch Manning",
            "QB",
            prop_fd=23.85,
            prop_pass_yds=272.5,
            prop_pass_tds=2.5,
            prop_rush_yds=29.5,
            prop_book="fanduel",
            prop_status="props",
        )
        rb1 = _p("Manny Covey", "RB", team="MEM", prop_status="no_market")
        rb2 = _p("Hollywood Smothers", "RB", prop_status="no_market")
        wr1 = _p("Emmett Mosley V", "WR", prop_status="no_market")
        wr2 = _p(
            "Jeremiah Smith",
            "WR",
            team="OSU",
            opponent="BALL",
            spread=-50.25,
            implied_total=53.125,
            implied_opp=2.875,
            prop_fd=11.5,
            prop_rec_yds=87.5,
            prop_receptions=5.5,
            prop_book="fanduel",
            prop_status="props",
        )
        wr3 = _p("Rico Scott", "WR", team="BAMA", prop_status="no_market")
        sflx = _p(
            "Marcel Reed",
            "QB",
            team="TXAM",
            opponent="MOST",
            spread=-40.5,
            implied_total=47.0,
            implied_opp=6.5,
            prop_fd=23.57,
            prop_pass_yds=245.5,
            prop_book="fanduel",
            prop_status="props",
        )
        lu = Lineup(
            slots={
                "QB": qb,
                "RB1": rb1,
                "RB2": rb2,
                "WR1": wr1,
                "WR2": wr2,
                "WR3": wr3,
                "SUPERFLEX": sflx,
            },
            method="pulp-cbc",
            pool=[qb, rb1, rb2, wr1, wr2, wr3, sflx],
        )
        d = lu.to_dict()
        by_id = {row["id"]: row for row in d["picker"]}
        for key, row in d["slots"].items():
            picker = next(p for p in d["picker"] if p["slot_key"] == key)
            self.assertEqual(row["note"], picker["note"])
            self.assertEqual(row["starter"], picker["starter"])
            self.assertTrue(row["note"])
        self.assertTrue(by_id["Arch Manning"]["starter"])
        self.assertIn("(*)", picker_name("Arch Manning", True))
        self.assertIn("no player props", by_id["Rico Scott"]["note"].lower())
        self.assertLess(by_id["Rico Scott"]["script_mult"], 1.0)
        self.assertLess(by_id["Jeremiah Smith"]["script_mult"], 1.0)
        self.assertTrue(any("huge implied" in n for n in d["notes"]))


class LineupScriptNotesTest(unittest.TestCase):
    def test_blowout_with_rush_prop(self):
        wr = _p(
            "Jeremiah Smith",
            "WR",
            team="OSU",
            opponent="BALL",
            spread=-50.25,
            implied_total=53.1,
            implied_opp=2.9,
        )
        rb = _p(
            "Bo Jackson",
            "RB",
            team="OSU",
            opponent="BALL",
            spread=-50.25,
            prop_fd=9.0,
            prop_rush_yds=90.5,
            prop_book="fanduel",
        )
        notes = lineup_script_notes([wr], pool=[wr, rb])
        self.assertEqual(len(notes), 1)
        self.assertIn("OSU", notes[0])
        self.assertIn("huge implied", notes[0])
        self.assertIn("Jackson", notes[0])

    def test_generational_suffix_not_used_as_last(self):
        rb = _p(
            "Rueben Owens II",
            "RB",
            team="TXAM",
            opponent="MOST",
            spread=-40.5,
            implied_total=47.0,
            implied_opp=6.5,
            prop_fd=5.0,
            prop_rush_yds=50.5,
            prop_book="fanduel",
        )
        notes = lineup_script_notes([rb], pool=[rb])
        self.assertEqual(len(notes), 1)
        self.assertIn("Owens", notes[0])
        self.assertNotRegex(notes[0], r"\bII\b")


if __name__ == "__main__":
    unittest.main()
