"""Structural game Monte Carlo FD-point draws (Vegas total+spread)."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from ncaaf.interview import LEAN_MULT
from ncaaf.optimize import _sim_n, fppg_objective_error, parse_args
from ncaaf.players import Player
from ncaaf.projections import (
    FLOOR_SALARY,
    VALUE_EPS,
    projection_board,
    prop_factor,
    week1_score,
)
from ncaaf.props import PlayerProp
from ncaaf.sim import (
    DEFAULT_DRAWS,
    apply_ilp_objective,
    has_volume_props,
    model_point,
    sim_header,
    simulate_games,
    simulate_player,
    simulate_pool,
    volume_point,
)


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("pid", kw.get("name", "p")),
        name="Cam",
        position="WR",
        salary=8000,
        team="MISS",
        opponent="LOU",
        game="LOU@MISS",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=32.5,
        depth_rank=1,
        spread=-7.0,
        prop_fd=None,
        script_applied=True,
    )
    fields.update(kw)
    obj = week1_score(
        fields["implied_total"] or 0.0,
        fields["salary"],
        depth_rank=fields.get("depth_rank"),
        position=fields["position"],
        prop_fd=fields.get("prop_fd"),
        team_spread=fields.get("spread"),
        apply_script=fields.get("script_applied", True),
    )
    fields["objective"] = obj
    allowed = Player.__dataclass_fields__
    return Player(**{k: v for k, v in fields.items() if k in allowed})


class HeaderTest(unittest.TestCase):
    def test_header_names_game_draw(self):
        text = sim_header(10000)
        self.assertEqual(
            text,
            "sim 10000 game draws — one world per game (Vegas total+spread); "
            "teammates share it. Not a PBP copula.",
        )


class FlagsTest(unittest.TestCase):
    def test_sim_default_off_flag_is_10000_zero_off(self):
        off = parse_args(["--csv", "x.csv"])
        self.assertEqual(off.sim, 0)
        on = parse_args(["--csv", "x.csv", "--sim"])
        self.assertEqual(on.sim, DEFAULT_DRAWS)
        zero = parse_args(["--csv", "x.csv", "--sim", "0"])
        self.assertEqual(zero.sim, 0)
        n = parse_args(["--csv", "x.csv", "--sim", "500"])
        self.assertEqual(n.sim, 500)
        seed = parse_args(["--csv", "x.csv", "--sim-seed", "7"])
        self.assertEqual(seed.sim_seed, 7)
        default_seed = parse_args(["--csv", "x.csv"])
        self.assertEqual(default_seed.sim_seed, 1)

    def test_objective_flag_default_mean(self):
        off = parse_args(["--csv", "x.csv"])
        self.assertEqual(off.objective, "mean")
        floor = parse_args(["--csv", "x.csv", "--objective", "floor"])
        self.assertEqual(floor.objective, "floor")
        ceil = parse_args(["--csv", "x.csv", "--objective=ceiling"])
        self.assertEqual(ceil.objective, "ceiling")
        self.assertEqual(_sim_n(off), 0)
        self.assertEqual(_sim_n(floor), DEFAULT_DRAWS)
        self.assertEqual(
            _sim_n(parse_args(["--csv", "x.csv", "--objective", "floor", "--sim", "500"])),
            500,
        )

    def test_use_fppg_rejects_floor_ceiling(self):
        self.assertIsNone(fppg_objective_error(True, "mean"))
        self.assertIsNone(fppg_objective_error(False, "floor"))
        err = fppg_objective_error(True, "floor")
        self.assertIsNotNone(err)
        self.assertIn("--use-fppg", err)
        self.assertIn("floor", err)
        self.assertIsNotNone(fppg_objective_error(True, "ceiling"))


class PropsSimTest(unittest.TestCase):
    def test_mean_follows_clamped_implied(self):
        prop = PlayerProp(
            name="arch manning",
            pass_yds=250.5,
            pass_tds=1.5,
            rush_yds=25.5,
            book="fanduel",
        )
        fd = prop.fd_points()
        assert fd is not None
        pl = _pl(
            pid="arch",
            name="Arch Manning",
            position="QB",
            salary=11000,
            team="TEX",
            opponent="TXST",
            game="TXST@TEX",
            implied_total=45.0,
            prop_fd=fd,
            prop_pass_yds=250.5,
            prop_pass_tds=1.5,
            prop_rush_yds=25.5,
        )
        self.assertTrue(has_volume_props(pl))
        self.assertAlmostEqual(volume_point(pl), fd, places=6)
        st = simulate_player(pl, n=DEFAULT_DRAWS, seed=1)
        self.assertEqual(st.source, "props")
        core = model_point(pl)
        self.assertAlmostEqual(st.mean, core * prop_factor(core, fd), delta=1.0)
        self.assertLess(st.p10, st.p50)
        self.assertLess(st.p50, st.p90)

    def test_cfb_scoring_no_nfl_yardage_bonus(self):
        # 100 rush yds = 10.0 CFB; NFL would add +3. 300 pass = 12.0, not +3.
        rush = _pl(
            pid="rb100",
            name="Hundred",
            position="RB",
            prop_rush_yds=100.0,
            prop_fd=10.0,
        )
        self.assertAlmostEqual(volume_point(rush), 10.0, places=6)
        pas = _pl(
            pid="qb300",
            name="Three Hundred",
            position="QB",
            prop_pass_yds=300.0,
            prop_fd=12.0,
        )
        self.assertAlmostEqual(volume_point(pas), 12.0, places=6)
        rec = _pl(
            pid="wr",
            name="Catch",
            position="WR",
            prop_rec_yds=80.0,
            prop_receptions=5.0,
            prop_fd=10.5,
        )
        # 80*0.1 + 5*0.5 = 10.5; no rec TD invented
        self.assertAlmostEqual(volume_point(rec), 10.5, places=6)

    def test_draws_floor_at_zero(self):
        pl = _pl(
            pid="tiny",
            name="Tiny",
            position="RB",
            prop_rush_yds=1.0,
            prop_fd=0.1,
        )
        st = simulate_player(pl, n=2000, seed=1)
        self.assertGreaterEqual(st.p10, 0.0)
        self.assertGreaterEqual(st.mean, 0.0)


class ModelSimTest(unittest.TestCase):
    def test_model_point_excludes_leftover(self):
        pl = _pl(name="Walk On WR", position="WR", depth_rank=1, salary=8000)
        leftover = VALUE_EPS * (FLOOR_SALARY / 8000)
        core = week1_score(32.5, 8000, 1, "WR", team_spread=-7.0) - leftover
        self.assertAlmostEqual(model_point(pl), core, places=6)
        self.assertAlmostEqual(
            week1_score(32.5, 8000, 1, "WR", team_spread=-7.0),
            core + leftover,
            places=6,
        )

    def test_mean_matches_week1_without_leftover(self):
        pl = _pl(name="Walk On WR", position="WR", depth_rank=1, salary=8000)
        st = simulate_player(pl, n=DEFAULT_DRAWS, seed=1)
        self.assertEqual(st.source, "model")
        self.assertFalse(has_volume_props(pl))
        self.assertAlmostEqual(st.mean, model_point(pl), delta=0.5)
        leftover = VALUE_EPS * (FLOOR_SALARY / 8000)
        self.assertAlmostEqual(
            st.mean,
            week1_score(32.5, 8000, 1, "WR", team_spread=-7.0) - leftover,
            delta=0.5,
        )

    def test_script_ignore_is_identity_mix(self):
        keep = _pl(
            name="Sit WR",
            position="WR",
            spread=-28.0,
            implied_total=40.0,
            script_applied=True,
        )
        ignore = _pl(
            pid="sit-off",
            name="Sit WR",
            position="WR",
            spread=-28.0,
            implied_total=40.0,
            script_applied=False,
        )
        self.assertGreater(model_point(ignore), model_point(keep))


class SeedTest(unittest.TestCase):
    def test_seed_reproducible_and_order_independent(self):
        a = _pl(pid="a", name="A", position="QB", implied_total=40.0)
        b = _pl(pid="b", name="B", position="RB", implied_total=28.0)
        one = simulate_player(a, n=500, seed=1)
        two = simulate_player(a, n=500, seed=1)
        self.assertEqual(one.mean, two.mean)
        self.assertEqual(one.p10, two.p10)
        fwd = simulate_pool([a, b], n=500, seed=1)
        rev = simulate_pool([b, a], n=500, seed=1)
        self.assertEqual(fwd["a"].mean, rev["a"].mean)
        self.assertEqual(fwd["b"].p90, rev["b"].p90)
        other = simulate_player(a, n=500, seed=2)
        self.assertNotEqual(one.mean, other.mean)


class BoardSimTest(unittest.TestCase):
    def test_board_rows_get_percentiles(self):
        model = _pl(name="Walk On WR", position="WR", depth_rank=1, salary=8000)
        prop = _pl(
            pid="chambliss",
            name="Trinidad Chambliss",
            position="QB",
            depth_rank=1,
            salary=11600,
            prop_fd=23.5,
            prop_pass_yds=240.5,
            prop_pass_tds=1.5,
            prop_rush_yds=35.5,
        )
        by_pid = simulate_pool([model, prop], n=DEFAULT_DRAWS, seed=1)
        rows = {r["player"]: r for r in projection_board(
            [model, prop], sim_by_pid=by_pid
        )}
        for name in ("Walk On WR", "Trinidad Chambliss"):
            self.assertIn("mean", rows[name])
            self.assertIn("p10", rows[name])
            self.assertIn("p50", rows[name])
            self.assertIn("p90", rows[name])
        core = model_point(prop)
        self.assertAlmostEqual(
            rows["Trinidad Chambliss"]["mean"],
            core * prop_factor(core, prop.prop_fd),
            delta=2.0,
        )
        leftover = VALUE_EPS * (FLOOR_SALARY / 8000)
        self.assertAlmostEqual(
            rows["Walk On WR"]["mean"],
            week1_score(32.5, 8000, 1, "WR", team_spread=-7.0) - leftover,
            delta=1.5,
        )
        # Point estimate `proj` is still week1_score (ILP), not the sim mean.
        self.assertAlmostEqual(
            rows["Walk On WR"]["proj"],
            week1_score(32.5, 8000, 1, "WR", team_spread=-7.0),
            places=4,
        )

    def test_board_without_sim_has_no_percentiles(self):
        pl = _pl(name="Walk On WR")
        row = projection_board([pl])[0]
        self.assertNotIn("mean", row)
        self.assertNotIn("p10", row)


class PrintTest(unittest.TestCase):
    def test_board_header_says_game_monte_carlo(self):
        from ncaaf.projections import print_projection_board

        pl = _pl(name="Walk On WR")
        by_pid = simulate_pool([pl], n=50, seed=1)
        rows = projection_board([pl], sim_by_pid=by_pid)
        buf = io.StringIO()
        with redirect_stderr(buf):
            print_projection_board(rows, sim_n=50)
        text = buf.getvalue()
        self.assertIn("game Monte Carlo", text)
        self.assertIn("teammates share the draw", text)
        self.assertIn("mean", text)
        self.assertIn("p10", text)
        self.assertIn("p90", text)
        self.assertIn("p99", text)


class IlpObjectiveTest(unittest.TestCase):
    def _prop_qb(self) -> Player:
        prop = PlayerProp(
            name="arch manning",
            pass_yds=250.5,
            pass_tds=1.5,
            rush_yds=25.5,
            book="fanduel",
        )
        fd = prop.fd_points()
        assert fd is not None
        return _pl(
            pid="arch",
            name="Arch Manning",
            position="QB",
            salary=11000,
            team="TEX",
            opponent="TXST",
            game="TXST@TEX",
            implied_total=45.0,
            prop_fd=fd,
            prop_pass_yds=250.5,
            prop_pass_tds=1.5,
            prop_rush_yds=25.5,
        )

    def test_mean_does_not_change_week1_score(self):
        pl = self._prop_qb()
        want = week1_score(
            45.0,
            11000,
            depth_rank=1,
            position="QB",
            prop_fd=pl.prop_fd,
            team_spread=-7.0,
        )
        self.assertAlmostEqual(pl.objective, want, places=6)
        by_pid = simulate_pool([pl], n=200, seed=1)
        mean_pool = apply_ilp_objective([pl], "mean", sim_by_pid=by_pid)
        self.assertAlmostEqual(mean_pool[0].objective, want, places=6)
        self.assertEqual(mean_pool[0].objective, pl.objective)

    def test_ceiling_ge_floor_same_pool_prop_player(self):
        pl = self._prop_qb()
        want_mean = pl.objective
        by_pid = simulate_pool([pl], n=DEFAULT_DRAWS, seed=1)
        floor_pool = apply_ilp_objective([pl], "floor", sim_by_pid=by_pid)
        ceil_pool = apply_ilp_objective([pl], "ceiling", sim_by_pid=by_pid)
        st = by_pid[pl.pid]
        self.assertAlmostEqual(floor_pool[0].objective, st.p10, places=6)
        self.assertAlmostEqual(ceil_pool[0].objective, st.p99, places=6)
        self.assertGreaterEqual(ceil_pool[0].objective, floor_pool[0].objective)
        self.assertAlmostEqual(pl.objective, want_mean, places=6)

    def test_lean_applies_to_p10_p90_not_double_on_mean(self):
        pl = self._prop_qb()
        leaned = pl.objective * LEAN_MULT
        by_pid = simulate_pool([pl], n=500, seed=1)
        st = by_pid[pl.pid]
        mean_pool = apply_ilp_objective(
            [pl],
            "mean",
            sim_by_pid=by_pid,
            lean_pids=frozenset({pl.pid}),
            lean_mult=LEAN_MULT,
        )
        self.assertAlmostEqual(mean_pool[0].objective, pl.objective, places=6)
        self.assertNotAlmostEqual(mean_pool[0].objective, leaned, places=4)
        floor_pool = apply_ilp_objective(
            [pl],
            "floor",
            sim_by_pid=by_pid,
            lean_pids=frozenset({pl.pid}),
            lean_mult=LEAN_MULT,
        )
        ceil_pool = apply_ilp_objective(
            [pl],
            "ceiling",
            sim_by_pid=by_pid,
            lean_pids=frozenset({pl.pid}),
            lean_mult=LEAN_MULT,
        )
        self.assertAlmostEqual(
            floor_pool[0].objective, st.p10 * LEAN_MULT, places=6
        )
        self.assertAlmostEqual(
            ceil_pool[0].objective, st.p99 * LEAN_MULT, places=6
        )

    def test_sim_to_dict_aliases_floor_ceiling(self):
        pl = self._prop_qb()
        st = simulate_player(pl, n=200, seed=1)
        d = st.to_dict()
        self.assertEqual(d["floor"], d["p10"])
        self.assertEqual(d["ceiling"], d["p99"])
        self.assertIn("mean", d)


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    den = (dx * dy) ** 0.5
    return 0.0 if den == 0 else num / den


class GameCorrTest(unittest.TestCase):
    def test_teammates_share_the_world_other_game_does_not(self):
        qb = _pl(
            pid="qb",
            name="QB",
            position="QB",
            team="ORE",
            opponent="OKST",
            game="ORE@OKST",
            implied_total=39.0,
            implied_opp=15.5,
            total=54.5,
            spread=-23.5,
        )
        wr = _pl(
            pid="wr",
            name="WR",
            position="WR",
            team="ORE",
            opponent="OKST",
            game="ORE@OKST",
            implied_total=39.0,
            implied_opp=15.5,
            total=54.5,
            spread=-23.5,
        )
        opp = _pl(
            pid="opp",
            name="Opp WR",
            position="WR",
            team="OKST",
            opponent="ORE",
            game="ORE@OKST",
            implied_total=15.5,
            implied_opp=39.0,
            total=54.5,
            spread=23.5,
        )
        other = _pl(
            pid="other",
            name="Other RB",
            position="RB",
            team="DUKE",
            opponent="ILL",
            game="DUKE@ILL",
            implied_total=23.0,
            implied_opp=29.0,
            total=52.0,
            spread=6.0,
        )
        gs = simulate_games([qb, wr, opp, other], n=2000, seed=1)
        cq = list(gs.draws["qb"])
        cw = list(gs.draws["wr"])
        co = list(gs.draws["opp"])
        cd = list(gs.draws["other"])
        self.assertGreater(_pearson(cq, cw), 0.5)
        self.assertGreater(_pearson(cq, cw), _pearson(cq, co))
        self.assertLess(abs(_pearson(cq, cd)), 0.15)

    def test_lineup_joint_is_not_the_sum_of_p90s(self):
        qb = _pl(
            pid="qb",
            name="QB",
            position="QB",
            team="ORE",
            opponent="OKST",
            game="ORE@OKST",
            implied_total=39.0,
            implied_opp=15.5,
            total=54.5,
            spread=-23.5,
        )
        wr = _pl(
            pid="wr",
            name="WR",
            position="WR",
            team="ORE",
            opponent="OKST",
            game="ORE@OKST",
            implied_total=39.0,
            implied_opp=15.5,
            total=54.5,
            spread=-23.5,
        )
        gs = simulate_games([qb, wr], n=2000, seed=1)
        joint = gs.lineup_stats(["qb", "wr"])
        assert joint is not None
        summed = gs.by_pid["qb"].p90 + gs.by_pid["wr"].p90
        self.assertNotAlmostEqual(joint.p90, summed, places=1)
        self.assertGreater(joint.p90, gs.by_pid["qb"].p90)


if __name__ == "__main__":
    unittest.main()
