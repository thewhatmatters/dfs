"""Team-mode game noise, fit on the 2024 regular season only.

2025 is the judging season, so none of these constants use it.

Game scores
-----------
Each team-mode draw is one bivariate normal on the game total and the
FanDuel home spread (negative spread means the home team is favored):

    total  ~ N(closing total,  TEAM_TOTAL_SD)
    spread ~ N(closing spread, TEAM_SPREAD_SD)
    corr(total, spread) = TEAM_TOTAL_SPREAD_RHO

Home and away points are ``(total - spread) / 2`` and ``(total + spread) / 2``,
the same split the default sim uses. The SDs are not scaled by EPA variance:
the 2024 residual already mixes every team.

How the SDs were derived
------------------------
272 completed 2024 regular-season games from the nflverse schedules release
(``games.csv``, ``game_type=REG``). nflverse ``spread_line`` is positive when
the home team is favored, so the FanDuel home spread is ``-spread_line`` and
the closing home margin is ``spread_line``. Residuals:

    total_resid  = (home_score + away_score) - total_line
    spread_resid = -( (home_score - away_score) - spread_line )

``spread_resid`` is in FanDuel home-spread units, which is what the sim
draws. Sample SD uses the ``n - 1`` divisor. Pearson is the usual centered
correlation. The rows are ``nfl/testdata/sim_team_2024_game_residuals.csv``.
``fit_spread_total`` recomputes the three constants (rounded to 4 decimals):

    TEAM_TOTAL_SD          12.4874
    TEAM_SPREAD_SD         12.6228
    TEAM_TOTAL_SPREAD_RHO  -0.0832

Score damping
-------------
Pass yards, pass TDs, pass attempts, and rush attempts are multiplied by

    1 + TEAM_SCORE_DAMP * (team_points / implied - 1)

so teammates move with their own score and a QB moves against the defense
that allowed those points. ``TEAM_SCORE_DAMP = 0`` leaves the implied anchor.
``1`` is the raw ``team_points / implied`` ratio.

The 2024 actual correlation of starter-QB FanDuel points vs the opposing
DEF (points-allowed bucket plus sacks, interceptions, fumble recoveries,
safeties, blocks, and return TDs; attempts >= 10) is ``QB_OPP_DEF_CORRELATION``
= -0.4663 on 544 team-games. Rows:
``nfl/testdata/sim_team_2024_qb_def.csv``. A 1:1 score link overshoots that,
because the sim DEF is almost a function of the same points.
``TEAM_SCORE_DAMP`` is 0.6901: the bisection
(``python3 -m nfl.sim_team --fit-damp``, 2024 week-1 lines, 2000 draws,
seed 1) that matches the mean within-game QB–opp DEF correlation to
-0.4663. A second seed on that slate was -0.4668.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

# Sample SDs and correlation of 2024 closing-line residuals. See the module
# docstring. Rounded to 4 decimals by fit_spread_total.
TEAM_TOTAL_SD = 12.4874
TEAM_SPREAD_SD = 12.6228
TEAM_TOTAL_SPREAD_RHO = -0.0832
# 2024 pooled Pearson, starter QB vs opposing DEF. fit_qb_def_correlation.
QB_OPP_DEF_CORRELATION = -0.4663
# How strongly team-mode production follows the drawn team score.
# Bisection on 2024 week-1 closing lines (WEEK1_2024_LINES), both teams on
# the opportunity path, 2000 draws, seed 1, targeting QB_OPP_DEF_CORRELATION.
# The probe landed at 0.6901 (mean within-game QB–opp DEF correlation
# -0.4663). Seed 2 on the same slate was -0.4668. Undamped (1.0) was about
# -0.70 on that slate, which is the overshoot this shrink removes.
TEAM_SCORE_DAMP = 0.6901

_DATA = Path(__file__).resolve().parent / "testdata"
RESIDUALS_CSV = _DATA / "sim_team_2024_game_residuals.csv"
QB_DEF_CSV = _DATA / "sim_team_2024_qb_def.csv"

# 2024 week 1 closing lines (nflverse): away, home, total, spread_line.
# spread_line > 0 means the home team is favored.
WEEK1_2024_LINES = (
    ("BAL", "KC", 46.0, 3.0),
    ("GB", "PHI", 49.5, 2.0),
    ("PIT", "ATL", 43.0, 4.0),
    ("ARI", "BUF", 46.0, 6.5),
    ("TEN", "CHI", 43.0, 4.0),
    ("NE", "CIN", 40.5, 8.0),
    ("HOU", "IND", 49.0, -3.0),
    ("JAX", "MIA", 49.5, 3.5),
    ("CAR", "NO", 41.5, 3.5),
    ("MIN", "NYG", 42.5, -1.0),
    ("LV", "LAC", 41.0, 3.0),
    ("DEN", "SEA", 42.0, 6.5),
    ("DAL", "CLE", 42.0, 2.5),
    ("WAS", "TB", 42.0, 4.0),
    ("LA", "DET", 54.0, 5.0),
    ("NYJ", "SF", 43.0, 3.5),
)


def _finite_number(value):
    """True for a finite int or float. Booleans and missing values are not."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return number == number and number != float("inf") and number != float("-inf")


def fitted_constants_ready():
    """True when the four 2024 constants can drive a team-mode draw.

    A missing, non-numeric, or non-finite constant is not ready. The sim
    then keeps the default draw and logs a warning instead of raising.
    """
    total_sd = TEAM_TOTAL_SD
    spread_sd = TEAM_SPREAD_SD
    rho = TEAM_TOTAL_SPREAD_RHO
    damp = TEAM_SCORE_DAMP
    if not _finite_number(total_sd) or not _finite_number(spread_sd):
        return False
    if float(total_sd) <= 0.0 or float(spread_sd) <= 0.0:
        return False
    if not _finite_number(rho) or not _finite_number(damp):
        return False
    if float(rho) < -1.0 or float(rho) > 1.0:
        return False
    return True


def parse_sim_mode(raw):
    """``off`` (default) or ``team``. Anything else raises ValueError."""
    text = (raw or "off").strip().lower()
    if text in ("", "off", "none"):
        return "off"
    if text == "team":
        return "team"
    raise ValueError("unknown sim mode %s; use off or team" % text)


def sample_sd(xs):
    """Sample standard deviation (``n - 1``). ``None`` when ``n < 2``."""
    n = len(xs)
    if n < 2:
        return None
    mean = sum(xs) / float(n)
    var = sum((value - mean) ** 2 for value in xs) / float(n - 1)
    return math.sqrt(var)


def pearson(xs, ys):
    """Centered Pearson correlation. ``None`` when undefined."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / float(n)
    my = sum(ys) / float(n)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0.0 or dy == 0.0:
        return None
    return num / (dx * dy)


def _round4(value):
    return round(float(value) + 0.0, 4)


def fit_spread_total(total_residuals, spread_residuals):
    """``(total_sd, spread_sd, rho)`` from closing-line residuals.

    ``spread_residuals`` use the FanDuel home-spread sign. Each value is
    rounded to 4 decimals, which is how the committed constants were stored.
    """
    total_sd = sample_sd(list(total_residuals))
    spread_sd = sample_sd(list(spread_residuals))
    rho = pearson(list(total_residuals), list(spread_residuals))
    if total_sd is None or spread_sd is None or rho is None:
        raise ValueError("need at least 3 residual rows to fit the game noise")
    return _round4(total_sd), _round4(spread_sd), _round4(rho)


def fit_qb_def_correlation(qb_points, def_points):
    """Pooled QB vs opposing-DEF Pearson, rounded to 4 decimals."""
    rho = pearson(list(qb_points), list(def_points))
    if rho is None:
        raise ValueError("need at least 3 QB-DEF rows")
    return _round4(rho)


def load_residuals(path=None):
    """``(total_residuals, spread_residuals)`` from the 2024 CSV."""
    src = Path(path) if path is not None else RESIDUALS_CSV
    totals = []
    spreads = []
    with src.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            totals.append(float(row["total_resid"]))
            spreads.append(float(row["spread_resid"]))
    return totals, spreads


def load_qb_def(path=None):
    """``(qb_points, def_points)`` from the 2024 CSV."""
    src = Path(path) if path is not None else QB_DEF_CSV
    qb_points = []
    def_points = []
    with src.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            qb_points.append(float(row["qb_fd"]))
            def_points.append(float(row["def_fd"]))
    return qb_points, def_points


def production_scale(team_pts, implied, damp):
    """Shrink ``team_pts / implied`` toward 1.

    ``damp`` 0 leaves the anchor on the implied total. ``damp`` 1 is the
    raw ratio. The result stays positive when both inputs are positive and
    ``damp`` is in ``[0, 1]``.
    """
    imp = float(implied)
    if imp <= 0.0:
        return 1.0
    ratio = max(0.0, float(team_pts)) / imp
    return 1.0 + float(damp) * (ratio - 1.0)


def draw_total_and_spread(rng, total_mu, spread_mu, total_sd=None, spread_sd=None, rho=None):
    """One bivariate normal draw of ``(total, home_spread)``.

    Two standard normals, then the Cholesky factor of the correlation.
    The total is not floored here; the game draw applies the same floor
    as the default path.
    """
    sd_total = TEAM_TOTAL_SD if total_sd is None else float(total_sd)
    sd_spread = TEAM_SPREAD_SD if spread_sd is None else float(spread_sd)
    corr = TEAM_TOTAL_SPREAD_RHO if rho is None else float(rho)
    corr = max(-1.0, min(1.0, corr))
    z_total = rng.gauss(0.0, 1.0)
    z_spread = rng.gauss(0.0, 1.0)
    total = float(total_mu) + sd_total * z_total
    spread = float(spread_mu) + sd_spread * (
        corr * z_total + math.sqrt(max(0.0, 1.0 - corr * corr)) * z_spread
    )
    return total, spread


def search_score_damp(measure, target, lo=0.0, hi=1.0, rounds=14):
    """Bisection on a monotone correlation.

    ``measure(damp)`` returns a correlation that gets more negative as
    ``damp`` rises. Returns ``(damp, correlation)`` at the closest probe,
    with damp rounded to 4 decimals.
    """
    best_damp = None
    best_rho = None
    for _ in range(int(rounds)):
        mid = 0.5 * (float(lo) + float(hi))
        rho = float(measure(mid))
        if best_rho is None or abs(rho - target) < abs(best_rho - target):
            best_damp = mid
            best_rho = rho
        if rho > target:
            lo = mid
        else:
            hi = mid
    return _round4(best_damp), best_rho


def build_week1_slate():
    """2024 week-1 lines, both teams on the opportunity path. No network."""
    from nfl.players import Player
    from nfl.sim_inputs import SimInputs, TargetWeek

    players = []
    weeks = []
    for away, home, total, nfl_spread in WEEK1_2024_LINES:
        fd_spread = -float(nfl_spread)
        home_impl = (float(total) - fd_spread) / 2.0
        away_impl = (float(total) + fd_spread) / 2.0
        game = "%s@%s" % (away, home)
        for team, opp, impl, opp_impl, spread in (
            (home, away, home_impl, away_impl, fd_spread),
            (away, home, away_impl, home_impl, -fd_spread),
        ):
            common = dict(
                team=team,
                opponent=opp,
                game=game,
                fppg=None,
                injury="",
                roster_position="",
                spread=spread,
                total=float(total),
                implied_total=impl,
                implied_opp=opp_impl,
            )
            players.append(
                Player(
                    pid="%s-qb" % team,
                    name="%s QB" % team,
                    position="QB",
                    salary=8000,
                    depth_rank=1,
                    **common,
                )
            )
            wr = Player(
                pid="%s-wr" % team,
                name="%s WR" % team,
                position="WR",
                salary=7000,
                depth_rank=1,
                **common,
            )
            players.append(wr)
            players.append(
                Player(
                    pid="%s-rb" % team,
                    name="%s RB" % team,
                    position="RB",
                    salary=6500,
                    depth_rank=1,
                    **common,
                )
            )
            players.append(
                Player(
                    pid="%s-def" % team,
                    name="%s DEF" % team,
                    position="DEF",
                    salary=4000,
                    depth_rank=None,
                    **common,
                )
            )
            for week in (1, 2, 3):
                weeks.append(
                    TargetWeek(
                        season=2024,
                        week=week,
                        position="WR",
                        player_name=wr.name,
                        team_fd=team,
                        targets=8.0,
                        target_share=0.25,
                        team_targets=32.0,
                        team_pass_attempts=34.0,
                    )
                )
    return players, SimInputs(targets=tuple(weeks))


def mean_qb_def_correlation(damp, n=2000, seed=1):
    """Mean within-game Pearson of QB vs opposing DEF at this damp."""
    from nfl.sim import pearson as sim_pearson
    from nfl.sim import simulate_games

    players, inputs = build_week1_slate()
    played = simulate_games(
        players,
        n=int(n),
        seed=int(seed),
        inputs=inputs,
        sim_mode="team",
        score_damp=float(damp),
    )
    rhos = []
    for away, home, _total, _spread in WEEK1_2024_LINES:
        for team, opp in ((away, home), (home, away)):
            rho = sim_pearson(
                list(played.draws["%s-qb" % team]),
                list(played.draws["%s-def" % opp]),
            )
            rhos.append(float(rho))
    return sum(rhos) / float(len(rhos))


def main(argv=None):
    """Print the committed fit. ``--fit-damp`` reruns the 2024 bisection."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    totals, spreads = load_residuals()
    total_sd, spread_sd, rho = fit_spread_total(totals, spreads)
    qb_points, def_points = load_qb_def()
    qb_def = fit_qb_def_correlation(qb_points, def_points)
    print("games %d" % len(totals))
    print("TEAM_TOTAL_SD %s" % total_sd)
    print("TEAM_SPREAD_SD %s" % spread_sd)
    print("TEAM_TOTAL_SPREAD_RHO %s" % rho)
    print("QB_OPP_DEF_CORRELATION %s (n %d)" % (qb_def, len(qb_points)))
    print("TEAM_SCORE_DAMP %s" % TEAM_SCORE_DAMP)
    if "--fit-damp" in args:
        print("fitting damp to %s ..." % qb_def, file=sys.stderr)

        def measure(damp, _target=qb_def):
            rho_hat = mean_qb_def_correlation(damp)
            print("  damp %.4f  qb-def %.4f" % (damp, rho_hat), file=sys.stderr)
            return rho_hat

        damp, fitted = search_score_damp(measure, qb_def)
        print("fitted TEAM_SCORE_DAMP %s  correlation %.4f" % (damp, fitted))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
