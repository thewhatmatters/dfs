"""FanDuel NFL classic rules lock (contest 133104)."""

from __future__ import annotations

import unittest
from dataclasses import replace

from types import SimpleNamespace

from nfl.rules import (
    DST_SACK_TO_PRIOR,
    FANDUEL_MAX_PER_TEAM,
    FANDUEL_NFL,
    FANDUEL_PICKER_ORDER,
    FanDuelNflClassic,
    HOUSE_CASH_LINE,
    MAX_LINEUPS,
    MIN_UNIQUE_DEFAULT,
    STACK_COEF,
    SkillSide,
    bring_back_illegal,
    bring_back_players,
    dst_pa_key,
    dst_pa_points,
    dst_projection,
    max_per_team_illegal,
    opp_dst_illegal,
    pass_stack_players,
    qb_opp_dst_illegal,
    stack_premium_points,
    stack_qb_illegal,
)


class CapFloorTest(unittest.TestCase):
    def test_cap_and_house_floor(self):
        self.assertEqual(FANDUEL_NFL.salary_cap, 60_000)
        self.assertEqual(FANDUEL_NFL.salary_floor, 58_000)

    def test_house_cash_line_and_upload_cap(self):
        self.assertEqual(HOUSE_CASH_LINE, 150.0)
        self.assertEqual(MAX_LINEUPS, 150)
        self.assertEqual(MIN_UNIQUE_DEFAULT, 2)
        self.assertLessEqual(MAX_LINEUPS, 250)
        self.assertEqual(FANDUEL_NFL.bring_back, 0)
        self.assertTrue(FANDUEL_NFL.require_qb_with_two_pass_catchers)
        self.assertEqual(STACK_COEF, 0.12)
        self.assertEqual(FANDUEL_MAX_PER_TEAM, 4)

    def test_bring_back_cannot_be_negative(self):
        with self.assertRaises(ValueError):
            FanDuelNflClassic(bring_back=-1)

    def test_floor_cannot_exceed_cap(self):
        with self.assertRaises(ValueError):
            FanDuelNflClassic(salary_floor=70_000)

    def test_floor_cannot_be_negative(self):
        with self.assertRaises(ValueError):
            FanDuelNflClassic(salary_floor=-1)

    def test_floor_zero_disables(self):
        rules = FanDuelNflClassic(salary_floor=0)
        self.assertEqual(rules.salary_floor, 0)
        self.assertEqual(rules.salary_cap, 60_000)


class RosterTest(unittest.TestCase):
    def test_nine_slots(self):
        self.assertEqual(len(FANDUEL_NFL.slot_names), 9)
        self.assertEqual(
            FANDUEL_NFL.slot_names,
            ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", "DEF"],
        )

    def test_picker_matches_upload_template(self):
        labels = [label for _key, label in FANDUEL_PICKER_ORDER]
        self.assertEqual(labels, ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DEF"])
        self.assertEqual(len(FANDUEL_PICKER_ORDER), 9)

    def test_flex_is_rb_wr_te_not_qb_or_def(self):
        flex = FANDUEL_NFL.eligible_positions("FLEX")
        self.assertEqual(flex, frozenset({"RB", "WR", "TE"}))
        self.assertNotIn("QB", flex)
        self.assertNotIn("D", flex)
        self.assertNotIn("DEF", flex)

    def test_wr_slots_are_wr_only(self):
        self.assertEqual(FANDUEL_NFL.eligible_positions("WR"), frozenset({"WR"}))
        self.assertEqual(FANDUEL_NFL.eligible_positions("TE"), frozenset({"TE"}))

    def test_def_csv_position_is_d(self):
        self.assertEqual(FANDUEL_NFL.eligible_positions("DEF"), frozenset({"D"}))

    def test_team_limits(self):
        self.assertEqual(FANDUEL_NFL.min_teams, 3)
        self.assertEqual(FANDUEL_NFL.max_per_team, 3)
        self.assertEqual(FANDUEL_MAX_PER_TEAM, 4)

    def test_max_per_team_bounds(self):
        FanDuelNflClassic(max_per_team=1)
        FanDuelNflClassic(max_per_team=4)
        with self.assertRaises(ValueError):
            FanDuelNflClassic(max_per_team=0)
        with self.assertRaises(ValueError):
            FanDuelNflClassic(max_per_team=5)


class ScoringTest(unittest.TestCase):
    def test_yardage_bonus_keys(self):
        s = FANDUEL_NFL.scoring
        self.assertEqual(s["bonus_pass_yd_300"], 3)
        self.assertEqual(s["bonus_rush_yd_100"], 3)
        self.assertEqual(s["bonus_rec_yd_100"], 3)

    def test_dst_pa_21_27_is_zero(self):
        self.assertEqual(FANDUEL_NFL.scoring["dst_pa_21_27"], 0)

    def test_dst_pa_bands_from_lobby(self):
        s = FANDUEL_NFL.scoring
        self.assertEqual(s["dst_pa_0"], 10)
        self.assertEqual(s["dst_pa_1_6"], 7)
        self.assertEqual(s["dst_pa_7_13"], 4)
        self.assertEqual(s["dst_pa_14_20"], 1)
        self.assertEqual(s["dst_pa_28_34"], -1)
        self.assertEqual(s["dst_pa_35_plus"], -4)

    def test_half_ppr_and_skill_rates(self):
        s = FANDUEL_NFL.scoring
        self.assertEqual(s["pass_yd"], 0.04)
        self.assertEqual(s["pass_td"], 4)
        self.assertEqual(s["rush_yd"], 0.1)
        self.assertEqual(s["rush_td"], 6)
        self.assertEqual(s["rec_yd"], 0.1)
        self.assertEqual(s["rec"], 0.5)
        self.assertEqual(s["rec_td"], 6)
        self.assertEqual(s["int"], -1)
        self.assertEqual(s["fum_lost"], -2)

    def test_no_kicker_keys(self):
        keys = FANDUEL_NFL.scoring
        self.assertNotIn("fg", keys)
        self.assertNotIn("xp", keys)
        self.assertFalse(any(k.startswith("kicker") or k.startswith("fg_") for k in keys))


class QbOppDstTest(unittest.TestCase):
    def test_flag_defaults_on(self):
        self.assertTrue(FANDUEL_NFL.forbid_qb_opp_dst)

    def test_cin_qb_tb_dst_illegal(self):
        # 133104 is TB@CIN.
        self.assertTrue(qb_opp_dst_illegal("CIN", "TB", "TB"))
        self.assertTrue(opp_dst_illegal(def_team="TB", qb_opp="TB"))

    def test_cin_qb_cin_dst_legal(self):
        self.assertFalse(qb_opp_dst_illegal("CIN", "TB", "CIN"))

    def test_cin_qb_det_dst_legal(self):
        self.assertFalse(qb_opp_dst_illegal("CIN", "TB", "DET"))

    def test_illustration_was_bal(self):
        self.assertTrue(qb_opp_dst_illegal("CIN", "BAL", "BAL"))

    def test_flag_off_does_not_change_helper(self):
        rules = replace(FANDUEL_NFL, forbid_qb_opp_dst=False)
        self.assertFalse(rules.forbid_qb_opp_dst)
        self.assertTrue(qb_opp_dst_illegal("CIN", "TB", "TB"))


class StudRbOppDstTest(unittest.TestCase):
    def test_flags_and_cut_default(self):
        self.assertTrue(FANDUEL_NFL.forbid_stud_rb_opp_dst)
        self.assertEqual(FANDUEL_NFL.stud_rb_min_salary, 7000)
        self.assertTrue(FANDUEL_NFL.forbid_qb_opp_dst)

    def test_bijan_like_8800_vs_no_dst_illegal(self):
        # Illustration: Bijan vs Saints. 133104 game is ATL@PIT (opp PIT).
        self.assertTrue(opp_dst_illegal(def_team="NO", rbs=[("NO", 8800)]))

    def test_same_rb_5000_legal(self):
        self.assertFalse(opp_dst_illegal(def_team="NO", rbs=[("NO", 5000)]))

    def test_cheap_rb2_4500_legal(self):
        self.assertFalse(opp_dst_illegal(def_team="NO", rbs=[("NO", 4500)]))

    def test_burrow_cin_vs_tb_dst_still_illegal(self):
        self.assertTrue(qb_opp_dst_illegal("CIN", "TB", "TB"))
        self.assertTrue(
            opp_dst_illegal(def_team="TB", qb_opp="TB", rbs=[("NO", 8800)])
        )

    def test_flex_stud_rb_counts(self):
        bijan_flex = SkillSide(position="RB", opp="NO", salary=8800)
        self.assertTrue(opp_dst_illegal(def_team="NO", skill=[bijan_flex]))

    def test_bring_back_wr_does_not_trigger(self):
        wr = SkillSide(position="WR", opp="NO", salary=8500)
        self.assertFalse(opp_dst_illegal(def_team="NO", skill=[wr]))

    def test_stud_rb_same_team_dst_legal(self):
        self.assertFalse(opp_dst_illegal(def_team="ATL", rbs=[("NO", 8800)]))

    def test_stud_rb_other_game_dst_legal(self):
        # Real 133104 Bijan (ATL@PIT) vs NO DST is legal — Saints is not PIT.
        self.assertFalse(opp_dst_illegal(def_team="NO", rbs=[("PIT", 8800)]))

    def test_flag_off_does_not_change_helper(self):
        rules = replace(FANDUEL_NFL, forbid_stud_rb_opp_dst=False)
        self.assertFalse(rules.forbid_stud_rb_opp_dst)
        self.assertTrue(opp_dst_illegal(def_team="NO", rbs=[("NO", 8800)]))


class DstPaMapperTest(unittest.TestCase):
    def test_lobby_bands(self):
        self.assertEqual(dst_pa_key(0), "dst_pa_0")
        self.assertEqual(dst_pa_points(0), 10)
        self.assertEqual(dst_pa_points(6.9), 7)
        self.assertEqual(dst_pa_points(7), 4)
        self.assertEqual(dst_pa_points(13.9), 4)
        self.assertEqual(dst_pa_points(14), 1)
        self.assertEqual(dst_pa_points(20.9), 1)
        self.assertEqual(dst_pa_key(21), "dst_pa_21_27")
        self.assertEqual(dst_pa_points(21), 0)
        self.assertEqual(dst_pa_points(27.9), 0)
        self.assertEqual(dst_pa_points(28), -1)
        self.assertEqual(dst_pa_points(35), -4)

    def test_sack_to_prior_keeps_mid_band_off_stub(self):
        self.assertEqual(DST_SACK_TO_PRIOR, 3.0)
        self.assertEqual(dst_projection(24.5), 3.0)
        self.assertEqual(dst_projection(0), 13.0)
        self.assertEqual(dst_projection(36), -1.0)


def _p(position, team, opponent="", name="X"):
    return SimpleNamespace(
        position=position, team=team, opponent=opponent, name=name, salary=5000
    )


def _burrow_nine(*, chase=True, egbuka=False, cin_te=False, flex="WAS-RB"):
    """CIN QB vs TB. flex: WAS-RB | CIN-WR | TB-WR | CIN-RB | TB-RB."""
    wr1_cin = chase
    slots = {
        "QB": _p("QB", "CIN", "TB", "Joe Burrow"),
        "RB1": _p("RB", "DET", "NO", "R1"),
        "RB2": _p("RB", "PHI", "WAS", "R2"),
        "WR1": _p(
            "WR",
            "CIN" if wr1_cin else "DET",
            "TB" if wr1_cin else "NO",
            "Ja'Marr Chase" if wr1_cin else "W1",
        ),
        "WR2": _p("WR", "DET", "NO", "W2"),
        "WR3": _p("WR", "PHI", "WAS", "W3"),
        "TE": _p(
            "TE",
            "CIN" if cin_te else "DET",
            "TB" if cin_te else "NO",
            "Mike Gesicki" if cin_te else "T1",
        ),
        "DEF": _p("D", "PHI", "WAS", "PHI DST"),
    }
    if flex == "CIN-WR":
        slots["FLEX"] = _p("WR", "CIN", "TB", "Tee Higgins")
    elif flex == "TB-WR":
        slots["FLEX"] = _p("WR", "TB", "CIN", "Emeka Egbuka")
    elif flex == "CIN-RB":
        slots["FLEX"] = _p("RB", "CIN", "TB", "Chase Brown")
    elif flex == "TB-RB":
        slots["FLEX"] = _p("RB", "TB", "CIN", "Bucky Irving")
    else:
        slots["FLEX"] = _p("RB", "WAS", "PHI", "F1")
    if egbuka and flex != "TB-WR":
        slots["WR3"] = _p("WR", "TB", "CIN", "Emeka Egbuka")
    return slots


class BringBackTest(unittest.TestCase):
    def test_default_off(self):
        slots = _burrow_nine(chase=True)
        self.assertTrue(pass_stack_players(slots))
        self.assertFalse(bring_back_illegal(slots, 0))
        self.assertTrue(bring_back_illegal(slots, 1))

    def test_burrow_chase_without_bucs_wr_infeasible_at_n1(self):
        slots = _burrow_nine(chase=True)
        self.assertTrue(bring_back_illegal(slots, 1))
        self.assertFalse(bring_back_illegal(slots, 0))

    def test_burrow_chase_egbuka_feasible_at_n1(self):
        slots = _burrow_nine(chase=True, egbuka=True)
        bbs = bring_back_players(slots)
        self.assertEqual([p.name for p in bbs], ["Emeka Egbuka"])
        self.assertFalse(bring_back_illegal(slots, 1))

    def test_solo_burrow_no_cin_wr_te_feasible_at_n1(self):
        slots = _burrow_nine(chase=False)
        self.assertEqual(pass_stack_players(slots), [])
        self.assertFalse(bring_back_illegal(slots, 1))

    def test_flex_wr_te_counts_as_pass_stack(self):
        no_wr_stack = _burrow_nine(chase=False, flex="CIN-WR")
        self.assertTrue(pass_stack_players(no_wr_stack))
        self.assertTrue(bring_back_illegal(no_wr_stack, 1))
        te_stack = _burrow_nine(chase=False, cin_te=True)
        self.assertTrue(pass_stack_players(te_stack))
        self.assertTrue(bring_back_illegal(te_stack, 1))

    def test_flex_wr_on_opponent_counts_as_bring_back(self):
        slots = _burrow_nine(chase=True, flex="TB-WR")
        self.assertFalse(bring_back_illegal(slots, 1))
        self.assertEqual(bring_back_players(slots)[0].name, "Emeka Egbuka")

    def test_flex_rb_does_not_count(self):
        cin_rb = _burrow_nine(chase=False, flex="CIN-RB")
        self.assertEqual(pass_stack_players(cin_rb), [])
        self.assertFalse(bring_back_illegal(cin_rb, 1))
        tb_rb = _burrow_nine(chase=True, flex="TB-RB")
        self.assertTrue(bring_back_illegal(tb_rb, 1))
        self.assertEqual(bring_back_players(tb_rb), [])

    def test_opp_dst_and_rb_are_not_bring_backs(self):
        slots = _burrow_nine(chase=True)
        slots["DEF"] = _p("D", "TB", "CIN", "TB DST")
        slots["RB2"] = _p("RB", "TB", "CIN", "Bucky Irving")
        self.assertEqual(bring_back_players(slots), [])
        self.assertTrue(bring_back_illegal(slots, 1))


def _lac_four(*, qb="LAC", wr2="LAC", flex="DET-RB"):
    """Herbert + Hampton + McConkey + optional QJ. 4 LAC when wr2 and qb are LAC."""
    slots = {
        "QB": _p("QB", qb, "KC" if qb == "LAC" else "NO", "Justin Herbert" if qb == "LAC" else "Jared Goff"),
        "RB1": _p("RB", "LAC", "KC", "Omarion Hampton"),
        "RB2": _p("RB", "DET", "NO", "Jahmyr Gibbs"),
        "WR1": _p("WR", "LAC", "KC", "Ladd McConkey"),
        "WR2": _p("WR", wr2, "KC" if wr2 == "LAC" else "NO", "Quentin Johnston" if wr2 == "LAC" else "Jamo Williams"),
        "WR3": _p("WR", "DET", "NO", "Amon-Ra St. Brown"),
        "TE": _p("TE", "BAL", "BUF", "Mark Andrews"),
        "DEF": _p("D", "TEN", "NYJ", "TEN"),
    }
    if flex == "LAC-WR":
        slots["FLEX"] = _p("WR", "LAC", "KC", "QJ Flex")
    elif flex == "LAC-RB":
        slots["FLEX"] = _p("RB", "LAC", "KC", "LAC RB2")
    else:
        slots["FLEX"] = _p("RB", "CIN", "TB", "Chase Brown")
    return slots


class HouseMaxPerTeamTest(unittest.TestCase):
    def test_four_lac_illegal_at_house_3(self):
        slots = _lac_four()
        self.assertTrue(max_per_team_illegal(slots, 3))
        self.assertFalse(max_per_team_illegal(slots, 4))

    def test_three_lac_legal_at_house_3(self):
        slots = _lac_four(wr2="DET")
        self.assertFalse(max_per_team_illegal(slots, 3))
        counts = {}
        for p in slots.values():
            counts[p.team] = counts.get(p.team, 0) + 1
        self.assertEqual(counts["LAC"], 3)


class StackQbTest(unittest.TestCase):
    def test_two_lac_wr_without_herbert_illegal(self):
        slots = _lac_four(qb="DET")
        self.assertTrue(stack_qb_illegal(slots, True))
        self.assertFalse(stack_qb_illegal(slots, False))

    def test_two_lac_wr_with_herbert_legal(self):
        slots = _lac_four()
        self.assertFalse(stack_qb_illegal(slots, True))

    def test_one_wr_without_qb_legal(self):
        slots = _lac_four(qb="DET", wr2="DET")
        self.assertFalse(stack_qb_illegal(slots, True))

    def test_two_rbs_without_qb_legal(self):
        slots = _lac_four(qb="DET", wr2="DET", flex="LAC-RB")
        self.assertFalse(stack_qb_illegal(slots, True))
        rbs = [p for p in slots.values() if p.position == "RB" and p.team == "LAC"]
        self.assertGreaterEqual(len(rbs), 2)

    def test_flex_wr_counts_as_second_catcher(self):
        slots = _lac_four(qb="DET", wr2="DET", flex="LAC-WR")
        # WR1 McConkey + FLEX LAC WR, DET QB.
        self.assertTrue(stack_qb_illegal(slots, True))

    def test_incomplete_without_qb_not_illegal_yet(self):
        slots = _lac_four()
        del slots["QB"]
        self.assertFalse(stack_qb_illegal(slots, True))


class StackPremiumTest(unittest.TestCase):
    def test_premium_on_wr_te_not_rb(self):
        wr = SimpleNamespace(position="WR", team="LAC", projection=10.0, name="Ladd McConkey")
        rb = SimpleNamespace(position="RB", team="LAC", projection=12.0, name="Omarion Hampton")
        te = SimpleNamespace(position="TE", team="LAC", projection=8.0, name="TE")
        dst = SimpleNamespace(position="D", team="LAC", projection=5.0, name="LAC DST")
        other = SimpleNamespace(position="WR", team="DET", projection=11.0, name="Jamo")
        slots = {
            "QB": SimpleNamespace(position="QB", team="LAC", projection=20.0, name="Justin Herbert"),
            "WR1": wr,
            "RB1": rb,
            "TE": te,
            "DEF": dst,
            "WR2": other,
        }
        pts = stack_premium_points(slots)
        self.assertAlmostEqual(pts, STACK_COEF * (10.0 + 8.0), places=6)
        self.assertEqual(STACK_COEF, 0.12)


class InjuryCodesTest(unittest.TestCase):
    def test_133104_export_codes(self):
        self.assertEqual(FANDUEL_NFL.out_codes, frozenset({"IR", "NA"}))
        self.assertEqual(FANDUEL_NFL.questionable_codes, frozenset({"Q"}))


if __name__ == "__main__":
    unittest.main()
