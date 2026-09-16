"""Interview parsing, exclusive vs lean, empty-input defaults."""

from __future__ import annotations

import io
import unittest
from dataclasses import replace

from ncaaf.interview import (
    LEAN_MULT,
    InterviewAnswers,
    apply_interview,
    parse_answers,
    parse_cut_choice,
    parse_games_choice,
    parse_lock_fade,
    parse_sit_choice,
    parse_superflex_choice,
    run_interview,
)
from ncaaf.lines import TeamLine
from ncaaf.optimize import parse_args
from ncaaf.players import Player
from ncaaf.projections import week1_score
from ncaaf.rules import FANDUEL_NCAAF
from ncaaf.solver import solve_ilp


def _game(game, away, home, imp_a, imp_h, spread_h, total) -> TeamLine:
    return TeamLine(
        game=game,
        home_fd=home,
        away_fd=away,
        home_spread=spread_h,
        total=total,
        implied_home=imp_h,
        implied_away=imp_a,
        home_moneyline=None,
        away_moneyline=None,
        provider="t",
        source="t",
    )


def _p(pid, pos, team, salary=5000, obj=10.0, **kw) -> Player:
    return Player(
        pid=pid,
        name=kw.pop("name", pid),
        position=pos,
        salary=salary,
        team=team,
        opponent=kw.pop("opponent", "X"),
        game=kw.pop("game", "A@B"),
        fppg=None,
        injury="",
        roster_position="",
        objective=obj,
        **kw,
    )


GAMES = [
    _game("BALL@OSU", "BALL", "OSU", 2.5, 53.0, -50.5, 55.5),
    _game("BC@CIN", "BC", "CIN", 22.25, 29.25, -7.0, 51.5),
    _game("TULN@DUKE", "TULN", "DUKE", 21.0, 30.5, -9.5, 51.5),
]


class ParseInterviewTest(unittest.TestCase):
    def test_empty_input_is_recommend(self):
        a = parse_answers(numbered=GAMES)
        self.assertEqual(a.games_mode, "all")
        self.assertFalse(a.exclusive)
        self.assertTrue(a.apply_sit)
        self.assertEqual(a.superflex, "qb")
        self.assertEqual(a.lock_names, ())
        self.assertEqual(a.fade_names, ())
        self.assertEqual(parse_cut_choice(""), False)
        self.assertEqual(parse_sit_choice(""), True)
        self.assertEqual(parse_superflex_choice(""), "qb")
        self.assertEqual(parse_lock_fade(""), ((), ()))
        self.assertEqual(parse_lock_fade("none"), ((), ()))
        mode, selected = parse_games_choice("", GAMES)
        self.assertEqual(mode, "all")
        self.assertEqual(len(selected), 3)

    def test_top_implied_either_side_cutoff(self):
        mode, selected = parse_games_choice("top", GAMES)
        self.assertEqual(mode, "top")
        self.assertEqual(set(selected), {"BALL@OSU", "TULN@DUKE"})
        self.assertNotIn("BC@CIN", selected)

    def test_top_n_and_pick_numbers(self):
        # numbered_games sorts by max implied: OSU 53, DUKE 30.5, CIN 29.25
        from ncaaf.interview import numbered_games

        numbered = numbered_games(GAMES)
        mode, selected = parse_games_choice("top 1", numbered)
        self.assertEqual(mode, "top")
        self.assertEqual(selected, ("BALL@OSU",))
        mode, selected = parse_games_choice("1,3", numbered)
        self.assertEqual(mode, "pick")
        self.assertEqual(selected, (numbered[0].game, numbered[2].game))

    def test_lock_fade_forms(self):
        self.assertEqual(
            parse_lock_fade("lock Jeremiah Smith fade Emmett Mosley"),
            (("Jeremiah Smith",), ("Emmett Mosley",)),
        )
        self.assertEqual(parse_lock_fade("Nate Sheppard"), (("Nate Sheppard",), ()))
        self.assertEqual(parse_lock_fade("fade Rico Scott"), ((), ("Rico Scott",)))

    def test_parse_args_interview_defaults(self):
        ns = parse_args(["--csv", "x.csv", "--interview-defaults"])
        self.assertTrue(ns.interview_defaults)
        self.assertFalse(ns.agent)


class ApplyInterviewTest(unittest.TestCase):
    def test_exclusive_drops_other_games_lean_keeps_and_bumps(self):
        games = GAMES
        pool = [
            _p("osu_wr", "WR", "OSU", obj=10.0, game="BALL@OSU"),
            _p("cin_wr", "WR", "CIN", obj=10.0, game="BC@CIN"),
        ]
        exclusive = InterviewAnswers(
            games_mode="pick",
            selected_games=("BALL@OSU",),
            exclusive=True,
            apply_sit=True,
            superflex="qb",
            lock_names=(),
            fade_names=(),
        )
        out, locks = apply_interview(pool, games, exclusive)
        self.assertEqual({p.team for p in out}, {"OSU"})
        self.assertEqual(locks, frozenset())

        lean = replace(exclusive, exclusive=False)
        out, _ = apply_interview(pool, games, lean)
        self.assertEqual(len(out), 2)
        by = {p.pid: p for p in out}
        self.assertAlmostEqual(by["osu_wr"].objective, 10.0 * LEAN_MULT)
        self.assertAlmostEqual(by["cin_wr"].objective, 10.0)

    def test_ignore_sit_forces_script_mult_one(self):
        pl = _p(
            "wr1",
            "WR",
            "OSU",
            salary=8000,
            obj=99.0,
            implied_total=40.0,
            spread=-28.0,
            depth_rank=1,
        )
        answers = InterviewAnswers(
            games_mode="all",
            selected_games=(),
            exclusive=False,
            apply_sit=False,
            superflex="qb",
            lock_names=(),
            fade_names=(),
        )
        out, _ = apply_interview([pl], GAMES, answers)
        want = week1_score(
            40.0, 8000, depth_rank=1, position="WR", team_spread=-28.0, apply_script=False
        )
        self.assertAlmostEqual(out[0].objective, want)
        self.assertFalse(out[0].script_applied)
        with_script = week1_score(
            40.0, 8000, depth_rank=1, position="WR", team_spread=-28.0
        )
        self.assertGreater(out[0].objective, with_script)

    def test_fade_and_lock(self):
        pool = [
            _p("qb_cheap", "QB", "T1", 4000, obj=8, name="Cheap Qb"),
            _p("qb_dear", "QB", "T1", 12000, obj=9, name="Dear Qb"),
            _p("rb1", "RB", "T2", 4000, name="Rb One"),
            _p("rb2", "RB", "T2", 4000, name="Rb Two"),
            _p("wr1", "WR", "T3", 4000, name="Wr One"),
            _p("wr2", "WR", "T3", 4000, name="Wr Two"),
            _p("wr3", "WR", "T3", 4000, name="Wr Three"),
            _p("wr4", "WR", "T1", 4000, name="Wr Four"),
        ]
        answers = InterviewAnswers(
            games_mode="all",
            selected_games=(),
            exclusive=False,
            apply_sit=True,
            superflex="qb",
            lock_names=("Dear Qb",),
            fade_names=("Wr Four",),
        )
        out, lock_ids = apply_interview(pool, GAMES, answers)
        self.assertNotIn("wr4", {p.pid for p in out})
        self.assertEqual(lock_ids, frozenset({"qb_dear"}))
        rules = replace(FANDUEL_NCAAF, salary_floor=0)
        lu = solve_ilp(out, rules, lock_ids=lock_ids)
        self.assertIn("qb_dear", {p.pid for p in lu.slots.values()})


class RunInterviewTest(unittest.TestCase):
    def test_empty_lines_accept_recommends(self):
        infile = io.StringIO("\n\n\n\n")
        outfile = io.StringIO()
        ans = run_interview(GAMES, use_defaults=False, infile=infile, outfile=outfile)
        self.assertEqual(ans.games_mode, "all")
        self.assertTrue(ans.apply_sit)
        self.assertEqual(ans.superflex, "qb")
        self.assertEqual(ans.lock_names, ())
        self.assertIn("Q1", outfile.getvalue())
        self.assertNotIn("Q2", outfile.getvalue())

    def test_use_defaults_does_not_read_stdin(self):
        infile = io.StringIO("top\nexclusive\nignore\nany\nlock Smith\n")
        outfile = io.StringIO()
        ans = run_interview(GAMES, use_defaults=True, infile=infile, outfile=outfile)
        self.assertEqual(ans, parse_answers(numbered=GAMES))
        self.assertIn("Q1 games: all", outfile.getvalue())
        self.assertEqual(infile.tell(), 0)


if __name__ == "__main__":
    unittest.main()
