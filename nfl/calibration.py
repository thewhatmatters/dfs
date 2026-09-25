"""Ensemble calibration scores. NumPy and SciPy only.

Draws are ``(n_rows, n_draws)`` and observations ``y`` are ``(n_rows,)``.
The holdout harness calls this module; it does not change the sim.
"""

from __future__ import annotations

import numpy as np
from scipy import stats

# Week-block bootstrap length used by the holdout paired comparisons.
N_BOOT = 2000
BOOT_SEED = 0
LOG_CLIP = 1e-6
PIT_BINS = 10
# q01..q99. Persisted when the raw draw matrix is too large to keep.
QUANTILES = np.arange(1, 100) / 100.0


def crps_normal(y, mu, sigma):
    """Closed form CRPS for a normal predictive distribution.

    ``sigma * (z * (2 Phi(z) - 1) + 2 phi(z) - 1/sqrt(pi))`` with
    ``z = (y - mu) / sigma``. At ``N(0, 1)`` and ``y = 0`` this is
    ``(sqrt(2) - 1) / sqrt(pi)``.
    """
    y = np.asarray(y, dtype=float)
    mu = np.asarray(mu, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    z = (y - mu) / sigma
    return sigma * (
        z * (2.0 * stats.norm.cdf(z) - 1.0)
        + 2.0 * stats.norm.pdf(z)
        - 1.0 / np.sqrt(np.pi)
    )


def ensemble_pair_mean(samples) -> float:
    """``E|X-X'|`` for one sample, ``X'`` an independent copy.

    ``X`` and ``X'`` are drawn from the empirical distribution, so the
    diagonal ``i = j`` is included and contributes 0. For sorted
    ``x_(1)..x_(n)`` and ``i = 1..n``:

        (2 / n^2) * sum((2 i - n - 1) * x_(i))
    """
    x = np.sort(np.asarray(samples, dtype=float).ravel())
    n = int(x.size)
    if n == 0:
        return float("nan")
    i = np.arange(1, n + 1, dtype=float)
    return float((2.0 / (n * n)) * np.sum((2.0 * i - n - 1.0) * x))


def crps_ensemble(draws, y) -> np.ndarray:
    """Sample CRPS. ``draws`` is ``(n_rows, n_draws)``, ``y`` is ``(n_rows,)``.

    ``term1 = mean |x - y|`` on each row. ``E|X-X'|`` is
    ``ensemble_pair_mean`` on that row. ``crps = term1 - 0.5 * E|X-X'|``.
    """
    x = np.sort(np.asarray(draws, dtype=float), axis=1)
    obs = np.asarray(y, dtype=float).reshape(-1)
    if x.ndim != 2 or obs.shape[0] != x.shape[0]:
        raise ValueError("draws must be (n_rows, n_draws) and y (n_rows,)")
    n = x.shape[1]
    if n == 0:
        return np.full(obs.shape[0], np.nan)
    term1 = np.mean(np.abs(x - obs[:, None]), axis=1)
    i = np.arange(1, n + 1, dtype=float)
    weights = 2.0 * i - n - 1.0
    energy = (2.0 / (n * n)) * (x * weights[None, :]).sum(axis=1)
    return term1 - 0.5 * energy


def pit(draws, y, rng) -> np.ndarray:
    """Randomized PIT. ``pit = P(X<y) + U * P(X=y)``, ``U ~ Uniform(0, 1)``.

    ``rng`` is a ``numpy.random.Generator``. Ties are the reason for ``U``;
    continuous draws almost never tie.
    """
    sample = np.asarray(draws, dtype=float)
    obs = np.asarray(y, dtype=float).reshape(-1, 1)
    if sample.ndim != 2 or obs.shape[0] != sample.shape[0]:
        raise ValueError("draws must be (n_rows, n_draws) and y (n_rows,)")
    below = np.mean(sample < obs, axis=1)
    equal = np.mean(sample == obs, axis=1)
    u = rng.random(obs.shape[0])
    return below + u * equal


def pit_histogram(values, bins: int = PIT_BINS) -> dict:
    """10-bin histogram on ``[0, 1]`` and a chi-square test against uniform.

    ``p = scipy.stats.chi2.sf(chi2, bins - 1)``.
    """
    vals = np.asarray(values, dtype=float)
    vals = vals[np.isfinite(vals)]
    counts, _edges = np.histogram(vals, bins=int(bins), range=(0.0, 1.0))
    n = int(vals.size)
    expected = n / float(bins) if bins else float("nan")
    if n == 0 or expected <= 0:
        chi2 = float("nan")
        p_value = float("nan")
    else:
        chi2 = float(np.sum((counts - expected) ** 2 / expected))
        p_value = float(stats.chi2.sf(chi2, int(bins) - 1))
    return {
        "bins": int(bins),
        "counts": [int(c) for c in counts],
        "expected": float(expected) if n else 0.0,
        "chi2": chi2,
        "df": int(bins) - 1,
        "p": p_value,
        "n": n,
    }


def interval_hit(draws, y, lo: float, hi: float) -> np.ndarray:
    """True when ``y`` is inside ``np.quantile(draws, [lo, hi], axis=1)``."""
    sample = np.asarray(draws, dtype=float)
    obs = np.asarray(y, dtype=float).reshape(-1)
    if sample.ndim != 2 or obs.shape[0] != sample.shape[0]:
        raise ValueError("draws must be (n_rows, n_draws) and y (n_rows,)")
    q_lo = np.quantile(sample, float(lo), axis=1)
    q_hi = np.quantile(sample, float(hi), axis=1)
    return (obs >= q_lo) & (obs <= q_hi)


def brier_score(p, o) -> np.ndarray:
    """``(p - o)^2``. ``p`` is not clipped."""
    prob = np.asarray(p, dtype=float)
    outcome = np.asarray(o, dtype=float)
    return (prob - outcome) ** 2


def log_score(p, o) -> np.ndarray:
    """``o log p + (1-o) log(1-p)`` with ``p`` clipped to ``[1e-6, 1-1e-6]``.

    Higher is better. Brier is the other direction (lower is better).
    """
    prob = np.clip(np.asarray(p, dtype=float), LOG_CLIP, 1.0 - LOG_CLIP)
    outcome = np.asarray(o, dtype=float)
    return outcome * np.log(prob) + (1.0 - outcome) * np.log(1.0 - prob)


def fixed_threshold_scores(draws, actual, thresholds) -> dict:
    """Brier and log score for ``P(draw >= t)`` against ``actual >= t``.

    ``draws`` is ``(n, n_draws)``. ``actual`` is ``(n,)``. The same
    thresholds apply to every row (lineup totals use 125 and 165).
    """
    sample = np.atleast_2d(np.asarray(draws, dtype=float))
    obs = np.asarray(actual, dtype=float).reshape(-1)
    cuts = np.asarray(list(thresholds), dtype=float)
    if sample.shape[0] != obs.shape[0]:
        raise ValueError("draws and actual row counts differ")
    if cuts.size == 0 or obs.size == 0:
        return {
            "thresholds": [float(t) for t in cuts],
            "p": np.zeros((obs.size, cuts.size)),
            "o": np.zeros((obs.size, cuts.size)),
            "brier": np.zeros((obs.size, cuts.size)),
            "log_score": np.zeros((obs.size, cuts.size)),
        }
    prob = np.mean(sample[:, :, None] >= cuts[None, None, :], axis=1)
    outcome = (obs[:, None] >= cuts[None, :]).astype(float)
    return {
        "thresholds": [float(t) for t in cuts],
        "p": prob,
        "o": outcome,
        "brier": brier_score(prob, outcome),
        "log_score": log_score(prob, outcome),
    }


def salary_multiple_scores(draws, y, salary, multiples=(2.0, 3.0, 4.0)) -> dict:
    """``P(points >= m * salary/1000)`` for each multiple.

    Rows with ``salary <= 0`` are dropped. Thresholds differ by row.
    """
    sample = np.asarray(draws, dtype=float)
    obs = np.asarray(y, dtype=float).reshape(-1)
    sal = np.asarray(salary, dtype=float).reshape(-1)
    mults = np.asarray(list(multiples), dtype=float)
    keep = sal > 0
    index = np.flatnonzero(keep)
    empty = {
        "n": 0,
        "multiples": [float(m) for m in mults],
        "index": index,
        "p": np.zeros((0, mults.size)),
        "o": np.zeros((0, mults.size)),
        "brier": np.zeros((0, mults.size)),
        "log_score": np.zeros((0, mults.size)),
    }
    if sample.ndim != 2 or obs.shape[0] != sample.shape[0] or sal.shape[0] != obs.shape[0]:
        raise ValueError("draws, y, and salary must share n_rows")
    if not np.any(keep) or mults.size == 0:
        return empty
    kept = sample[keep]
    yy = obs[keep]
    thr = sal[keep, None] * mults[None, :] / 1000.0
    prob = np.mean(kept[:, :, None] >= thr[:, None, :], axis=1)
    outcome = (yy[:, None] >= thr).astype(float)
    return {
        "n": int(index.size),
        "multiples": [float(m) for m in mults],
        "index": index,
        "threshold": thr,
        "p": prob,
        "o": outcome,
        "brier": brier_score(prob, outcome),
        "log_score": log_score(prob, outcome),
    }


def monte_carlo_summary(samples) -> dict:
    """Mean, Monte Carlo SE, and Student-t 95% half-width across seeds.

    ``SE = sd(ddof=1) / sqrt(n)``. Half-width is
    ``t_{n-1, 0.975} * SE``. ``n < 2`` leaves SE and the half-width null.
    """
    x = np.asarray(list(samples), dtype=float).ravel()
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "se": None,
            "half_width_95": None,
            "values": [],
        }
    mean = float(x.mean())
    values = [float(v) for v in x]
    if n < 2:
        return {
            "n": n,
            "mean": mean,
            "se": None,
            "half_width_95": None,
            "values": values,
        }
    se = float(x.std(ddof=1) / np.sqrt(n))
    half = float(stats.t.ppf(0.975, n - 1) * se)
    return {
        "n": n,
        "mean": mean,
        "se": se,
        "half_width_95": half,
        "values": values,
    }


def _block_arrays(values, blocks) -> list[np.ndarray]:
    obs = np.asarray(values, dtype=float).ravel()
    labels = np.asarray(blocks)
    if labels.shape[0] != obs.shape[0]:
        raise ValueError("values and blocks length mismatch")
    groups: dict = {}
    order: list = []
    for value, label in zip(obs.tolist(), labels.tolist()):
        if label not in groups:
            groups[label] = []
            order.append(label)
        groups[label].append(value)
    return [np.asarray(groups[label], dtype=float) for label in order]


def block_bootstrap(values, blocks, *, n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> dict:
    """Resample whole blocks, never rows. Estimate is the sum/count ratio.

    Percentile 95% CI. ``note`` is ``"few blocks: CI is rough"`` when
    fewer than 20 blocks were observed.
    """
    n_boot = int(n_boot)
    groups = _block_arrays(values, blocks) if len(np.asarray(values).ravel()) else []
    n_blocks = len(groups)
    n_rows = int(sum(g.size for g in groups))
    note = "few blocks: CI is rough" if n_blocks < 20 else ""
    if n_rows == 0 or n_blocks == 0:
        return {
            "estimate": None,
            "se": None,
            "ci95": [None, None],
            "n_rows": 0,
            "n_blocks": 0,
            "n_boot": n_boot,
            "note": note or "few blocks: CI is rough",
        }
    point = float(sum(float(g.sum()) for g in groups) / n_rows)
    rng = np.random.default_rng(int(seed))
    estimates = np.empty(n_boot, dtype=float)
    for draw_i in range(n_boot):
        chosen = rng.integers(0, n_blocks, size=n_blocks)
        total = 0.0
        count = 0
        for idx in chosen:
            group = groups[int(idx)]
            total += float(group.sum())
            count += int(group.size)
        estimates[draw_i] = total / count
    se = float(estimates.std(ddof=1)) if n_boot > 1 else None
    lo = float(np.quantile(estimates, 0.025))
    hi = float(np.quantile(estimates, 0.975))
    return {
        "estimate": point,
        "se": se,
        "ci95": [lo, hi],
        "n_rows": n_rows,
        "n_blocks": n_blocks,
        "n_boot": n_boot,
        "note": note,
    }


def paired_difference(loss_a, loss_b, blocks, *, n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> dict:
    """Week-block bootstrap of ``d = loss_a - loss_b``.

    ``excludes_zero`` is true when the 95% percentile interval misses 0.
    Positive ``d`` means ``a`` has the higher loss.
    """
    diff = np.asarray(loss_a, dtype=float) - np.asarray(loss_b, dtype=float)
    out = block_bootstrap(diff, blocks, n_boot=n_boot, seed=seed)
    lo, hi = out["ci95"]
    out["excludes_zero"] = bool(
        lo is not None and hi is not None and (lo > 0.0 or hi < 0.0)
    )
    return out


def fisher_z(c_sim, c_act, n: int) -> dict:
    """``se = 1/sqrt(n-3)``, ``z = |atanh(c_sim) - atanh(c_act)| / se``.

    Correlations are clipped to ``+/- 0.999999`` before ``atanh``.
    ``flag`` is true when ``z > 4``. ``n <= 3`` leaves ``z`` null.
    """
    n = int(n)

    def _clip(value):
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(number):
            return None
        return float(np.clip(number, -0.999999, 0.999999))

    sim = _clip(c_sim)
    act = _clip(c_act)
    out = {
        "c_sim": None if c_sim is None else sim,
        "c_act": None if c_act is None else act,
        "n": n,
        "se": None,
        "z": None,
        "flag": False,
    }
    if sim is None or act is None or n <= 3:
        return out
    se = 1.0 / np.sqrt(n - 3.0)
    z = abs(float(np.arctanh(sim) - np.arctanh(act))) / se
    out["se"] = float(se)
    out["z"] = float(z)
    out["flag"] = bool(z > 4.0)
    return out
