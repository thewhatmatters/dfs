"""Structural game Monte Carlo FD-point draws (Vegas total+spread)."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from nfl.optimize import _sim_n, parse_args
from nfl.players import Player
from nfl.projections import POS_FD_SHARE, projection_board, prop_factor, week1_score
from nfl.rules import DST_SACK_TO_PRIOR, FANDUEL_NFL, dst_projection
from nfl.sim import (
    DEFAULT_DRAWS,
    apply_ilp_objective,
    has_volume_props,
    model_point,
    sim_header,
    simulate_games,
    simulate_player,
    simulate_pool,
    volume_point,
    yardage_bonuses,
    _props_draw,
    _score_world,
)


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("pid", kw.get("name", "p")),
        name="Cam",
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
        depth_rank=1,
        prop_fd=None,
    )
    fields.update(kw)
    if fields.get("objective") is None:
        fields["objective"] = week1_score(
            fields.get("implied_total") or 0.0,
            depth_rank=fields.get("depth_rank"),
            position=fields["position"],
            prop_fd=fields.get("prop_fd"),
            implied_opp=fields.get("implied_opp"),
        )
    allowed = Player.__dataclass_fields__
    return Player(**{k: v for k, v in fields.items() if k in allowed})


class _FixedGauss:
    def __init__(self, value: float) -> None:
        self.value = value

    def gauss(self, mu: float, sigma: float) -> float:
        return self.value


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
        # --sim=0 is off only for mean; floor still runs default draws.
        self.assertEqual(
            _sim_n(parse_args(["--csv", "x.csv", "--objective", "floor", "--sim", "0"])),
            DEFAULT_DRAWS,
        )
        self.assertEqual(
            _sim_n(parse_args(["--csv", "x.csv", "--sim", "0"])),
            0,
        )

    def test_cash_line_n_lineups_min_unique_defaults(self):
        off = parse_args(["--csv", "x.csv"])
        self.assertEqual(off.cash_line, 150.0)
        self.assertEqual(off.n_lineups, 1)
        self.assertEqual(off.min_unique, 2)
        self.assertEqual(off.bring_back, 0)
        self.assertEqual(off.max_per_team, 3)
        self.assertEqual(off.stack_qb, "on")
        self.assertIsNone(off.upload)
        custom = parse_args(
            [
                "--csv",
                "x.csv",
                "--cash-line",
                "140",
                "--n-lineups",
                "20",
                "--min-unique",
                "3",
                "--upload",
                "results/nfl-133104-upload.csv",
                "--bring-back",
                "1",
                "--max-per-team",
                "4",
                "--stack-qb",
                "off",
            ]
        )
        self.assertEqual(custom.cash_line, 140.0)
        self.assertEqual(custom.n_lineups, 20)
        self.assertEqual(custom.min_unique, 3)
        self.assertEqual(custom.bring_back, 1)
        self.assertEqual(custom.max_per_team, 4)
        self.assertEqual(custom.stack_qb, "off")
        self.assertEqual(custom.upload, "results/nfl-133104-upload.csv")

    def test_n_lineups_bounds(self):
        buf = io.StringIO()
        with redirect_stderr(buf), self.assertRaises(SystemExit):
            parse_args(["--csv", "x.csv", "--n-lineups", "0"])
        with redirect_stderr(buf), self.assertRaises(SystemExit):
            parse_args(["--csv", "x.csv", "--n-lineups", "151"])
        with redirect_stderr(buf), self.assertRaises(SystemExit):
            parse_args(["--csv", "x.csv", "--min-unique", "0"])
        ok = parse_args(["--csv", "x.csv", "--n-lineups", "150"])
        self.assertEqual(ok.n_lineups, 150)
        with redirect_stderr(buf), self.assertRaises(SystemExit):
            parse_args(["--csv", "x.csv", "--max-per-team", "0"])
        with redirect_stderr(buf), self.assertRaises(SystemExit):
            parse_args(["--csv", "x.csv", "--max-per-team", "5"])
        four = parse_args(["--csv", "x.csv", "--max-per-team", "4"])
        self.assertEqual(four.max_per_team, 4)


class BonusDrawTest(unittest.TestCase):
    def test_bonuses_fire_on_310_pass_draw(self):
        sc = FANDUEL_NFL.scoring
        pl = _pl(
            pid="qb310",
            name="Three Ten",
            position="QB",
            prop_pass_yds=250.0,
            prop_fd=10.0,
        )
        pts = _props_draw(_FixedGauss(310.0), pl)
        self.assertAlmostEqual(
            pts, 310.0 * sc["pass_yd"] + sc["bonus_pass_yd_300"], places=6
        )
        just_under = _props_draw(_FixedGauss(299.0), pl)
        self.assertAlmostEqual(just_under, 299.0 * sc["pass_yd"], places=6)
        self.assertAlmostEqual(
            yardage_bonuses({"pass_yd": 310.0}), sc["bonus_pass_yd_300"]
        )
        self.assertEqual(yardage_bonuses({"pass_yd": 299.9}), 0.0)

    def test_rush_and_rec_bonuses_on_draw(self):
        sc = FANDUEL_NFL.scoring
        rush = _pl(pid="rb100", position="RB", prop_rush_yds=80.0, prop_fd=8.0)
        rec = _pl(
            pid="wr100",
            position="WR",
            prop_rec_yds=80.0,
            prop_receptions=5.0,
            prop_fd=10.5,
        )
        rush_pts = _props_draw(_FixedGauss(100.0), rush)
        self.assertAlmostEqual(
            rush_pts, 100.0 * sc["rush_yd"] + sc["bonus_rush_yd_100"], places=6
        )
        rec_pts = _props_draw(_FixedGauss(100.0), rec)
        self.assertAlmostEqual(
            rec_pts,
            100.0 * sc["rec_yd"] + 100.0 * sc["rec"] + sc["bonus_rec_yd_100"],
            places=6,
        )


class PropsSimTest(unittest.TestCase):
    def test_mean_follows_clamped_implied(self):
        sc = FANDUEL_NFL.scoring
        fd = 250.5 * sc["pass_yd"] + 1.5 * sc["pass_td"] + 25.5 * sc["rush_yd"]
        pl = _pl(
            pid="goff",
            name="Jared Goff",
            position="QB",
            salary=8000,
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=28.0,
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
        self.assertNotAlmostEqual(week1_score(28.0, 1, "QB", prop_fd=fd), fd)

    def test_volume_point_includes_line_bonus(self):
        sc = FANDUEL_NFL.scoring
        over = _pl(
            pid="qb300",
            position="QB",
            prop_pass_yds=300.0,
            prop_fd=300.0 * sc["pass_yd"] + sc["bonus_pass_yd_300"],
        )
        under = _pl(
            pid="qb299",
            position="QB",
            prop_pass_yds=299.5,
            prop_fd=299.5 * sc["pass_yd"],
        )
        self.assertAlmostEqual(volume_point(over), over.prop_fd, places=6)
        self.assertAlmostEqual(volume_point(under), under.prop_fd, places=6)

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
    def test_model_point_is_implied_depth_share(self):
        pl = _pl(name="Walk On WR", position="WR", depth_rank=1, implied_total=30.0)
        self.assertAlmostEqual(model_point(pl), 30.0 * 1.0 * 0.18, places=6)
        self.assertAlmostEqual(model_point(pl), week1_score(30.0, 1, "WR"), places=6)

    def test_mean_matches_model_point(self):
        pl = _pl(name="Walk On WR", position="WR", depth_rank=1, implied_total=30.0)
        st = simulate_player(pl, n=DEFAULT_DRAWS, seed=1)
        self.assertEqual(st.source, "model")
        self.assertFalse(has_volume_props(pl))
        self.assertAlmostEqual(st.mean, model_point(pl), delta=0.5)


class DstSimTest(unittest.TestCase):
    def test_dst_p10_p90_exist(self):
        pl = _pl(
            pid="phi-dst",
            name="PHI DST",
            position="D",
            team="PHI",
            opponent="WAS",
            game="WAS@PHI",
            implied_total=22.0,
            implied_opp=24.5,
            depth_rank=None,
        )
        st = simulate_player(pl, n=DEFAULT_DRAWS, seed=1)
        self.assertEqual(st.source, "model")
        self.assertIsNotNone(st.p10)
        self.assertIsNotNone(st.p90)
        self.assertGreaterEqual(st.p90, st.p10)
        d = st.to_dict()
        self.assertEqual(d["floor"], d["p10"])
        self.assertEqual(d["ceiling"], d["p90"])
        self.assertAlmostEqual(model_point(pl), dst_projection(24.5), places=6)
        self.assertGreater(model_point(pl), DST_SACK_TO_PRIOR - 0.01)


class SeedTest(unittest.TestCase):
    def test_seed_reproducible_and_order_independent(self):
        a = _pl(pid="a", name="A", position="QB", implied_total=28.0)
        b = _pl(pid="b", name="B", position="RB", implied_total=24.0)
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
            pid="qb",
            name="Jared Goff",
            position="QB",
            depth_rank=1,
            salary=8000,
            prop_fd=18.5,
            prop_pass_yds=250.5,
            prop_pass_tds=1.5,
        )
        by_pid = simulate_pool([model, prop], n=DEFAULT_DRAWS, seed=1)
        rows = {r["player"]: r for r in projection_board(
            [model, prop], sim_by_pid=by_pid
        )}
        for name in ("Walk On WR", "Jared Goff"):
            self.assertIn("mean", rows[name])
            self.assertIn("p10", rows[name])
            self.assertIn("p50", rows[name])
            self.assertIn("p90", rows[name])
        self.assertAlmostEqual(
            rows["Walk On WR"]["proj"],
            week1_score(24.0, 1, "WR"),
            places=4,
        )

    def test_board_without_sim_has_no_percentiles(self):
        pl = _pl(name="Walk On WR")
        row = projection_board([pl])[0]
        self.assertNotIn("mean", row)
        self.assertNotIn("p10", row)


class PrintTest(unittest.TestCase):
    def test_board_header_says_game_monte_carlo(self):
        from nfl.projections import print_projection_board

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


class IlpObjectiveTest(unittest.TestCase):
    def _prop_qb(self) -> Player:
        sc = FANDUEL_NFL.scoring
        fd = 250.5 * sc["pass_yd"] + 1.5 * sc["pass_td"] + 25.5 * sc["rush_yd"]
        return _pl(
            pid="goff",
            name="Jared Goff",
            position="QB",
            salary=8000,
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=28.0,
            prop_fd=fd,
            prop_pass_yds=250.5,
            prop_pass_tds=1.5,
            prop_rush_yds=25.5,
        )

    def test_mean_does_not_replace_week1_score(self):
        pl = self._prop_qb()
        want = week1_score(
            28.0,
            depth_rank=1,
            position="QB",
            prop_fd=pl.prop_fd,
        )
        self.assertAlmostEqual(pl.objective, want, places=6)
        by_pid = simulate_pool([pl], n=200, seed=1)
        mean_pool = apply_ilp_objective([pl], "mean", sim_by_pid=by_pid)
        self.assertAlmostEqual(mean_pool[0].objective, want, places=6)
        self.assertEqual(mean_pool[0].objective, pl.objective)

    def test_ceiling_ge_floor_same_pool(self):
        pl = self._prop_qb()
        dst = _pl(
            pid="phi-dst",
            name="PHI DST",
            position="D",
            team="PHI",
            opponent="WAS",
            implied_opp=24.5,
            depth_rank=None,
        )
        want_mean = pl.objective
        by_pid = simulate_pool([pl, dst], n=DEFAULT_DRAWS, seed=1)
        floor_pool = apply_ilp_objective([pl, dst], "floor", sim_by_pid=by_pid)
        ceil_pool = apply_ilp_objective([pl, dst], "ceiling", sim_by_pid=by_pid)
        st = by_pid[pl.pid]
        self.assertAlmostEqual(floor_pool[0].objective, st.p10, places=6)
        self.assertAlmostEqual(ceil_pool[0].objective, st.p90, places=6)
        self.assertGreaterEqual(ceil_pool[0].objective, floor_pool[0].objective)
        self.assertGreaterEqual(ceil_pool[1].objective, floor_pool[1].objective)
        self.assertAlmostEqual(pl.objective, want_mean, places=6)

    def test_sim_to_dict_aliases_floor_ceiling(self):
        pl = self._prop_qb()
        st = simulate_player(pl, n=200, seed=1)
        d = st.to_dict()
        self.assertEqual(d["floor"], d["p10"])
        self.assertEqual(d["ceiling"], d["p90"])
        self.assertIn("mean", d)
        self.assertIn("p99", d)
        self.assertNotEqual(d["ceiling"], d["p99"])


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
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=28.0,
            implied_opp=22.0,
            total=50.0,
            spread=-6.0,
        )
        wr = _pl(
            pid="wr",
            name="WR",
            position="WR",
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=28.0,
            implied_opp=22.0,
            total=50.0,
            spread=-6.0,
        )
        opp = _pl(
            pid="opp",
            name="Opp WR",
            position="WR",
            team="NO",
            opponent="DET",
            game="NO@DET",
            implied_total=22.0,
            implied_opp=28.0,
            total=50.0,
            spread=6.0,
        )
        other = _pl(
            pid="other",
            name="Other RB",
            position="RB",
            team="PHI",
            opponent="WAS",
            game="WAS@PHI",
            implied_total=24.0,
            implied_opp=22.0,
            total=46.0,
            spread=-2.0,
        )
        gs = simulate_games([qb, wr, opp, other], n=2000, seed=1)
        cq = list(gs.draws["qb"])
        cw = list(gs.draws["wr"])
        co = list(gs.draws["opp"])
        cd = list(gs.draws["other"])
        self.assertGreater(_pearson(cq, cw), 0.5)
        self.assertGreater(_pearson(cq, cw), _pearson(cq, co))
        self.assertLess(abs(_pearson(cq, cd)), 0.15)

    def test_dst_anti_correlates_with_opp_skill(self):
        dst = _pl(
            pid="det-dst",
            name="DET DST",
            position="D",
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=28.0,
            implied_opp=22.0,
            total=50.0,
            spread=-6.0,
            depth_rank=None,
        )
        opp = _pl(
            pid="opp",
            name="Opp WR",
            position="WR",
            team="NO",
            opponent="DET",
            game="NO@DET",
            implied_total=22.0,
            implied_opp=28.0,
            total=50.0,
            spread=6.0,
        )
        gs = simulate_games([dst, opp], n=2000, seed=1)
        r = _pearson(list(gs.draws["det-dst"]), list(gs.draws["opp"]))
        self.assertLess(r, -0.3)

    def test_lineup_joint_is_not_the_sum_of_p90s(self):
        qb = _pl(
            pid="qb",
            name="QB",
            position="QB",
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=28.0,
            implied_opp=22.0,
            total=50.0,
            spread=-6.0,
        )
        wr = _pl(
            pid="wr",
            name="WR",
            position="WR",
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=28.0,
            implied_opp=22.0,
            total=50.0,
            spread=-6.0,
        )
        opp = _pl(
            pid="opp",
            name="Opp WR",
            position="WR",
            team="NO",
            opponent="DET",
            game="NO@DET",
            implied_total=22.0,
            implied_opp=28.0,
            total=50.0,
            spread=6.0,
        )
        gs = simulate_games([qb, wr, opp], n=2000, seed=1)
        joint = gs.lineup_stats(["qb", "wr", "opp"])
        assert joint is not None
        summed = gs.by_pid["qb"].p90 + gs.by_pid["wr"].p90 + gs.by_pid["opp"].p90
        self.assertNotAlmostEqual(joint.p90, summed, places=1)
        self.assertGreater(joint.p90, gs.by_pid["qb"].p90)


class GameBonusTest(unittest.TestCase):
    def test_scaled_rush_line_fires_bonus(self):
        sc = FANDUEL_NFL.scoring
        rb = _pl(
            pid="rb90",
            position="RB",
            implied_total=20.0,
            prop_rush_yds=90.0,
        )
        share = POS_FD_SHARE["RB"]
        over = _score_world(rb, 30.0, 20.0)
        self.assertAlmostEqual(over, 30.0 * share + sc["bonus_rush_yd_100"], places=6)
        under = _score_world(rb, 20.0, 20.0)
        self.assertAlmostEqual(under, 20.0 * share, places=6)

    def test_game_draws_sometimes_fire_rush_bonus(self):
        sc = FANDUEL_NFL.scoring
        rb = _pl(
            pid="rb90",
            position="RB",
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=20.0,
            implied_opp=20.0,
            total=40.0,
            spread=0.0,
            prop_rush_yds=90.0,
        )
        gs = simulate_games([rb], n=2000, seed=1)
        share = POS_FD_SHARE["RB"]
        bonus = sc["bonus_rush_yd_100"]
        fired = 0
        for pts in gs.draws["rb90"]:
            team_if_bonus = (pts - bonus) / share
            if team_if_bonus <= 0:
                continue
            yds = 90.0 * team_if_bonus / 20.0
            if yds >= 100.0 - 1e-9 and abs(team_if_bonus * share + bonus - pts) < 1e-6:
                fired += 1
        self.assertGreater(fired, 0)


if __name__ == "__main__":
    unittest.main()
