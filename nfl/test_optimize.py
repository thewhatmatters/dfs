"""NFL week-1 score, DST PA, and opp-DST ILP on a tiny fake pool."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from dataclasses import replace

from nfl.lines import filter_commence, implied_totals, parse_odds_games, slate_window
from nfl.names import match_key
from nfl.explain import bring_back_note, explain_player, lineup_notes
from nfl.optimize import _attach_totals, _print_picker, qb_wr_stack
from nfl.players import Player, filter_pool
from nfl.projections import (
    POS_FD_SHARE,
    PROP_FACTOR_HI,
    PROP_FACTOR_LO,
    score_player,
    week1_score,
)
from nfl.props import PlayerProp
from nfl.rules import (
    FANDUEL_NFL,
    STACK_COEF,
    dst_projection,
    opp_dst_illegal,
    pass_stack_players,
    stack_qb_illegal,
)
from nfl.sim import SimStats
from nfl.solver import Infeasible, Lineup, lineup_pids, solve_greedy, solve_ilp, solve_ilp_many
from nfl.teams import lookup_odds, require_mapped


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("pid", kw.get("name", "p")),
        name=kw.get("name", "Cam"),
        position="WR",
        salary=5000,
        team="DET",
        opponent="NO",
        game="NO@DET",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=24.0,
        implied_opp=22.0,
        objective=kw.get("objective"),
    )
    fields.update(kw)
    if fields.get("objective") is None:
        fields["objective"] = week1_score(
            fields.get("implied_total") or 0.0,
            depth_rank=fields.get("depth_rank"),
            position=fields["position"],
            prop_fd=fields.get("prop_fd"),
            implied_opp=fields.get("implied_opp"),
            target_share=fields.get("target_share"),
        )
    allowed = Player.__dataclass_fields__
    return Player(**{k: v for k, v in fields.items() if k in allowed})


class Week1ScoreTest(unittest.TestCase):
    def test_model_uses_nfl_shares(self):
        self.assertEqual(POS_FD_SHARE["QB"], 0.50)
        self.assertEqual(POS_FD_SHARE["RB"], 0.28)
        self.assertEqual(POS_FD_SHARE["WR"], 0.18)
        self.assertEqual(POS_FD_SHARE["TE"], 0.12)
        self.assertAlmostEqual(week1_score(30.0, 1, "WR"), 30.0 * 1.0 * 0.18)
        self.assertAlmostEqual(week1_score(30.0, None, "RB"), 30.0 * 0.05 * 0.28)

    def test_prop_is_clamped_tilt_not_override(self):
        core = week1_score(40.0, 1, "QB")
        hi = week1_score(40.0, 1, "QB", prop_fd=100.0)
        lo = week1_score(40.0, 1, "QB", prop_fd=0.1)
        self.assertAlmostEqual(core, 40.0 * 1.0 * 0.50)
        self.assertAlmostEqual(hi, core * PROP_FACTOR_HI, places=6)
        self.assertAlmostEqual(lo, core * PROP_FACTOR_LO, places=6)
        mid = week1_score(40.0, 1, "QB", prop_fd=22.5)
        self.assertAlmostEqual(mid, 22.5, places=6)
        self.assertLess(mid, core * PROP_FACTOR_HI)
        self.assertGreater(mid, core * PROP_FACTOR_LO)

    def test_target_share_is_usage_tilt_not_override(self):
        from nfl.projections import USAGE_FACTOR_HI, USAGE_FACTOR_LO

        base = week1_score(30.0, 1, "WR")
        self.assertAlmostEqual(base, 30.0 * 1.0 * 0.18)
        even = week1_score(30.0, 1, "WR", target_share=0.24)
        self.assertAlmostEqual(even, base)
        hi = week1_score(30.0, 1, "WR", target_share=0.48)
        lo = week1_score(30.0, 1, "WR", target_share=0.01)
        self.assertAlmostEqual(hi, base * USAGE_FACTOR_HI, places=6)
        self.assertAlmostEqual(lo, base * USAGE_FACTOR_LO, places=6)
        qb = week1_score(30.0, 1, "QB", target_share=0.50)
        self.assertAlmostEqual(qb, 30.0 * 1.0 * 0.50)

    def test_props_note_says_tilt(self):
        pl = _pl(
            name="Jared Goff",
            position="QB",
            depth_rank=1,
            prop_fd=18.5,
            prop_pass_yds=250.5,
            prop_book="fanduel",
        )
        info = explain_player(pl)
        self.assertEqual(info["prop_status"], "props")
        self.assertIn("±20% tilt on implied", info["note"])
        self.assertIn("250.5 pass yds", info["note"])

    def test_def_uses_pa_plus_prior(self):
        self.assertAlmostEqual(week1_score(0.0, None, "D", implied_opp=24.5), 3.0)
        self.assertAlmostEqual(week1_score(0.0, None, "DEF", implied_opp=24.5), dst_projection(24.5))


class PropBonusTest(unittest.TestCase):
    def test_bonuses_when_line_meets_threshold(self):
        over = PlayerProp(name="x", pass_yds=300, rush_yds=100, rec_yds=100, receptions=5)
        under = PlayerProp(name="x", pass_yds=299.5, rush_yds=99.5, rec_yds=99.5, receptions=5)
        sc = FANDUEL_NFL.scoring
        want_over = (
            300 * sc["pass_yd"]
            + 100 * sc["rush_yd"]
            + 100 * sc["rec_yd"]
            + 5 * sc["rec"]
            + sc["bonus_pass_yd_300"]
            + sc["bonus_rush_yd_100"]
            + sc["bonus_rec_yd_100"]
        )
        want_under = 299.5 * sc["pass_yd"] + 99.5 * sc["rush_yd"] + 99.5 * sc["rec_yd"] + 5 * sc["rec"]
        self.assertAlmostEqual(over.fd_points(), want_over)
        self.assertAlmostEqual(under.fd_points(), want_under)
        self.assertGreater(over.fd_points() - under.fd_points(), 8.0)


class NameJoinTest(unittest.TestCase):
    def test_jr_strip(self):
        self.assertEqual(match_key("Erick All Jr."), match_key("Erick All"))
        self.assertEqual(match_key("Marvin Harrison Jr."), "marvin harrison")

    def test_jac_jax_was_wsh(self):
        self.assertEqual(lookup_odds("Jacksonville Jaguars").fd, "JAC")
        self.assertEqual(lookup_odds("JAX").fd, "JAC")
        self.assertEqual(lookup_odds("Washington Commanders").fd, "WAS")
        self.assertEqual(lookup_odds("WSH").fd, "WAS")
        require_mapped({"JAC", "WAS", "LAC"})


class FilterPoolTest(unittest.TestCase):
    def test_keeps_d_and_q_drops_ir_na(self):
        pool = filter_pool(
            [
                _pl(name="QB", position="QB", injury=""),
                _pl(name="QWR", position="WR", injury="Q"),
                _pl(name="IR", position="RB", injury="IR"),
                _pl(name="NA", position="TE", injury="NA"),
                _pl(name="DST", position="D", team="PHI", opponent="WAS", game="WAS@PHI"),
                _pl(name="K", position="K"),
            ]
        )
        names = {p.name for p in pool}
        self.assertEqual(names, {"QB", "QWR", "DST"})


class CommenceFilterTest(unittest.TestCase):
    def test_drops_season_duplicate_outside_weekend(self):
        start, end = slate_window(__import__("datetime").date(2026, 9, 13))
        payload = [
            {
                "home_team": "Los Angeles Chargers",
                "away_team": "Arizona Cardinals",
                "commence_time": "2026-09-13T20:00:00Z",
                "bookmakers": [],
            },
            {
                "home_team": "Los Angeles Chargers",
                "away_team": "Arizona Cardinals",
                "commence_time": "2026-12-20T18:00:00Z",
                "bookmakers": [],
            },
        ]
        kept = filter_commence(payload, start, end)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["commence_time"], "2026-09-13T20:00:00Z")

    def test_implied_totals_match_cfb_formula(self):
        home, away = implied_totals(48.5, -3.5)
        self.assertAlmostEqual(home, 26.0)
        self.assertAlmostEqual(away, 22.5)


class OppDstIlpTest(unittest.TestCase):
    def _pool(self) -> list[Player]:
        """One QB (CIN vs TB), one stud RB (ATL vs PIT), juiced illegal DSTs."""
        rows = [
            _pl(
                name="Burrow",
                position="QB",
                team="CIN",
                opponent="TB",
                game="TB@CIN",
                salary=8000,
                objective=25.0,
            ),
            _pl(
                name="Bijan",
                position="RB",
                team="ATL",
                opponent="PIT",
                game="ATL@PIT",
                salary=8800,
                objective=22.0,
            ),
            _pl(
                name="RB2",
                position="RB",
                team="DET",
                opponent="NO",
                game="NO@DET",
                salary=4500,
                objective=8.0,
            ),
            _pl(
                name="RB3",
                position="RB",
                team="PHI",
                opponent="WAS",
                game="WAS@PHI",
                salary=4500,
                objective=7.0,
            ),
            _pl(
                name="WR1",
                position="WR",
                team="DET",
                opponent="NO",
                game="NO@DET",
                salary=6000,
                objective=12.0,
            ),
            _pl(
                name="WR2",
                position="WR",
                team="PHI",
                opponent="WAS",
                game="WAS@PHI",
                salary=5500,
                objective=11.0,
            ),
            _pl(
                name="WR3",
                position="WR",
                team="NO",
                opponent="DET",
                game="NO@DET",
                salary=5000,
                objective=10.0,
            ),
            _pl(
                name="TE1",
                position="TE",
                team="DET",
                opponent="NO",
                game="NO@DET",
                salary=5000,
                objective=9.0,
            ),
            _pl(
                name="WR4",
                position="WR",
                team="CIN",
                opponent="TB",
                game="TB@CIN",
                salary=6500,
                objective=14.0,
            ),
            _pl(
                name="TB DST",
                position="D",
                team="TB",
                opponent="CIN",
                game="TB@CIN",
                salary=3000,
                objective=80.0,
            ),
            _pl(
                name="PIT DST",
                position="D",
                team="PIT",
                opponent="ATL",
                game="ATL@PIT",
                salary=3000,
                objective=5.0,
            ),
            _pl(
                name="PHI DST",
                position="D",
                team="PHI",
                opponent="WAS",
                game="WAS@PHI",
                salary=3000,
                objective=5.0,
            ),
        ]
        return rows

    def test_ilp_avoids_qb_and_stud_rb_opp_dst(self):
        rules = replace(FANDUEL_NFL, salary_floor=0)
        lu = solve_ilp(self._pool(), rules)
        dst = lu.slots["DEF"]
        qb = lu.slots["QB"]
        self.assertEqual(qb.name, "Burrow")
        self.assertEqual(dst.name, "PHI DST")
        self.assertNotEqual(dst.team, qb.opponent)
        skill = [
            (p.opponent, p.salary)
            for p in lu.slots.values()
            if p.position == "RB"
        ]
        self.assertFalse(
            opp_dst_illegal(
                def_team=dst.team,
                qb_opp=qb.opponent,
                rbs=skill,
                stud_rb_min_salary=rules.stud_rb_min_salary,
            )
        )
        self.assertGreaterEqual(lu.salary, 0)
        self.assertLessEqual(lu.salary, rules.salary_cap)
        self.assertGreaterEqual(len(lu.teams), rules.min_teams)
        self.assertTrue(all(n <= rules.max_per_team for n in lu.teams.values()))

    def test_odds_parse_requires_window_not_week18(self):
        from datetime import date as date_cls
        from datetime import datetime, timezone

        start, end = slate_window(date_cls(2026, 9, 13))
        slate = [("ARI@LAC", "ARI", "LAC")]
        payload = [
            {
                "id": "week18",
                "home_team": "Los Angeles Chargers",
                "away_team": "Arizona Cardinals",
                "commence_time": "2026-12-20T18:00:00Z",
                "bookmakers": [
                    {
                        "key": "fanduel",
                        "markets": [
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {"name": "Los Angeles Chargers", "point": -3.5},
                                    {"name": "Arizona Cardinals", "point": 3.5},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [{"name": "Over", "point": 48.5}],
                            },
                        ],
                    }
                ],
            },
            {
                "id": "week1",
                "home_team": "Los Angeles Chargers",
                "away_team": "Arizona Cardinals",
                "commence_time": "2026-09-13T20:05:00Z",
                "bookmakers": [
                    {
                        "key": "fanduel",
                        "markets": [
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {"name": "Los Angeles Chargers", "point": -3.0},
                                    {"name": "Arizona Cardinals", "point": 3.0},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [{"name": "Over", "point": 44.5}],
                            },
                        ],
                    }
                ],
            },
        ]
        by = parse_odds_games(payload, slate, start=start, end=end)
        self.assertAlmostEqual(by["LAC"].total, 44.5)
        self.assertAlmostEqual(by["LAC"].home_spread, -3.0)
        self.assertTrue(start <= datetime(2026, 9, 13, 20, 5, tzinfo=timezone.utc) < end)


def _wide_pool() -> list[Player]:
    """Cheap legal 9s with extra skill so CBC can emit several unique sets."""
    rows: list[Player] = []

    def add(name, pos, team, opp, game, salary, obj, pid=None):
        rows.append(
            _pl(
                pid=pid or name,
                name=name,
                position=pos,
                team=team,
                opponent=opp,
                game=game,
                salary=salary,
                objective=obj,
                implied_total=24.0,
                implied_opp=22.0,
            )
        )

    add("QB-DET", "QB", "DET", "NO", "NO@DET", 6000, 25.0)
    add("QB-PHI", "QB", "PHI", "WAS", "WAS@PHI", 6000, 24.0)
    add("QB-CIN", "QB", "CIN", "TB", "TB@CIN", 6000, 23.0)
    add("QB-ATL", "QB", "ATL", "PIT", "ATL@PIT", 6000, 22.0)
    for i, (team, opp, game) in enumerate(
        [
            ("DET", "NO", "NO@DET"),
            ("PHI", "WAS", "WAS@PHI"),
            ("CIN", "TB", "TB@CIN"),
            ("ATL", "PIT", "ATL@PIT"),
            ("NO", "DET", "NO@DET"),
            ("WAS", "PHI", "WAS@PHI"),
            ("GB", "MIN", "GB@MIN"),
            ("TEN", "NYJ", "NYJ@TEN"),
        ]
    ):
        add(f"RB{i}", "RB", team, opp, game, 5000, 12.0 - i * 0.3)
    for i, (team, opp, game) in enumerate(
        [
            ("DET", "NO", "NO@DET"),
            ("DET", "NO", "NO@DET"),
            ("PHI", "WAS", "WAS@PHI"),
            ("CIN", "TB", "TB@CIN"),
            ("CIN", "TB", "TB@CIN"),
            ("ATL", "PIT", "ATL@PIT"),
            ("NO", "DET", "NO@DET"),
            ("WAS", "PHI", "WAS@PHI"),
            ("GB", "MIN", "GB@MIN"),
            ("TEN", "NYJ", "NYJ@TEN"),
            ("MIN", "GB", "GB@MIN"),
            ("NYJ", "TEN", "NYJ@TEN"),
        ]
    ):
        add(f"WR{i}", "WR", team, opp, game, 5000, 11.0 - i * 0.2)
    add("TE0", "TE", "DET", "NO", "NO@DET", 5000, 9.0)
    add("TE1", "TE", "PHI", "WAS", "WAS@PHI", 5000, 8.5)
    add("TE2", "TE", "CIN", "TB", "TB@CIN", 5000, 8.0)
    add("TE3", "TE", "ATL", "PIT", "ATL@PIT", 5000, 7.5)
    add("DST-GB", "D", "GB", "MIN", "GB@MIN", 3000, 6.0)
    add("DST-TEN", "D", "TEN", "NYJ", "NYJ@TEN", 3000, 5.5)
    add("DST-MIN", "D", "MIN", "GB", "GB@MIN", 3000, 5.0)
    add("DST-NYJ", "D", "NYJ", "TEN", "NYJ@TEN", 3000, 4.5)
    return rows


class LineupTotalsTest(unittest.TestCase):
    def test_proj_is_week1_not_ilp_obj(self):
        players = []
        sim = {}
        slots = {}
        keys = ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", "DEF"]
        pos = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "RB", "D"]
        for i, (k, p) in enumerate(zip(keys, pos)):
            pl = _pl(
                pid=f"p{i}",
                name=f"P{i}",
                position=p,
                team="DET" if i < 4 else "PHI",
                opponent="NO" if i < 4 else "WAS",
                salary=5000,
                prop_fd=10.0,
                objective=20.0,  # ceiling ILP obj; week1 is implied×share×tilt
            )
            players.append(pl)
            slots[k] = pl
            sim[pl.pid] = SimStats(
                mean=10.0, p10=8.0, p50=10.0, p90=14.0, n=10, source="props"
            )
        lu_obj = Lineup(slots=slots, method="test", salary_floor=0)
        lu = lu_obj.to_dict()
        _attach_totals(lu, lu_obj, sim, cash_line=150.0)
        want_proj = sum(score_player(p) for p in players)
        self.assertAlmostEqual(lu["lineup_proj"], want_proj, places=4)
        self.assertAlmostEqual(lu["lineup_floor"], 72.0, places=4)
        self.assertAlmostEqual(lu["lineup_ceiling"], 126.0, places=4)
        self.assertEqual(lu["cash_line"], 150.0)
        self.assertAlmostEqual(lu["ceiling_minus_cash"], -24.0, places=4)
        self.assertLess(lu["lineup_ceiling"], 150.0)
        self.assertNotAlmostEqual(lu["lineup_proj"], lu["projection"])


class NLineupsTest(unittest.TestCase):
    def test_unique_sets_min_unique_and_legal(self):
        rules = replace(FANDUEL_NFL, salary_floor=0)
        pool = _wide_pool()
        one = solve_ilp(pool, rules)
        many = solve_ilp_many(pool, rules, n_lineups=5, min_unique=2)
        self.assertEqual(lineup_pids(many[0]), lineup_pids(one))
        self.assertGreaterEqual(len(many), 3)
        sets = [lineup_pids(lu) for lu in many]
        self.assertEqual(len(sets), len(set(sets)))
        for prev, cur in zip(sets, sets[1:]):
            self.assertGreaterEqual(len(cur - prev), 2)
            self.assertLessEqual(len(cur & prev), 7)
        for lu in many:
            self.assertEqual(len(lu.slots), 9)
            self.assertLessEqual(lu.salary, rules.salary_cap)
            self.assertGreaterEqual(len(lu.teams), rules.min_teams)
            self.assertTrue(all(n <= rules.max_per_team for n in lu.teams.values()))
            dst = lu.slots["DEF"]
            qb = lu.slots["QB"]
            self.assertFalse(
                opp_dst_illegal(
                    def_team=dst.team,
                    qb_opp=qb.opponent,
                    rbs=[
                        (p.opponent, p.salary)
                        for p in lu.slots.values()
                        if p.position == "RB"
                    ],
                )
            )

    def test_infeasible_later_returns_what_we_got(self):
        rules = replace(FANDUEL_NFL, salary_floor=0)
        pool = self._tiny_two()
        got = solve_ilp_many(pool, rules, n_lineups=20, min_unique=2)
        self.assertGreaterEqual(len(got), 1)
        self.assertLess(len(got), 20)

    def _tiny_two(self) -> list[Player]:
        """Barely more than one legal 9 — CBC should stop before 20."""
        return OppDstIlpTest()._pool()

    def test_qb_wr_stack_label(self):
        slots = {}
        slots["QB"] = _pl(name="Joe Burrow", position="QB", team="CIN", opponent="TB")
        slots["RB1"] = _pl(name="R1", position="RB", team="DET", pid="r1")
        slots["RB2"] = _pl(name="R2", position="RB", team="PHI", pid="r2")
        slots["WR1"] = _pl(name="Ja'Marr Chase", position="WR", team="CIN", pid="w1")
        slots["WR2"] = _pl(name="W2", position="WR", team="DET", pid="w2")
        slots["WR3"] = _pl(name="W3", position="WR", team="PHI", pid="w3")
        slots["TE"] = _pl(name="T", position="TE", team="ATL", pid="t")
        slots["FLEX"] = _pl(name="Tee Higgins", position="WR", team="CIN", pid="w4")
        slots["DEF"] = _pl(name="Titans", position="D", team="TEN", pid="d")
        lu = Lineup(slots=slots, method="test")
        label = qb_wr_stack(lu)
        self.assertTrue(label.startswith("CIN "))
        self.assertIn("Ja'Marr Chase", label)
        self.assertIn("Tee Higgins", label)

    def test_does_not_import_ncaaf(self):
        import nfl.optimize
        import nfl.solver
        import nfl.upload

        for mod in (nfl.optimize, nfl.solver, nfl.upload):
            self.assertNotIn("ncaaf", mod.__dict__)


class PickerPrintTest(unittest.TestCase):
    def test_picker_printer_includes_slot_and_proj(self):
        lu = {
            "picker": [
                {
                    "slot": "QB",
                    "name": "Joe Burrow",
                    "position": "QB",
                    "team": "CIN",
                    "salary": 8200,
                    "projection": 21.4,
                    "floor": 17.2,
                    "ceiling": 28.6,
                    "starter": True,
                    "note": "Odds props (FanDuel): 267.5 pass yds, 2.5 pass TDs.",
                    "prop_pass_yds": 267.5,
                    "prop_pass_tds": 2.5,
                }
            ],
            "salary": 59900,
            "lineup_proj": 93.3,
            "lineup_floor": 72.9,
            "lineup_ceiling": 129.3,
            "cash_line": 150.0,
            "ceiling_minus_cash": -20.7,
            "notes": ["stack CIN QB+WR/TE (Chase)"],
        }
        buf = io.StringIO()
        with redirect_stderr(buf):
            _print_picker(lu)
        text = buf.getvalue()
        self.assertTrue("┌" in text or "+" in text)
        self.assertIn("Slot", text)
        self.assertIn("Proj", text)
        self.assertIn("Props", text)
        self.assertIn("Joe Burrow (*)", text)
        self.assertIn("$8,200", text)
        self.assertIn("267.5 pass / 2.5 TD", text)
        self.assertIn("Lineup", text)
        self.assertIn("cash 150", text)
        self.assertNotIn("Odds props", text)


def _bb_pool(*, chase=True, egbuka=False) -> list[Player]:
    """Tiny legal 9-ish pool: Burrow CIN vs TB. Floor 0 in tests."""
    rows: list[Player] = []

    def add(name, pos, team, opp, game, salary, obj):
        rows.append(
            _pl(
                pid=name,
                name=name,
                position=pos,
                team=team,
                opponent=opp,
                game=game,
                salary=salary,
                objective=obj,
            )
        )

    add("Burrow", "QB", "CIN", "TB", "TB@CIN", 8000, 25.0)
    add("RB-DET", "RB", "DET", "NO", "NO@DET", 4500, 8.0)
    add("RB-PHI", "RB", "PHI", "WAS", "WAS@PHI", 4500, 7.0)
    add("RB-WAS", "RB", "WAS", "PHI", "WAS@PHI", 4500, 6.0)
    if chase:
        add("Chase", "WR", "CIN", "TB", "TB@CIN", 6500, 20.0)
    add("WR-DET", "WR", "DET", "NO", "NO@DET", 5000, 12.0)
    add("WR-PHI", "WR", "PHI", "WAS", "WAS@PHI", 5000, 11.0)
    if not chase:
        add("WR-NO", "WR", "NO", "DET", "NO@DET", 5000, 10.0)
    if egbuka:
        add("Egbuka", "WR", "TB", "CIN", "TB@CIN", 5500, 18.0)
    add("TE-ATL", "TE", "ATL", "PIT", "ATL@PIT", 5000, 9.0)
    add("PHI DST", "D", "PHI", "WAS", "WAS@PHI", 3000, 5.0)
    return rows


class BringBackIlpTest(unittest.TestCase):
    def test_n0_burrow_chase_without_bucs_wr_feasible(self):
        rules = replace(FANDUEL_NFL, salary_floor=0, bring_back=0)
        lu = solve_ilp(_bb_pool(chase=True, egbuka=False), rules)
        names = {p.name for p in lu.slots.values()}
        self.assertIn("Burrow", names)
        self.assertIn("Chase", names)
        self.assertNotIn("Egbuka", names)
        self.assertIn("bring-back off", lu.to_dict()["notes"])

    def test_n1_burrow_chase_without_bucs_wr_infeasible(self):
        rules = replace(FANDUEL_NFL, salary_floor=0, bring_back=1)
        pool = _bb_pool(chase=True, egbuka=False)
        with self.assertRaises(Infeasible):
            solve_ilp(pool, rules)
        with self.assertRaises(Infeasible):
            solve_greedy(pool, rules)

    def test_n1_burrow_chase_egbuka_feasible(self):
        rules = replace(FANDUEL_NFL, salary_floor=0, bring_back=1)
        pool = _bb_pool(chase=True, egbuka=True)
        for solve in (solve_ilp, solve_greedy):
            lu = solve(pool, rules)
            names = {p.name for p in lu.slots.values()}
            self.assertIn("Burrow", names)
            self.assertIn("Chase", names)
            self.assertIn("Egbuka", names)
            note = bring_back_note(lu.slots, 1)
            self.assertEqual(note, "bring-back TB WR/TE (Egbuka)")
            self.assertIn(note, lu.to_dict()["notes"])

    def test_n1_solo_burrow_no_cin_wr_te_feasible(self):
        rules = replace(FANDUEL_NFL, salary_floor=0, bring_back=1)
        pool = _bb_pool(chase=False, egbuka=False)
        lu = solve_ilp(pool, rules)
        names = {p.name for p in lu.slots.values()}
        self.assertIn("Burrow", names)
        self.assertNotIn("Chase", names)
        self.assertEqual(pass_stack_players(lu.slots), [])
        self.assertIn("bring-back n/a (no pass stack)", lu.to_dict()["notes"])
        greedy = solve_greedy(pool, rules)
        self.assertIn("Burrow", {p.name for p in greedy.slots.values()})

    def test_lineup_notes_off_and_stack(self):
        rules = replace(FANDUEL_NFL, salary_floor=0, bring_back=0)
        lu = solve_ilp(_bb_pool(chase=True, egbuka=True), rules)
        notes = lineup_notes(lu.slots, bring_back=0)
        self.assertIn("bring-back off", notes)
        self.assertIn("stack premium on", notes)


def _lac_four_only_pool() -> list[Player]:
    """Exactly 9 players, 4 LAC — feasible at max 4, infeasible at house 3."""
    rows: list[Player] = []

    def add(name, pos, team, opp, game, salary, obj):
        rows.append(
            _pl(
                pid=name,
                name=name,
                position=pos,
                team=team,
                opponent=opp,
                game=game,
                salary=salary,
                objective=obj,
            )
        )

    add("Herbert", "QB", "LAC", "KC", "KC@LAC", 8000, 25.0)
    add("Hampton", "RB", "LAC", "KC", "KC@LAC", 7000, 20.0)
    add("Gibbs", "RB", "DET", "NO", "NO@DET", 8000, 18.0)
    add("McConkey", "WR", "LAC", "KC", "KC@LAC", 6500, 16.0)
    add("Johnston", "WR", "LAC", "KC", "KC@LAC", 5500, 14.0)
    add("Jamo", "WR", "DET", "NO", "NO@DET", 6000, 12.0)
    add("Andrews", "TE", "BAL", "BUF", "BAL@BUF", 5500, 10.0)
    add("Brown", "RB", "CIN", "TB", "TB@CIN", 5000, 9.0)
    add("TEN DST", "D", "TEN", "NYJ", "NYJ@TEN", 3000, 5.0)
    return rows


class HouseMaxPerTeamIlpTest(unittest.TestCase):
    def test_four_lac_infeasible_at_3_feasible_at_4(self):
        pool = _lac_four_only_pool()
        house = replace(FANDUEL_NFL, salary_floor=0, max_per_team=3)
        lobby = replace(FANDUEL_NFL, salary_floor=0, max_per_team=4)
        with self.assertRaises(Infeasible):
            solve_ilp(pool, house)
        with self.assertRaises(Infeasible):
            solve_greedy(pool, house)
        lu = solve_ilp(pool, lobby)
        self.assertEqual(lu.teams.get("LAC"), 4)
        greedy = solve_greedy(pool, lobby)
        self.assertEqual(greedy.teams.get("LAC"), 4)
        self.assertIn("house 4/team bound on LAC", lu.to_dict()["notes"])


def _stack_qb_pool(*, herbert=False, second_lac_wr=True, extra_wr=True) -> list[Player]:
    """DET QB vs LAC WRs.

    extra_wr=False → exactly 3 WRs (McConkey, Johnston, Jamo) so both LAC
    WRs must be used. FLEX is a third RB.
    """
    rows: list[Player] = []

    def add(name, pos, team, opp, game, salary, obj):
        rows.append(
            _pl(
                pid=name,
                name=name,
                position=pos,
                team=team,
                opponent=opp,
                game=game,
                salary=salary,
                objective=obj,
            )
        )

    add("Goff", "QB", "DET", "NO", "NO@DET", 7500, 24.0)
    if herbert:
        add("Herbert", "QB", "LAC", "KC", "KC@LAC", 8000, 23.0)
    add("Gibbs", "RB", "DET", "NO", "NO@DET", 8000, 18.0)
    add("Brown", "RB", "CIN", "TB", "TB@CIN", 5000, 8.0)
    add("FlexRB", "RB", "PHI", "WAS", "WAS@PHI", 4500, 6.0)
    add("McConkey", "WR", "LAC", "KC", "KC@LAC", 6500, 16.0)
    if second_lac_wr:
        add("Johnston", "WR", "LAC", "KC", "KC@LAC", 5500, 14.0)
    add("Jamo", "WR", "DET", "NO", "NO@DET", 6000, 12.0)
    if extra_wr:
        add("W3", "WR", "PHI", "WAS", "WAS@PHI", 5000, 10.0)
        add("W4", "WR", "WAS", "PHI", "WAS@PHI", 5000, 9.0)
    add("Andrews", "TE", "BAL", "BUF", "BAL@BUF", 5500, 9.0)
    add("TEN DST", "D", "TEN", "NYJ", "NYJ@TEN", 3000, 5.0)
    return rows


class StackQbIlpTest(unittest.TestCase):
    def test_two_wr_without_qb_infeasible(self):
        rules = replace(FANDUEL_NFL, salary_floor=0)
        # Only 3 WRs, two LAC — must take both without a LAC QB.
        pool = _stack_qb_pool(herbert=False, extra_wr=False)
        with self.assertRaises(Infeasible):
            solve_ilp(pool, rules)
        with self.assertRaises(Infeasible):
            solve_greedy(pool, rules)

    def test_two_wr_without_qb_ok_when_flag_off(self):
        rules = replace(
            FANDUEL_NFL, salary_floor=0, require_qb_with_two_pass_catchers=False
        )
        pool = _stack_qb_pool(herbert=False, extra_wr=False)
        lu = solve_ilp(pool, rules)
        names = {p.name for p in lu.slots.values()}
        self.assertIn("McConkey", names)
        self.assertIn("Johnston", names)
        self.assertIn("Goff", names)
        self.assertTrue(stack_qb_illegal(lu.slots, True))
        self.assertFalse(stack_qb_illegal(lu.slots, False))
        greedy = solve_greedy(pool, rules)
        self.assertIn("McConkey", {p.name for p in greedy.slots.values()})

    def test_two_wr_with_herbert_feasible(self):
        rules = replace(FANDUEL_NFL, salary_floor=0)
        pool = _stack_qb_pool(herbert=True, extra_wr=False)
        for solve in (solve_ilp, solve_greedy):
            lu = solve(pool, rules)
            names = {p.name for p in lu.slots.values()}
            self.assertIn("Herbert", names)
            self.assertIn("McConkey", names)
            self.assertIn("Johnston", names)
            self.assertIn("stack LAC QB+WR (McConkey, Johnston)", lu.to_dict()["notes"])

    def test_one_wr_without_qb_feasible(self):
        rules = replace(FANDUEL_NFL, salary_floor=0)
        pool = _stack_qb_pool(herbert=False, second_lac_wr=False)
        lu = solve_ilp(pool, rules)
        names = {p.name for p in lu.slots.values()}
        self.assertIn("Goff", names)
        self.assertIn("McConkey", names)
        self.assertNotIn("Johnston", names)
        self.assertFalse(stack_qb_illegal(lu.slots, True))


def _premium_pool() -> list[Player]:
    """DET QB slightly ahead of LAC QB; LAC WR makes the stack premium swing it."""
    rows: list[Player] = []

    def add(name, pos, team, opp, game, salary, obj):
        rows.append(
            _pl(
                pid=name,
                name=name,
                position=pos,
                team=team,
                opponent=opp,
                game=game,
                salary=salary,
                objective=obj,
            )
        )

    add("Herbert", "QB", "LAC", "KC", "KC@LAC", 8000, 20.0)
    add("Goff", "QB", "DET", "NO", "NO@DET", 8000, 20.5)
    add("Hampton", "RB", "LAC", "KC", "KC@LAC", 7000, 8.0)
    add("Gibbs", "RB", "DET", "NO", "NO@DET", 7000, 8.0)
    add("McConkey", "WR", "LAC", "KC", "KC@LAC", 6500, 15.0)
    add("Jamo", "WR", "DET", "NO", "NO@DET", 6000, 10.0)
    add("W3", "WR", "PHI", "WAS", "WAS@PHI", 5000, 9.0)
    add("W4", "WR", "WAS", "PHI", "WAS@PHI", 5000, 8.0)
    add("Andrews", "TE", "BAL", "BUF", "BAL@BUF", 5500, 7.0)
    add("TEN DST", "D", "TEN", "NYJ", "NYJ@TEN", 3000, 5.0)
    return rows


class StackPremiumIlpTest(unittest.TestCase):
    def test_ilp_picks_herbert_with_ladd_premium(self):
        rules = replace(FANDUEL_NFL, salary_floor=0)
        lu = solve_ilp(_premium_pool(), rules)
        self.assertEqual(lu.slots["QB"].name, "Herbert")
        names = {p.name for p in lu.slots.values()}
        self.assertIn("McConkey", names)
        # Printed Proj is week1_score (20), not 20 + 0.12*15.
        self.assertAlmostEqual(lu.slots["QB"].projection, 20.0, places=6)
        self.assertAlmostEqual(STACK_COEF * 15.0, 1.8, places=6)
        self.assertIn("stack premium on", lu.to_dict()["notes"])

    def test_rb_does_not_create_qb_premium_alone(self):
        """Hampton (RB) on LAC does not pull Herbert over Goff without a WR."""
        rules = replace(FANDUEL_NFL, salary_floor=0)
        pool = [
            p
            for p in _premium_pool()
            if p.name != "McConkey"
        ]
        pool.append(
            _pl(
                pid="W5",
                name="W5",
                position="WR",
                team="ATL",
                opponent="PIT",
                game="ATL@PIT",
                salary=5000,
                objective=15.0,
            )
        )
        lu = solve_ilp(pool, rules)
        self.assertEqual(lu.slots["QB"].name, "Goff")


if __name__ == "__main__":
    unittest.main()
