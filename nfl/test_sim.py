"""Layered game Monte Carlo FD-point draws (total+spread, script, shares)."""

from __future__ import annotations

import io
import random
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from nfl.optimize import _sim_n, parse_args
from nfl.players import Player
from nfl.projections import POS_FD_SHARE, projection_board, prop_factor, week1_score
from nfl.rules import DST_SACK_TO_PRIOR, FANDUEL_NFL, dst_projection
from nfl.sim import (
    GameSim,
    SimStats,
    DEFAULT_DRAWS,
    SPREAD_SIGMA,
    TOTAL_SIGMA_FRAC,
    apply_ilp_objective,
    draw_simplex,
    format_board_vs_sim,
    format_sim_diagnostic,
    game_sigmas,
    has_volume_props,
    mean_target_share,
    model_point,
    neutral_pass_rate_of,
    passing_qb,
    pearson,
    rush_share_means,
    scripted_pass_rate,
    share_kappa,
    sim_header,
    simulate_games,
    simulate_player,
    simulate_pool,
    volume_point,
    yardage_bonuses,
    _props_draw,
    _score_world,
)
from nfl.sim_efficiency import (
    INT_RATE,
    PASS_YPA,
    OpportunityCount,
    PlaceholderEfficiency,
    ReceivingLine,
)
from nfl.sim_inputs import (
    SimInputs,
    SnapWeek,
    TargetWeek,
    TeamStat,
    load_sim_inputs,
    sim_inputs_from_records,
    team_stat_from_row,
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
            target_share=fields.get("target_share"),
            snap_share=fields.get("snap_share"),
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
            "sim 10000 layered draws — game total+spread "
            "(EPA dispersion when team stats exist), volume/script, "
            "opportunity shares. Teammates share the world. Not a PBP copula.",
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
        self.assertEqual(default_seed.sim_efficiency, "placeholder")
        data_eff = parse_args(["--csv", "x.csv", "--sim-efficiency", "data"])
        self.assertEqual(data_eff.sim_efficiency, "data")
        self.assertIsNone(default_seed.sim_inputs)
        loaded = parse_args(
            ["--csv", "x.csv", "--sim-inputs", "nfl/testdata/sim_layers.json"]
        )
        self.assertEqual(loaded.sim_inputs, "nfl/testdata/sim_layers.json")

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

    def test_projection_source_defaults_to_board(self):
        off = parse_args(["--csv", "x.csv"])
        self.assertEqual(off.projection_source, "board")
        self.assertEqual(_sim_n(off), 0)
        chosen = parse_args(["--csv", "x.csv", "--projection-source", "sim"])
        self.assertEqual(chosen.projection_source, "sim")
        self.assertEqual(_sim_n(chosen), DEFAULT_DRAWS)
        self.assertEqual(
            _sim_n(
                parse_args(
                    ["--csv", "x.csv", "--projection-source", "sim", "--sim", "0"]
                )
            ),
            DEFAULT_DRAWS,
        )
        self.assertEqual(
            _sim_n(
                parse_args(
                    ["--csv", "x.csv", "--projection-source", "sim", "--sim", "400"]
                )
            ),
            400,
        )
        with self.assertRaises(SystemExit):
            parse_args(["--csv", "x.csv", "--projection-source", "fppg"])

    def test_cash_line_n_lineups_min_unique_defaults(self):
        off = parse_args(["--csv", "x.csv"])
        self.assertEqual(off.cash_line, 150.0)
        self.assertEqual(off.n_lineups, 1)
        self.assertEqual(off.min_unique, 2)
        self.assertEqual(off.max_exposure, 1.0)
        self.assertEqual(off.diversity, "chalk")
        self.assertFalse(off.coverage)
        self.assertEqual(off.bring_back, 0)
        self.assertEqual(off.max_per_team, 3)
        self.assertEqual(off.stack_qb, "on")
        self.assertFalse(off.skip_targets)
        self.assertIsNone(off.targets_week)
        self.assertFalse(off.skip_snaps)
        self.assertIsNone(off.snaps_week)
        skipped = parse_args(["--csv", "x.csv", "--skip-targets", "--targets-week", "1"])
        self.assertTrue(skipped.skip_targets)
        self.assertEqual(skipped.targets_week, 1)
        snap_args = parse_args(["--csv", "x.csv", "--skip-snaps", "--snaps-week", "2"])
        self.assertTrue(snap_args.skip_snaps)
        self.assertEqual(snap_args.snaps_week, 2)
        with self.assertRaises(SystemExit):
            parse_args(["--csv", "x.csv", "--targets-week", "0"])
        with self.assertRaises(SystemExit):
            parse_args(["--csv", "x.csv", "--snaps-week", "0"])
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
        multi = parse_args(["--csv", "x.csv", "--n-lineups", "20"])
        self.assertEqual(multi.min_unique, 3)
        self.assertAlmostEqual(multi.max_exposure, 0.60)
        self.assertEqual(multi.diversity, "coverage")
        restore = parse_args(
            [
                "--csv",
                "x.csv",
                "--n-lineups",
                "20",
                "--min-unique",
                "2",
                "--max-exposure",
                "1",
                "--diversity",
                "chalk",
            ]
        )
        self.assertEqual(restore.min_unique, 2)
        self.assertEqual(restore.max_exposure, 1.0)
        self.assertEqual(restore.diversity, "chalk")
        alias = parse_args(["--csv", "x.csv", "--n-lineups", "5", "--coverage"])
        self.assertEqual(alias.diversity, "coverage")
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
        self.assertEqual(ok.min_unique, 3)
        self.assertAlmostEqual(ok.max_exposure, 0.60)
        with redirect_stderr(buf), self.assertRaises(SystemExit):
            parse_args(["--csv", "x.csv", "--max-exposure", "0"])
        with redirect_stderr(buf), self.assertRaises(SystemExit):
            parse_args(["--csv", "x.csv", "--max-exposure", "1.1"])
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


FIXTURE = Path(__file__).resolve().parent / "testdata" / "sim_layers.json"


def _std(xs: list[float]) -> float:
    n = len(xs)
    mu = sum(xs) / n
    return (sum((x - mu) ** 2 for x in xs) / (n - 1)) ** 0.5


class LayerInputTest(unittest.TestCase):
    def test_pooled_epa_var_uses_sums_over_precomputed(self):
        stat = TeamStat(
            team_fd="DET",
            side="offense",
            n=5,
            epa_sum=10.0,
            epa_sq_sum=30.0,
            epa_var=99.0,
        )
        # (30 - 10^2/5) / 4 = 2.5
        self.assertAlmostEqual(stat.epa_variance(), 2.5, places=6)

    def test_epa_var_field_when_sums_missing(self):
        stat = TeamStat(team_fd="DET", side="offense", epa_var=1.44, n=1)
        self.assertAlmostEqual(stat.epa_variance(), 1.44, places=6)

    def test_pass_rush_blend_when_overall_missing(self):
        # pass var = (200 - 0) / 99
        stat = TeamStat(
            team_fd="DET",
            side="offense",
            pass_n=100,
            pass_epa_sum=0.0,
            pass_epa_sq_sum=200.0,
            rush_n=50,
            rush_epa_var=4.0,
        )
        pass_var = 200.0 / 99.0
        want = (100 * pass_var + 50 * 4.0) / 150.0
        self.assertAlmostEqual(stat.epa_variance(), want, places=6)

    def test_missing_variance_keeps_fallback_sigmas(self):
        total_sigma, spread_sigma = game_sigmas(50.0, None, None)
        self.assertAlmostEqual(total_sigma, TOTAL_SIGMA_FRAC * 50.0, places=6)
        self.assertAlmostEqual(spread_sigma, SPREAD_SIGMA, places=6)

    def test_percent_rates_are_scaled(self):
        stat = team_stat_from_row(
            {"team_fd": "DET", "side": "offense", "pass_rate": 58, "proe": 3}
        )
        assert stat is not None
        self.assertAlmostEqual(stat.pass_rate, 0.58, places=6)
        self.assertAlmostEqual(stat.proe, 0.03, places=6)

    def test_neutral_rate_does_not_stack_proe(self):
        both = TeamStat(
            team_fd="DET",
            neutral_pass_rate=0.58,
            proe=0.10,
            pass_rate=0.70,
        )
        self.assertAlmostEqual(neutral_pass_rate_of(both), 0.58, places=6)
        stripped = TeamStat(team_fd="DET", pass_rate=0.64, proe=0.04)
        self.assertAlmostEqual(neutral_pass_rate_of(stripped), 0.60, places=6)
        self.assertAlmostEqual(neutral_pass_rate_of(None), 0.57, places=6)

    def test_trailing_teams_pass_more(self):
        neutral = 0.58
        trailing = scripted_pass_rate(neutral, margin=-14.0)
        leading = scripted_pass_rate(neutral, margin=14.0)
        self.assertGreater(trailing, neutral)
        self.assertLess(leading, neutral)
        self.assertAlmostEqual(trailing, 0.748, places=3)
        self.assertAlmostEqual(leading, 0.412, places=3)

    def test_dirichlet_shares_sum_to_one_and_compete(self):
        rng = random.Random(1)
        one = draw_simplex(rng, [0.4, 0.35, 0.25], 12.0)
        self.assertAlmostEqual(sum(one), 1.0, places=6)
        s1: list[float] = []
        s2: list[float] = []
        rng = random.Random(2)
        for _ in range(400):
            drawn = draw_simplex(rng, [0.4, 0.4, 0.2], 8.0)
            s1.append(drawn[0])
            s2.append(drawn[1])
        self.assertLess(pearson(s1, s2), -0.2)

    def test_rush_share_without_snaps_is_deterministic_weights(self):
        rb1 = _pl(pid="rb1", name="RB1", position="RB", depth_rank=1)
        rb2 = _pl(pid="rb2", name="RB2", position="RB", depth_rank=2)
        means, any_snaps = rush_share_means(
            [rb1, rb2], {"rb1": None, "rb2": None}
        )
        self.assertFalse(any_snaps)
        self.assertAlmostEqual(sum(means.values()), 1.0, places=6)
        self.assertGreater(means["rb1"], means["rb2"])
        again, _flag = rush_share_means([rb1, rb2], {})
        self.assertEqual(means, again)

    def test_rush_share_uses_offense_pct_when_present(self):
        rb1 = _pl(pid="rb1", name="RB1", position="RB", depth_rank=1)
        rb2 = _pl(pid="rb2", name="RB2", position="RB", depth_rank=1)
        means, any_snaps = rush_share_means(
            [rb1, rb2], {"rb1": 0.75, "rb2": 0.25}
        )
        self.assertTrue(any_snaps)
        self.assertAlmostEqual(means["rb1"], 0.75, places=6)
        self.assertAlmostEqual(means["rb2"], 0.25, places=6)


class LayerSimTest(unittest.TestCase):
    def test_empty_inputs_match_no_inputs(self):
        qb = _pl(pid="qb", name="QB", position="QB", total=50.0, spread=-6.0)
        wr = _pl(pid="wr", name="WR", position="WR", total=50.0, spread=-6.0)
        bare = simulate_games([qb, wr], n=300, seed=4)
        empty = simulate_games([qb, wr], n=300, seed=4, inputs=SimInputs())
        self.assertEqual(bare.draws, empty.draws)
        self.assertEqual(empty.opportunity_teams, frozenset())
        text = format_sim_diagnostic([qb, wr], bare)
        self.assertIn("role shares deterministic", text)
        self.assertIn(qb.name, text)
        self.assertIn("p10", text.splitlines()[0])

    def test_higher_epa_variance_widens_skill_draws(self):
        wr = _pl(
            pid="wr",
            name="Walk On WR",
            position="WR",
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=24.0,
            total=48.0,
            spread=0.0,
        )
        low = SimInputs(
            team_stats=(TeamStat(team_fd="DET", side="offense", epa_var=0.25),)
        )
        high = SimInputs(
            team_stats=(TeamStat(team_fd="DET", side="offense", epa_var=4.0),)
        )
        a = simulate_games([wr], n=2500, seed=1, inputs=low)
        b = simulate_games([wr], n=2500, seed=1, inputs=high)
        self.assertGreater(_std(list(b.draws["wr"])), _std(list(a.draws["wr"])) * 1.3)
        self.assertEqual(a.opportunity_teams, frozenset())

    def test_fixture_catchers_correlate_and_diagnostic_prints(self):
        inputs = load_sim_inputs(FIXTURE)
        self.assertEqual(inputs.snaps, ())
        self.assertGreater(len(inputs.targets), 0)
        self.assertGreater(len(inputs.team_stats), 0)
        qb = _pl(
            pid="qb",
            name="Jared Goff",
            position="QB",
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=28.0,
            implied_opp=22.0,
            total=50.0,
            spread=-6.0,
            salary=8000,
        )
        wr1 = _pl(
            pid="wr1",
            name="Amon-Ra St. Brown",
            position="WR",
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=28.0,
            total=50.0,
            spread=-6.0,
            depth_rank=1,
        )
        wr2 = _pl(
            pid="wr2",
            name="Jameson Williams",
            position="WR",
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=28.0,
            total=50.0,
            spread=-6.0,
            depth_rank=2,
        )
        rb = _pl(
            pid="rb",
            name="Jahmyr Gibbs",
            position="RB",
            team="DET",
            opponent="NO",
            game="NO@DET",
            implied_total=28.0,
            total=50.0,
            spread=-6.0,
            depth_rank=1,
        )
        pool = [qb, wr1, wr2, rb]
        gs = simulate_games(pool, n=3000, seed=1, inputs=inputs)
        rev = simulate_games(list(reversed(pool)), n=3000, seed=1, inputs=inputs)
        self.assertEqual(gs.draws["wr1"], rev.draws["wr1"])
        self.assertEqual(gs.opportunity_teams, frozenset({"DET"}))
        self.assertEqual(gs.by_pid["qb"].source, "game")
        self.assertEqual(gs.by_pid["wr1"].source, "game")
        text = format_sim_diagnostic(pool, gs)
        self.assertIn("Amon-Ra St. Brown", text)
        self.assertIn("Jameson Williams", text)
        self.assertIn("Jared Goff", text)
        self.assertIn("p10", text.splitlines()[0])
        self.assertIn("p50", text.splitlines()[0])
        self.assertIn("p90", text.splitlines()[0])
        self.assertIn("QB–WR mean r", text)
        self.assertIn("WR–WR mean r", text)
        self.assertIn("opportunity shares on: DET", text)
        for pl in pool:
            st = gs.by_pid[pl.pid]
            self.assertLess(st.p10, st.p50)
            self.assertLess(st.p50, st.p90)
        self.assertGreater(pearson(list(gs.draws["qb"]), list(gs.draws["wr1"])), 0.15)
        self.assertGreater(pearson(list(gs.draws["qb"]), list(gs.draws["wr2"])), 0.15)
        self.assertLess(pearson(list(gs.draws["wr1"]), list(gs.draws["wr2"])), -0.05)
        want = [p.objective for p in pool]
        mean_pool = apply_ilp_objective(pool, "mean", sim_by_pid=gs.by_pid)
        self.assertEqual([p.objective for p in mean_pool], want)
        floor_pool = apply_ilp_objective(pool, "floor", sim_by_pid=gs.by_pid)
        self.assertAlmostEqual(floor_pool[0].objective, gs.by_pid["qb"].p10, places=6)

    def test_kappa_from_fixture_weeks(self):
        inputs = load_sim_inputs(FIXTURE)
        series = []
        for name in ("Amon-Ra St. Brown", "Jameson Williams"):
            series.append(
                [
                    float(row.target_share)
                    for row in inputs.targets
                    if row.player_name == name and row.target_share is not None
                ]
            )
        kappa = share_kappa(series)
        self.assertGreater(kappa, 2.0)
        self.assertLess(kappa, 20.0)

    def test_unmatched_history_stays_on_role_share(self):
        inputs = load_sim_inputs(FIXTURE)
        qb = _pl(pid="qb", name="Nobody QB", position="QB", total=50.0, spread=-6.0)
        wr = _pl(pid="wr", name="Nobody WR", position="WR", total=50.0, spread=-6.0)
        bare = simulate_games([qb, wr], n=200, seed=1)
        named = simulate_games([qb, wr], n=200, seed=1, inputs=inputs)
        # Team stats still scale dispersion, so draws need not match.
        # No name hit → no Dirichlet, teammates stay lockstep.
        self.assertEqual(named.opportunity_teams, frozenset())
        self.assertGreater(
            pearson(list(named.draws["qb"]), list(named.draws["wr"])), 0.9
        )
        self.assertGreater(
            pearson(list(bare.draws["qb"]), list(bare.draws["wr"])), 0.9
        )

    def test_snaps_shift_rb_rush_score(self):
        def weeks(name: str, share: float, snaps: float) -> tuple[list[dict], list[dict]]:
            tgt = []
            snap = []
            for week in (1, 2, 3):
                tgt.append(
                    {
                        "season": 2025,
                        "week": week,
                        "position": "RB",
                        "player_name": name,
                        "team_fd": "DET",
                        "targets": 4,
                        "target_share": share,
                        "team_targets": 32,
                        "team_pass_attempts": 34,
                    }
                )
                snap.append(
                    {
                        "season": 2025,
                        "week": week,
                        "position": "RB",
                        "player_name": name,
                        "team_fd": "DET",
                        "offense_pct": snaps,
                    }
                )
            return tgt, snap

        t1, s1 = weeks("Lead Back", 0.12, 80)
        t2, s2 = weeks("Change Back", 0.12, 20)
        inputs = sim_inputs_from_records(
            team_stats=[
                {
                    "team_fd": "DET",
                    "side": "offense",
                    "neutral_pass_rate": 0.52,
                    "n": 200,
                    "epa_sum": 20,
                    "epa_sq_sum": 280,
                }
            ],
            targets=t1 + t2,
            snaps=s1 + s2,
        )
        self.assertAlmostEqual(inputs.snaps[0].offense_pct, 0.80, places=6)
        lead = _pl(
            pid="lead",
            name="Lead Back",
            position="RB",
            depth_rank=1,
            team="DET",
            game="NO@DET",
            total=46.0,
            spread=0.0,
            implied_total=23.0,
        )
        change = _pl(
            pid="change",
            name="Change Back",
            position="RB",
            depth_rank=2,
            team="DET",
            game="NO@DET",
            total=46.0,
            spread=0.0,
            implied_total=23.0,
        )
        gs = simulate_games([lead, change], n=400, seed=1, inputs=inputs)
        self.assertGreater(gs.by_pid["lead"].mean, gs.by_pid["change"].mean)

    def test_sim_modules_do_not_fetch(self):
        import inspect

        import nfl.sim as sim
        import nfl.sim_efficiency as eff
        import nfl.sim_inputs as inputs

        for mod in (sim, eff, inputs):
            src = inspect.getsource(mod)
            self.assertNotIn("http_json", src)
            self.assertNotIn("supabase", src)
            self.assertNotIn("urllib", src)
            self.assertNotIn("requests.", src)


def _corr_mean(text: str, label: str) -> float:
    prefix = label + " = "
    for line in text.splitlines():
        if line.startswith(prefix):
            return float(line.split("=", 1)[1].split()[0])
    raise AssertionError(f"missing {label}")


def _week_row(name: str, week: int, share: float | None, *, position: str = "WR") -> dict:
    targets = 0.0 if not share else round(30.0 * share, 2)
    return {
        "season": 2025,
        "week": week,
        "position": position,
        "player_name": name,
        "team_fd": "ARI",
        "targets": targets,
        "target_share": share,
        "team_targets": 30,
        "team_pass_attempts": 34,
    }


class _RecordingYards:
    """Scores rec_yd only, so a share of 1 makes the QB equal his WR."""

    def receiving_line(self, rng, position: str, targets: float) -> ReceivingLine:
        del rng, position
        t = max(0.0, float(targets))
        return ReceivingLine(targets=t, receptions=t, rec_yd=t * 10.0, rec_td=0.0)

    def points(self, rng, player: Player, opportunities: OpportunityCount) -> float:
        del rng
        if (player.position or "").upper() == "QB":
            lines = opportunities.team_receiving or ()
            return sum(line.rec_yd for line in lines)
        if opportunities.receiving is None:
            return 0.0
        return opportunities.receiving.rec_yd


class SampledBonusTest(unittest.TestCase):
    def test_bonus_fires_per_draw_not_on_the_mean(self):
        """Expected pass yards sit under 300. Some games still clear it."""
        from nfl.rules import skill_fd_points
        from nfl.sim_efficiency import sample_yards

        att = 280.0 / PASS_YPA  # conditional mean is 280, under the bonus
        mean_yds = att * PASS_YPA
        qb = _pl(pid="qb", name="QB", position="QB", salary=8000)
        eff = PlaceholderEfficiency()
        bonus_games = 0
        quiet_games = 0
        for i in range(400):
            rng = random.Random(i)
            pts = eff.points(
                rng, qb, OpportunityCount(pass_attempts=att, rushes=0.0)
            )
            yds = sample_yards(random.Random(i), mean_yds)
            quiet = skill_fd_points(
                pass_yd=yds,
                pass_td=att * 0.045,
                interceptions=att * INT_RATE,
            )
            # Recompute with the same seed the points() call used.
            self.assertAlmostEqual(pts, max(0.0, quiet), places=5)
            if yds >= 300.0:
                bonus_games += 1
                self.assertGreaterEqual(pts, quiet - 1e-6)
                self.assertAlmostEqual(
                    skill_fd_points(pass_yd=yds)
                    - skill_fd_points(pass_yd=0)
                    - yds * 0.04,
                    3.0,
                    places=6,
                )
            else:
                quiet_games += 1
                self.assertAlmostEqual(
                    skill_fd_points(pass_yd=yds) - yds * 0.04, 0.0, places=6
                )
        self.assertGreater(bonus_games, 30)
        self.assertGreater(quiet_games, 30)

    def test_attached_line_bonus_is_the_realized_yards(self):
        from nfl.rules import skill_fd_points

        eff = PlaceholderEfficiency()
        wr = _pl(pid="wr", name="WR", position="WR")
        under = ReceivingLine(targets=8, receptions=5, rec_yd=99.9, rec_td=0.4)
        over = ReceivingLine(targets=8, receptions=5, rec_yd=100.0, rec_td=0.4)
        pts_under = eff.points(
            random.Random(0),
            wr,
            OpportunityCount(targets=8, rushes=0.0, receiving=under),
        )
        pts_over = eff.points(
            random.Random(0),
            wr,
            OpportunityCount(targets=8, rushes=0.0, receiving=over),
        )
        self.assertAlmostEqual(
            pts_over - pts_under,
            skill_fd_points(rec_yd=100, receptions=5, rec_td=0.4)
            - skill_fd_points(rec_yd=99.9, receptions=5, rec_td=0.4),
            places=5,
        )
        self.assertAlmostEqual(pts_over - pts_under, 0.01 + 3.0, places=4)


class ValueReportTest(unittest.TestCase):
    def test_cash_and_gpp_flags_use_salary_multiples(self):
        from nfl.projections import format_value_report, value_flags

        wr = _pl(pid="wr", name="Wideout", position="WR", salary=8000, depth_rank=1)
        # Board multiple is week1, not a hard-coded 16. Flags below use explicit points.
        both = value_flags(
            "WR", proj=16.0, salary=8000, p10=16.0, mean=18.0, p90=24.0
        )
        self.assertAlmostEqual(both["multiple"], 2.0, places=3)
        self.assertTrue(both["cash_ok"])
        self.assertTrue(both["gpp_ok"])
        self.assertTrue(both["gpp_3x"])
        thin_floor = value_flags(
            "WR", proj=16.0, salary=8000, p10=10.0, mean=18.0, p90=19.0
        )
        self.assertFalse(thin_floor["cash_ok"])
        self.assertFalse(thin_floor["gpp_ok"])
        qb = value_flags("QB", proj=16.8, salary=8000)
        self.assertAlmostEqual(qb["multiple"], 2.1, places=3)
        self.assertFalse(qb["cash_ok"])
        te = value_flags("TE", proj=6.0, salary=4000)
        self.assertAlmostEqual(te["multiple"], 1.5, places=3)
        self.assertTrue(te["cash_ok"])
        dst = value_flags("D", proj=4.0, salary=3500)
        self.assertTrue(dst["cash_ok"])
        text = format_value_report([wr])
        self.assertIn("Wideout", text)
        self.assertIn("cash_ok", text)
        self.assertIn("DEF", text)


class ProjectionSourceTest(unittest.TestCase):
    def test_sim_mean_replaces_ilp_objective_board_does_not(self):
        pl = _pl(
            pid="wr",
            name="Alpha WR",
            position="WR",
            team="ARI",
            implied_total=24.0,
            depth_rank=1,
        )
        board = pl.objective
        stats = {
            pl.pid: SimStats(
                mean=18.5,
                p10=9.0,
                p50=17.0,
                p90=29.0,
                n=100,
                source="game",
            )
        }
        kept = apply_ilp_objective(
            [pl], "mean", sim_by_pid=stats, projection_source="board"
        )
        self.assertAlmostEqual(kept[0].objective, board, places=6)
        swapped = apply_ilp_objective(
            [pl], "mean", sim_by_pid=stats, projection_source="sim"
        )
        self.assertAlmostEqual(swapped[0].objective, 18.5, places=6)
        self.assertAlmostEqual(pl.objective, board, places=6)
        missing = apply_ilp_objective(
            [pl], "mean", sim_by_pid={}, projection_source="sim"
        )
        self.assertAlmostEqual(missing[0].objective, board, places=6)
        floor = apply_ilp_objective(
            [pl], "floor", sim_by_pid=stats, projection_source="sim"
        )
        ceil = apply_ilp_objective(
            [pl], "ceiling", sim_by_pid=stats, projection_source="board"
        )
        self.assertAlmostEqual(floor[0].objective, 9.0, places=6)
        self.assertAlmostEqual(ceil[0].objective, 29.0, places=6)

    def test_diagnostic_lists_largest_board_sim_gaps_first(self):
        big = _pl(
            pid="big",
            name="Big Mover",
            position="WR",
            team="ARI",
            implied_total=22.0,
            depth_rank=1,
        )
        small = _pl(
            pid="small",
            name="Small Mover",
            position="RB",
            team="ARI",
            implied_total=22.0,
            depth_rank=1,
        )
        quiet = _pl(
            pid="quiet",
            name="Quiet",
            position="TE",
            team="ARI",
            implied_total=22.0,
            depth_rank=1,
        )
        small_board = float(small.objective or 0.0)
        gs = GameSim(
            by_pid={
                "big": SimStats(
                    mean=30.0, p10=12, p50=28, p90=40, n=50, source="game"
                ),
                "small": SimStats(
                    mean=small_board + 0.4,
                    p10=1,
                    p50=small_board,
                    p90=8,
                    n=50,
                    source="game",
                ),
                "quiet": SimStats(
                    mean=float(quiet.objective or 0.0),
                    p10=1,
                    p50=2,
                    p90=3,
                    n=50,
                    source="game",
                ),
            },
            draws={},
        )
        text = format_board_vs_sim(
            [quiet, small, big], gs, projection_source="board", limit=2
        )
        self.assertLess(text.find("Big Mover"), text.find("Small Mover"))
        self.assertNotIn("Quiet", text)
        self.assertIn("ILP mean uses board (week1_score)", text)
        sim_text = format_board_vs_sim(
            [big], gs, projection_source="sim", limit=1
        )
        self.assertIn(
            "ILP mean uses sim mean; board stays week1_score for comparison",
            sim_text,
        )
        full = format_sim_diagnostic([big, small], gs)
        self.assertIn("board vs sim", full)
        self.assertIn("Big Mover", full)


class LiveShapeTest(unittest.TestCase):
    """Bugs from the Sep 27 gangstash slate: signs, duplicate QBs, zero-fill."""

    def test_passing_qb_is_depth_one_or_the_pass_prop(self):
        lone = [
            _pl(pid="a", name="Starter", position="QB", depth_rank=1),
            _pl(pid="b", name="Backup", position="QB", depth_rank=2),
            _pl(pid="c", name="Third", position="QB", depth_rank=3),
        ]
        self.assertEqual(passing_qb(lone).pid, "a")
        propped = [
            _pl(pid="a", name="A", position="QB", depth_rank=1, salary=7000),
            _pl(
                pid="b",
                name="B",
                position="QB",
                depth_rank=1,
                salary=6000,
                prop_pass_yds=245.5,
            ),
        ]
        self.assertEqual(passing_qb(propped).pid, "b")
        salary = [
            _pl(pid="a", name="A", position="QB", depth_rank=1, salary=7000),
            _pl(pid="b", name="B", position="QB", depth_rank=1, salary=8000),
        ]
        self.assertEqual(passing_qb(salary).pid, "b")
        prop_only = [
            _pl(pid="a", name="A", position="QB", depth_rank=2, salary=8000),
            _pl(
                pid="b",
                name="B",
                position="QB",
                depth_rank=3,
                salary=5000,
                prop_pass_tds=1.6,
            ),
        ]
        self.assertEqual(passing_qb(prop_only).pid, "b")
        by_depth = [
            _pl(pid="a", name="A", position="QB", depth_rank=3, salary=9000),
            _pl(pid="b", name="B", position="QB", depth_rank=2, salary=4000),
        ]
        self.assertEqual(passing_qb(by_depth).pid, "b")
        inactive = [
            _pl(pid="a", name="Out", position="QB", depth_rank=1, injury="D"),
            _pl(pid="b", name="Next", position="QB", depth_rank=2, injury=""),
        ]
        self.assertEqual(passing_qb(inactive).pid, "b")

    def test_qb_pass_yards_are_the_sum_of_receiving_lines(self):
        eff = PlaceholderEfficiency()
        rng = random.Random(0)
        wr = eff.receiving_line(rng, "WR", 10)
        te = eff.receiving_line(rng, "TE", 4)
        other = eff.receiving_line(rng, "WR", 6)
        qb = _pl(pid="qb", name="QB", position="QB")
        shared = OpportunityCount(
            pass_attempts=22.0,
            rushes=0.0,
            team_receiving=(wr, te, other),
        )
        pts = eff.points(rng, qb, shared)
        from nfl.rules import skill_fd_points

        pass_yd = wr.rec_yd + te.rec_yd + other.rec_yd
        pass_td = wr.rec_td + te.rec_td + other.rec_td
        expected = skill_fd_points(
            pass_yd=pass_yd,
            pass_td=pass_td,
            interceptions=22.0 * INT_RATE,
        )
        self.assertAlmostEqual(pts, max(0.0, expected), places=6)

        class _AtMean:
            def gauss(self, mu: float, sigma: float) -> float:
                return mu

        legacy = eff.points(
            _AtMean(), qb, OpportunityCount(pass_attempts=22.0, rushes=0.0)
        )
        self.assertAlmostEqual(
            legacy,
            skill_fd_points(
                pass_yd=22.0 * PASS_YPA,
                pass_td=22.0 * 0.045,
                interceptions=22.0 * INT_RATE,
            ),
            places=4,
        )
        self.assertNotAlmostEqual(pts, legacy, places=2)

        inputs = sim_inputs_from_records(
            targets=[
                _week_row("Only WR", week, 1.0) for week in (1, 2)
            ],
        )
        wr_only = _pl(
            pid="wr",
            name="Only WR",
            position="WR",
            team="ARI",
            opponent="SEA",
            game="SEA@ARI",
            total=47.0,
            spread=-3.0,
            implied_total=25.0,
            depth_rank=1,
        )
        starter = _pl(
            pid="qb",
            name="Starter",
            position="QB",
            team="ARI",
            opponent="SEA",
            game="SEA@ARI",
            total=47.0,
            spread=-3.0,
            implied_total=25.0,
            depth_rank=1,
            salary=8000,
        )
        gs = simulate_games(
            [starter, wr_only],
            n=40,
            seed=1,
            inputs=inputs,
            efficiency=_RecordingYards(),
        )
        self.assertEqual(gs.draws["qb"], gs.draws["wr"])
        self.assertGreater(gs.by_pid["qb"].mean, 0.0)

    def test_backup_qbs_are_zero_when_a_starter_exists(self):
        targets = []
        for name, shares in (
            ("Michael Wilson", (0.22, 0.18, 0.24)),
            ("Marvin Harrison", (0.28, 0.0, 0.0)),
        ):
            for week, share in enumerate(shares, start=1):
                targets.append(_week_row(name, week, share))
        inputs = sim_inputs_from_records(targets=targets)
        common = dict(
            team="ARI",
            opponent="SEA",
            game="SEA@ARI",
            total=47.0,
            spread=-3.0,
            implied_total=25.0,
        )
        starter = _pl(
            pid="qb1",
            name="Jacoby Brissett",
            position="QB",
            depth_rank=1,
            salary=7500,
            **common,
        )
        beck = _pl(
            pid="qb2",
            name="Carson Beck",
            position="QB",
            depth_rank=2,
            salary=6000,
            **common,
        )
        minshew = _pl(
            pid="qb3",
            name="Gardner Minshew II",
            position="QB",
            depth_rank=3,
            salary=5500,
            **common,
        )
        wilson = _pl(
            pid="wr1",
            name="Michael Wilson",
            position="WR",
            depth_rank=2,
            **common,
        )
        pool = [starter, beck, minshew, wilson]
        gs = simulate_games(pool, n=400, seed=1, inputs=inputs)
        self.assertGreater(gs.by_pid["qb1"].p50, gs.by_pid["qb2"].p90)
        self.assertEqual(set(gs.draws["qb2"]), {0.0})
        self.assertEqual(set(gs.draws["qb3"]), {0.0})
        self.assertNotEqual(gs.draws["qb1"], gs.draws["qb2"])
        self.assertGreater(gs.by_pid["qb1"].p10, 1.0)

    def test_inactive_players_lose_target_and_rush_share(self):
        from dataclasses import replace

        from nfl.sim_efficiency import DataEfficiency
        from nfl.sim_inputs import PlayerWeek

        targets = []
        for week in (1, 2, 3):
            targets.append(_week_row("A.J. Brown", week, 0.30))
            targets.append(_week_row("Kayshon Boutte", week, 0.18))
        base = sim_inputs_from_records(targets=targets)
        history = PlayerWeek(
            season=2025,
            week=1,
            position="WR",
            player_name="A.J. Brown",
            team_fd="ARI",
            targets=8,
            receptions=6,
            receiving_yards=90,
            receiving_tds=1,
        )
        inputs = replace(base, player_weeks=(history,))
        common = dict(
            team="ARI",
            opponent="SEA",
            game="SEA@ARI",
            total=47.0,
            spread=-3.0,
            implied_total=25.0,
        )
        healthy = _pl(pid="aj", name="A.J. Brown", position="WR", depth_rank=1, **common)
        ir = _pl(
            pid="aj",
            name="A.J. Brown",
            position="WR",
            depth_rank=1,
            injury="IR",
            **common,
        )
        mate = _pl(
            pid="kb",
            name="Kayshon Boutte",
            position="WR",
            depth_rank=2,
            **common,
        )
        qb = _pl(
            pid="qb",
            name="Jacoby Brissett",
            position="QB",
            depth_rank=1,
            salary=7500,
            **common,
        )
        both = simulate_games(
            [qb, healthy, mate],
            n=60,
            seed=1,
            inputs=inputs,
            efficiency=DataEfficiency(inputs, before_week=4),
        )
        out = simulate_games(
            [qb, ir, mate],
            n=60,
            seed=1,
            inputs=inputs,
            efficiency=DataEfficiency(inputs, before_week=4),
        )
        self.assertLess(out.by_pid["aj"].mean, 0.05)
        self.assertGreater(out.by_pid["kb"].mean, both.by_pid["kb"].mean + 0.5)

        rb_targets = [_week_row("Michael Wilson", week, 0.22) for week in (1, 2, 3)]
        rb_inputs = sim_inputs_from_records(targets=rb_targets)
        wr = _pl(pid="wr", name="Michael Wilson", position="WR", depth_rank=1, **common)
        lead = _pl(pid="rb1", name="James Conner", position="RB", depth_rank=1, **common)
        hurt = _pl(
            pid="rb2",
            name="Trey Benson",
            position="RB",
            depth_rank=2,
            injury="O",
            **common,
        )
        rushes = simulate_games(
            [qb, wr, lead, hurt],
            n=40,
            seed=2,
            inputs=rb_inputs,
        )
        self.assertLess(rushes.by_pid["rb2"].mean, 0.05)
        self.assertGreater(rushes.by_pid["rb1"].mean, 1.0)

    def test_zero_filled_weeks_do_not_dilute_share(self):
        star = [
            TargetWeek(
                season=2025,
                week=1,
                position="WR",
                player_name="Marvin Harrison",
                team_fd="ARI",
                targets=8.4,
                target_share=0.28,
                team_targets=30,
            ),
            TargetWeek(
                season=2025,
                week=2,
                position="WR",
                player_name="Marvin Harrison",
                team_fd="ARI",
                targets=0,
                target_share=0.0,
                team_targets=30,
            ),
            TargetWeek(
                season=2025,
                week=3,
                position="WR",
                player_name="Marvin Harrison",
                team_fd="ARI",
                targets=0,
                target_share=None,
                team_targets=30,
            ),
        ]
        self.assertAlmostEqual(mean_target_share(star), 0.28, places=6)
        played = [
            SnapWeek(
                season=2025,
                week=2,
                position="WR",
                player_name="Marvin Harrison",
                team_fd="ARI",
                offense_pct=0.80,
            )
        ]
        self.assertAlmostEqual(mean_target_share(star, played), 0.14, places=6)
        dnp = [
            SnapWeek(
                season=2025,
                week=2,
                position="WR",
                player_name="Marvin Harrison",
                team_fd="ARI",
                offense_pct=0.0,
            )
        ]
        self.assertAlmostEqual(mean_target_share(star, dnp), 0.28, places=6)

        targets = []
        for week, share in enumerate((0.28, 0.0, 0.0), start=1):
            targets.append(_week_row("Marvin Harrison", week, share))
        for week in (1, 2, 3):
            targets.append(_week_row("Kendrick Bourne", week, 0.12))
        inputs = sim_inputs_from_records(targets=targets)
        common = dict(
            team="ARI",
            opponent="SEA",
            game="SEA@ARI",
            total=47.0,
            spread=-3.0,
            implied_total=25.0,
        )
        mhj = _pl(
            pid="mhj",
            name="Marvin Harrison Jr.",
            position="WR",
            depth_rank=1,
            **common,
        )
        bourne = _pl(
            pid="bourne",
            name="Kendrick Bourne",
            position="WR",
            depth_rank=3,
            **common,
        )
        gs = simulate_games([mhj, bourne], n=600, seed=2, inputs=inputs)
        self.assertGreater(gs.by_pid["mhj"].p50, gs.by_pid["bourne"].p50)
        self.assertGreater(gs.by_pid["mhj"].mean, gs.by_pid["bourne"].mean)

    def test_starter_correlations_keep_sign_when_bench_flips_the_mean(self):
        targets = []
        # Shares swap hard, same shape as nfl/testdata/sim_layers.json, so
        # Dirichlet competition beats the shared pass-volume factor.
        for week, share in enumerate((0.45, 0.12, 0.40, 0.15), start=1):
            targets.append(_week_row("Amon-Ra St. Brown", week, share))
        for week, share in enumerate((0.12, 0.42, 0.14, 0.38), start=1):
            targets.append(_week_row("Jameson Williams", week, share))
        for week, share in enumerate((0.18, 0.10, 0.22, 0.12), start=1):
            targets.append(
                _week_row("Sam LaPorta", week, share, position="TE")
            )
        inputs = sim_inputs_from_records(
            team_stats=[
                {
                    "team_fd": "ARI",
                    "side": "offense",
                    "neutral_pass_rate": 0.57,
                    "n": 400,
                    "epa_sum": 40,
                    "epa_sq_sum": 560,
                }
            ],
            targets=targets,
        )
        common = dict(
            team="ARI",
            opponent="SEA",
            game="SEA@ARI",
            total=48.0,
            spread=-2.0,
            implied_total=25.0,
            implied_opp=23.0,
        )
        starter = _pl(
            pid="qb",
            name="Starter QB",
            position="QB",
            depth_rank=1,
            salary=8000,
            **common,
        )
        backup = _pl(
            pid="qb2",
            name="Backup QB",
            position="QB",
            depth_rank=2,
            salary=5000,
            **common,
        )
        wr1 = _pl(
            pid="wr1",
            name="Amon-Ra St. Brown",
            position="WR",
            depth_rank=1,
            **common,
        )
        wr2 = _pl(
            pid="wr2",
            name="Jameson Williams",
            position="WR",
            depth_rank=2,
            **common,
        )
        te = _pl(
            pid="te",
            name="Sam LaPorta",
            position="TE",
            depth_rank=1,
            **common,
        )
        bench = [
            _pl(
                pid=f"bench{i}",
                name=f"Bench WR {i}",
                position="WR",
                depth_rank=4 + (i % 3),
                salary=4000,
                **common,
            )
            for i in range(10)
        ]
        pool = [starter, backup, wr1, wr2, te, *bench]
        gs = simulate_games(pool, n=2500, seed=1, inputs=inputs)
        text = format_sim_diagnostic(pool, gs)
        self.assertGreater(_corr_mean(text, "QB–WR starters mean r"), 0.15)
        self.assertLess(_corr_mean(text, "WR–WR starters mean r"), -0.05)
        self.assertGreater(_corr_mean(text, "QB–TE starters mean r"), 0.10)
        self.assertGreater(
            pearson(list(gs.draws["qb"]), list(gs.draws["wr1"])), 0.15
        )
        self.assertGreater(
            pearson(list(gs.draws["qb"]), list(gs.draws["wr2"])), 0.15
        )
        self.assertLess(
            pearson(list(gs.draws["wr1"]), list(gs.draws["wr2"])), -0.05
        )
        # Bench WRs track team points, which move against pass rate.
        self.assertLess(
            _corr_mean(text, "QB–WR mean r"),
            _corr_mean(text, "QB–WR starters mean r"),
        )
        self.assertGreater(
            _corr_mean(text, "WR–WR mean r"),
            _corr_mean(text, "WR–WR starters mean r"),
        )
        self.assertEqual(set(gs.draws["qb2"]), {0.0})
        self.assertIn("starters: depth-1 QB, WR depth 1–3, TE depth 1", text)


def _skill_rows(name: str, pos: str, share: float, team: str) -> list[dict]:
    rows = []
    for week in (1, 2, 3):
        rows.append(
            {
                "season": 2026,
                "week": week,
                "position": pos,
                "player_name": name,
                "team_fd": team,
                "targets": round(30.0 * share, 2),
                "target_share": share,
                "team_targets": 30,
                "team_pass_attempts": 34,
            }
        )
    return rows


def _snap_rows(name: str, pct: float, team: str) -> list[dict]:
    return [
        {
            "season": 2026,
            "week": week,
            "position": "RB",
            "player_name": name,
            "team_fd": team,
            "offense_pct": pct,
        }
        for week in (1, 2, 3)
    ]


def _carry_rows(name: str, carries: float, team: str) -> list[dict]:
    return [
        {
            "season": 2026,
            "week": week,
            "position": "RB",
            "player_name": name,
            "team_fd": team,
            "carries": carries,
        }
        for week in (1, 2, 3)
    ]


class LevelAnchorTest(unittest.TestCase):
    def _offense(self, team: str, neutral: float = 0.57) -> dict:
        return {
            "team_fd": team,
            "side": "offense",
            "neutral_pass_rate": neutral,
            "pass_n": 34,
            "rush_n": 26,
            "n": 60,
            "epa_sum": 4,
            "epa_sq_sum": 80,
        }

    def test_sim_qb_means_correlate_with_implied_totals(self):
        """Neutral and scripted slates: starter sim means rise with the total."""
        specs = [
            ("LOW", "OPP", 18.0, 18.0, "OPP@LOW"),
            ("MID", "OPP2", 24.0, 24.0, "OPP2@MID"),
            ("HIGH", "OPP3", 30.0, 30.0, "OPP3@HIGH"),
            ("FAV", "DOG", 29.0, 17.0, "DOG@FAV"),
            ("DOG", "FAV", 17.0, 29.0, "DOG@FAV"),
        ]
        pool = []
        targets: list[dict] = []
        stats = []
        for team, opp, impl, opp_impl, game in specs:
            if any(row["team_fd"] == team for row in stats):
                continue
            stats.append(self._offense(team))
            targets.extend(_skill_rows(f"WR {team}", "WR", 0.32, team))
            total = impl + opp_impl
            home_spread = opp_impl - impl
            pool.append(
                _pl(
                    pid=f"qb-{team}",
                    name=f"QB {team}",
                    position="QB",
                    team=team,
                    opponent=opp,
                    game=game,
                    implied_total=impl,
                    implied_opp=opp_impl,
                    total=total,
                    spread=home_spread,
                    depth_rank=1,
                    salary=8000,
                )
            )
            pool.append(
                _pl(
                    pid=f"wr-{team}",
                    name=f"WR {team}",
                    position="WR",
                    team=team,
                    opponent=opp,
                    game=game,
                    implied_total=impl,
                    implied_opp=opp_impl,
                    total=total,
                    spread=home_spread,
                    depth_rank=1,
                )
            )
        inputs = sim_inputs_from_records(team_stats=stats, targets=targets)
        gs = simulate_games(pool, n=500, seed=1, inputs=inputs)
        means = [gs.by_pid[f"qb-{team}"].mean for team, *_rest in specs]
        implied = [impl for _team, _opp, impl, _oi, _game in specs]
        self.assertGreater(pearson(means, implied), 0.3)
        self.assertGreater(gs.by_pid["qb-HIGH"].mean, gs.by_pid["qb-LOW"].mean)
        self.assertGreater(gs.by_pid["qb-MID"].mean, gs.by_pid["qb-LOW"].mean)

    def test_pass_prop_sets_the_yard_anchor(self):
        from nfl.sim import pass_td_anchor, pass_yard_anchor

        self.assertAlmostEqual(pass_yard_anchor(22.0, 0.57), 22.0 * 0.57 * 17.5, places=4)
        self.assertEqual(pass_yard_anchor(18.0, 0.65, 280.0), 280.0)
        self.assertEqual(pass_td_anchor(24.0, 0.50, 1.7), 1.7)
        targets = _skill_rows("Only WR", "WR", 1.0, "DET")
        inputs = sim_inputs_from_records(
            team_stats=[self._offense("DET")],
            targets=targets,
        )
        common = dict(
            team="DET",
            opponent="NO",
            game="NO@DET",
            total=44.0,
            spread=0.0,
            implied_total=22.0,
            implied_opp=22.0,
            depth_rank=1,
        )
        bare = _pl(pid="qb", name="Bare", position="QB", salary=7000, **common)
        propped = _pl(
            pid="qb",
            name="Propped",
            position="QB",
            salary=7000,
            prop_pass_yds=310.0,
            prop_pass_tds=2.4,
            **common,
        )
        wr = _pl(pid="wr", name="Only WR", position="WR", **common)
        bare_gs = simulate_games([bare, wr], n=200, seed=1, inputs=inputs)
        prop_gs = simulate_games([propped, wr], n=200, seed=1, inputs=inputs)
        self.assertGreater(prop_gs.by_pid["qb"].mean, bare_gs.by_pid["qb"].mean + 2.0)

    def test_rb_volume_follows_snaps_carries_and_rush_props(self):
        targets = (
            _skill_rows("Bellcow", "RB", 0.16, "ATL")
            + _skill_rows("Committee", "RB", 0.06, "ATL")
            + _skill_rows("Wideout", "WR", 0.28, "ATL")
        )
        snaps = _snap_rows("Bellcow", 0.68, "ATL") + _snap_rows("Committee", 0.28, "ATL")
        carries = _carry_rows("Bellcow", 14, "ATL") + _carry_rows("Committee", 10, "ATL")
        inputs = sim_inputs_from_records(
            team_stats=[self._offense("ATL", neutral=0.55)],
            targets=targets,
            snaps=snaps,
            carries=carries,
        )
        common = dict(
            team="ATL",
            opponent="CAR",
            game="CAR@ATL",
            total=46.0,
            spread=-4.0,
            implied_total=25.0,
            implied_opp=21.0,
        )
        lead = _pl(pid="rb1", name="Bellcow", position="RB", depth_rank=1, **common)
        change = _pl(pid="rb2", name="Committee", position="RB", depth_rank=2, **common)
        wr = _pl(pid="wr", name="Wideout", position="WR", depth_rank=1, **common)
        qb = _pl(pid="qb", name="QB", position="QB", depth_rank=1, salary=7500, **common)
        gs = simulate_games([qb, lead, change, wr], n=400, seed=1, inputs=inputs)
        self.assertGreater(gs.by_pid["rb1"].mean, gs.by_pid["rb2"].mean)
        self.assertLess(gs.by_pid["rb1"].mean, 18.0)
        self.assertGreater(gs.by_pid["rb1"].mean, 4.0)

        solo_inputs = sim_inputs_from_records(
            team_stats=[self._offense("ATL", neutral=0.55)],
            targets=_skill_rows("Bellcow", "RB", 0.16, "ATL")
            + _skill_rows("Wideout", "WR", 0.28, "ATL"),
            snaps=_snap_rows("Bellcow", 0.80, "ATL"),
        )
        solo = simulate_games([qb, lead, wr], n=400, seed=1, inputs=solo_inputs)
        self.assertLess(gs.by_pid["rb1"].mean, solo.by_pid["rb1"].mean)

        propped = _pl(
            pid="rb1",
            name="Bellcow",
            position="RB",
            depth_rank=1,
            prop_rush_yds=70.0,
            **common,
        )
        prop_gs = simulate_games(
            [qb, propped, change, wr], n=400, seed=1, inputs=inputs
        )
        self.assertLess(prop_gs.by_pid["rb1"].mean, 16.0)
        self.assertLess(prop_gs.by_pid["rb1"].mean, solo.by_pid["rb1"].mean)


class LowTotalQbTest(unittest.TestCase):
    def test_starter_without_catcher_history_clears_the_role_share(self):
        """Implied 17.5 used to be team points × 0.50 (about 8.8, p90 about 11.4)."""
        common = dict(
            team="MIA",
            opponent="NE",
            game="NE@MIA",
            implied_total=17.5,
            implied_opp=24.0,
            total=41.5,
            spread=6.5,
        )
        qb = _pl(
            pid="willis",
            name="Malik Willis",
            position="QB",
            salary=7000,
            depth_rank=1,
            **common,
        )
        backup = _pl(
            pid="backup",
            name="Backup QB",
            position="QB",
            salary=5000,
            depth_rank=2,
            **common,
        )
        wr = _pl(pid="wr", name="MIA WR", position="WR", depth_rank=1, **common)
        role_share = 17.5 * POS_FD_SHARE["QB"]
        self.assertAlmostEqual(role_share, 8.75, places=2)
        gs = simulate_games([qb, backup, wr], n=800, seed=1)
        self.assertGreater(gs.by_pid["willis"].mean, 12.0)
        self.assertLess(gs.by_pid["willis"].mean, 17.0)
        self.assertGreater(gs.by_pid["willis"].p90, role_share + 2.0)
        self.assertLess(gs.by_pid["backup"].mean, 0.5)
        self.assertGreater(
            pearson(list(gs.draws["willis"]), list(gs.draws["wr"])), 0.9
        )

    def test_starter_with_catcher_history_clears_the_role_share(self):
        """Receiver target history used to leave the QB on team points × 0.50.

        Implied 17.5 was about 8.75 mean and p90 about 11.4. The opportunity
        path keeps the implied-total pass anchor and a rush floor.
        """
        common = dict(
            team="MIA",
            opponent="NE",
            game="NE@MIA",
            implied_total=17.5,
            implied_opp=24.0,
            total=41.5,
            spread=6.5,
        )
        qb = _pl(
            pid="willis",
            name="Malik Willis",
            position="QB",
            salary=7000,
            depth_rank=1,
            **common,
        )
        wr = _pl(
            pid="wr",
            name="Jaylen Waddle",
            position="WR",
            depth_rank=1,
            **common,
        )
        inputs = sim_inputs_from_records(
            targets=_skill_rows("Jaylen Waddle", "WR", 0.28, "MIA"),
        )
        role_share = 17.5 * POS_FD_SHARE["QB"]
        self.assertAlmostEqual(role_share, 8.75, places=2)
        gs = simulate_games([qb, wr], n=800, seed=1, inputs=inputs)
        self.assertGreater(gs.by_pid["willis"].mean, 12.0)
        self.assertLess(gs.by_pid["willis"].mean, 20.0)
        self.assertGreater(gs.by_pid["willis"].p90, role_share + 2.0)


if __name__ == "__main__":
    unittest.main()
