"""Known-value checks for ensemble scores. Offline; no gangstash."""

from __future__ import annotations

import math
import unittest

import numpy as np
from scipy import stats

from nfl.calibration import (
    brier_score,
    block_bootstrap,
    crps_ensemble,
    crps_normal,
    ensemble_pair_mean,
    fisher_z,
    fixed_threshold_scores,
    interval_hit,
    log_score,
    monte_carlo_summary,
    paired_difference,
    pit,
    pit_histogram,
    salary_multiple_scores,
)


class CrpsTest(unittest.TestCase):
    def test_normal_at_zero_is_the_closed_form(self) -> None:
        got = float(crps_normal(0.0, 0.0, 1.0))
        target = (math.sqrt(2.0) - 1.0) / math.sqrt(math.pi)
        self.assertAlmostEqual(got, target, places=12)

    def test_sample_crps_converges_to_the_normal(self) -> None:
        rng = np.random.default_rng(123)
        analytic = float(crps_normal(0.0, 0.0, 1.0))
        draws = rng.normal(0.0, 1.0, size=(1, 250_000))
        got = float(crps_ensemble(draws, np.array([0.0]))[0])
        self.assertAlmostEqual(got, analytic, delta=0.005)
        mu, sigma, y = 0.3, 2.0, 1.5
        draws = rng.normal(mu, sigma, size=(1, 250_000))
        got = float(crps_ensemble(draws, np.array([y]))[0])
        self.assertAlmostEqual(got, float(crps_normal(y, mu, sigma)), delta=0.02)

    def test_pair_mean_identity(self) -> None:
        x = np.array([1.0, 2.0, 4.0, 7.0, 7.0])
        n = int(x.size)
        pair = 0.0
        for i in range(n):
            for j in range(n):
                pair += abs(float(x[i] - x[j]))
        pair /= n * n
        self.assertAlmostEqual(ensemble_pair_mean(x), pair, places=12)
        draws = np.array([[1.0, 3.0, 4.0, 8.0]])
        y = np.array([2.0])
        term1 = float(np.mean(np.abs(draws - y[:, None])))
        expected = term1 - 0.5 * ensemble_pair_mean(draws[0])
        self.assertAlmostEqual(float(crps_ensemble(draws, y)[0]), expected, places=12)


class BrierLogTest(unittest.TestCase):
    def test_brier_known_values(self) -> None:
        self.assertAlmostEqual(float(brier_score(0.7, 1)), 0.09)
        self.assertAlmostEqual(float(brier_score(0.7, 0)), 0.49)

    def test_log_score_clips(self) -> None:
        got = float(log_score(0.0, 1.0))
        self.assertAlmostEqual(got, math.log(1e-6), places=8)
        got = float(log_score(1.0, 0.0))
        self.assertAlmostEqual(got, math.log(1e-6), places=8)

    def test_salary_and_lineup_thresholds(self) -> None:
        draws = np.array([[10.0, 20.0, 30.0, 40.0]])
        # salary 10000 → 2x/3x/4x are 20, 30, 40 points.
        scored = salary_multiple_scores(draws, np.array([30.0]), np.array([10000.0]))
        self.assertEqual(scored["n"], 1)
        self.assertAlmostEqual(float(scored["p"][0, 0]), 0.75)  # >= 20
        self.assertAlmostEqual(float(scored["o"][0, 0]), 1.0)
        self.assertAlmostEqual(float(scored["brier"][0, 0]), (0.75 - 1.0) ** 2)
        lineup = np.array([[100.0, 130.0, 170.0, 180.0]])
        totals = fixed_threshold_scores(lineup, np.array([160.0]), (125.0, 165.0))
        self.assertAlmostEqual(float(totals["p"][0, 0]), 0.75)
        self.assertAlmostEqual(float(totals["brier"][0, 0]), 0.0625)
        self.assertAlmostEqual(float(totals["p"][0, 1]), 0.5)
        self.assertEqual(float(totals["o"][0, 1]), 0.0)
        self.assertAlmostEqual(float(totals["brier"][0, 1]), 0.25)


class PitCoverageTest(unittest.TestCase):
    def test_calibrated_pit_and_underdispersed_coverage(self) -> None:
        rng = np.random.default_rng(7)
        n_rows, n_draws = 4000, 800
        y = rng.normal(size=n_rows)
        draws = rng.normal(size=(n_rows, n_draws))
        pits = pit(draws, y, np.random.default_rng(11))
        hist = pit_histogram(pits, bins=10)
        self.assertGreater(hist["p"], 0.01)
        cover = float(np.mean(interval_hit(draws, y, 0.10, 0.90)))
        self.assertAlmostEqual(cover, 0.80, delta=0.03)

        half = 0.5 * float(stats.norm.ppf(0.90))
        expected = 2.0 * float(stats.norm.cdf(half)) - 1.0
        self.assertAlmostEqual(expected, 0.478, delta=0.01)
        narrow = rng.normal(0.0, 0.5, size=(n_rows, n_draws))
        cover_u = float(np.mean(interval_hit(narrow, y, 0.10, 0.90)))
        self.assertAlmostEqual(cover_u, expected, delta=0.04)
        hist_u = pit_histogram(pit(narrow, y, np.random.default_rng(12)), bins=10)
        self.assertLess(hist_u["p"], 1e-6)


class BootstrapTest(unittest.TestCase):
    def test_block_bootstrap_matches_cluster_se_and_row_bootstrap_understates_it(self) -> None:
        rng = np.random.default_rng(0)
        n_blocks, width = 40, 25
        effect = rng.normal(0.0, 5.0, size=n_blocks)
        noise = rng.normal(0.0, 0.05, size=(n_blocks, width))
        values = (effect[:, None] + noise).ravel()
        blocks = np.repeat(np.arange(n_blocks), width)
        block_means = values.reshape(n_blocks, width).mean(axis=1)
        true_se = float(block_means.std(ddof=1) / np.sqrt(n_blocks))
        clustered = block_bootstrap(values, blocks, n_boot=2000, seed=1)
        rows = block_bootstrap(values, np.arange(values.size), n_boot=2000, seed=1)
        self.assertAlmostEqual(clustered["se"], true_se, delta=0.30 * true_se)
        self.assertLess(rows["se"], clustered["se"] * 0.5)
        self.assertEqual(clustered["n_blocks"], n_blocks)
        self.assertEqual(clustered["n_rows"], n_blocks * width)
        self.assertEqual(clustered["note"], "")

    def test_few_blocks_note(self) -> None:
        out = block_bootstrap([1.0, 2.0, 3.0, 4.0], [0, 0, 1, 1], n_boot=200, seed=0)
        self.assertEqual(out["note"], "few blocks: CI is rough")
        self.assertEqual(out["n_blocks"], 2)

    def test_paired_bootstrap_recovers_a_constant_shift(self) -> None:
        rng = np.random.default_rng(1)
        blocks = np.repeat(np.arange(30), 10)
        base = rng.normal(size=blocks.shape[0])
        out = paired_difference(base + 0.2, base, blocks, n_boot=2000, seed=0)
        self.assertAlmostEqual(out["estimate"], 0.2, places=12)
        self.assertTrue(out["excludes_zero"])
        self.assertGreater(out["ci95"][0], 0.0)
        self.assertEqual(out["n_boot"], 2000)


class MonteCarloSeTest(unittest.TestCase):
    def test_se_of_a_normal_mean(self) -> None:
        rng = np.random.default_rng(0)
        n = 40_000
        samples = rng.normal(0.0, 3.0, size=n)
        summary = monte_carlo_summary(samples)
        self.assertAlmostEqual(summary["se"], 3.0 / math.sqrt(n), delta=0.001)
        self.assertEqual(summary["n"], n)

    def test_student_t_half_width(self) -> None:
        summary = monte_carlo_summary([1.0, 2.0, 3.0])
        se = 1.0 / math.sqrt(3.0)
        half = float(stats.t.ppf(0.975, 2)) * se
        self.assertAlmostEqual(summary["mean"], 2.0)
        self.assertAlmostEqual(summary["se"], se, places=12)
        self.assertAlmostEqual(summary["half_width_95"], half, places=12)
        single = monte_carlo_summary([4.0])
        self.assertIsNone(single["se"])
        self.assertIsNone(single["half_width_95"])


class FisherTest(unittest.TestCase):
    def test_large_gap_is_flagged_and_clip_is_finite(self) -> None:
        n = 100
        checked = fisher_z(0.9, 0.0, n)
        se = 1.0 / math.sqrt(n - 3)
        z = abs(np.arctanh(0.9) - np.arctanh(0.0)) / se
        self.assertAlmostEqual(checked["se"], se, places=12)
        self.assertAlmostEqual(checked["z"], float(z), places=12)
        self.assertTrue(checked["flag"])
        quiet = fisher_z(0.05, 0.0, 30)
        self.assertFalse(quiet["flag"])
        clipped = fisher_z(1.0, 0.0, 50)
        self.assertTrue(math.isfinite(clipped["z"]))
        self.assertIsNone(fisher_z(0.2, 0.1, 3)["z"])


if __name__ == "__main__":
    unittest.main()
