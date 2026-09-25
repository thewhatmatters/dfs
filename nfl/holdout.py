"""Multi-season measurement for the NFL sim. Does not change the model.

```
python3 -m nfl.holdout --season 2024 --weeks 1-18 --n 3000 --json-out results/holdout-2024.json
python3 -m nfl.holdout --season 2025 --weeks 1-18 --n 3000 --json-out results/holdout-2025.json
python3 -m nfl.holdout --season 2026 --weeks 2 --n 3000 --json-out results/holdout-2026-w2.json
python3 -m nfl.holdout --seasons 2024,2025 --weeks 1-18 --n 3000 --json-out results/holdout-2024-2025.json
```

Each week uses the depth-chart pool (no FanDuel CSV) and only inputs from
before that week, the same rule as ``nfl.backtest``. Week 1 therefore has
no season-to-date board. ``--seed-prior-season`` (default off) replaces
week 1 sim inputs with the prior season's week 18.

``--population pregame`` (default) keeps depth-chart starters even when the
box score is a zero or missing. A missing row scores 0 unless injuries list
the player Out or IR before that team's kickoff. ``--population played``
is the old name-only join and does not skip duplicate draws. The report
above does not import NumPy or SciPy.

A pool row whose sim draws are not length ``n`` (the same pid listed twice)
is skipped and named on stderr. The depth pool itself is not deduped.

``--metrics``, more than one ``--seeds`` value, or ``--draws-out`` adds
PIT, CRPS, coverage, Brier and log score, paired tests, residual Fisher z,
and a Monte Carlo SE. That path imports NumPy and SciPy lazily and exits
with ``pip3 install -r requirements.txt`` when they are missing. With
``--json-out results/holdout-2024.json`` and metrics on, the run also writes
``results/holdout-2024.draws.npz`` and ``results/holdout-2024.manifest.json``.

The report pools starters and the full pool: mean error and MAE by position
for the board, the placeholder sim, and the data sim. It also reports sim
calibration, game margin and total variance, role correlations, Spearman
rank accuracy, a one-week ±10% sensitivity, and a props_closing baseline
when that dataset has rows. ``props_closing`` starts at 2026 week 3; earlier
seasons skip that section. ``--sim-mode team`` scores the opt-in team-score
sim. The default ``off`` is the current draws.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from nfl.backtest import (
    DepthPoolError,
    _STARTER_RANKS,
    _kickoff_stamp,
    _load_live,
    _position,
    index_actual_rows,
    index_dst_rows,
    is_starter,
)
from nfl.gangstash import (
    GangstashDataError,
    GangstashDataKeyMissing,
    GangstashError,
    GangstashKeyMissing,
    GangstashTruncated,
    capture_pulls,
)
from nfl.gangstash_data import (
    fetch_closing_lines,
    fetch_depth_charts_weekly,
    fetch_game_lines,
    fetch_props_closing,
    fetch_week_injuries,
    parse_player_stat_row,
)
from nfl.injuries import injury_code
from nfl.lines import LinesError
from nfl.names import match_key
from nfl.players import Player
from nfl.projections import score_player
from nfl.props import norm_prop, parse_stamp, prop_field
from nfl.rules import FANDUEL_NFL
from nfl.sim import GameSim, simulate_games
from nfl.sim_efficiency import DataEfficiency, build_efficiency, resolve_run_efficiency
from nfl.sim_feed import resolve_sim_inputs
from nfl.sim_inputs import CarryWeek, SimInputError, SimInputs, TargetWeek, TeamStat
from nfl.sim_team import parse_sim_mode
from nfl.teams import UnmappedTeam, require_fd

_POS_ORDER = ("QB", "RB", "WR", "TE", "DEF")
_CAL_CUTS = (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90)
_PAIRS = ("QB-WR1", "QB-TE1", "QB-RB1", "QB-oppDEF", "QB-oppQB", "WR1-WR2")
_SENSITIVITY = (
    "pass_multiplier",
    "rush_multiplier",
    "pace",
    "red_zone_td_rate",
    "target_carry_shares",
)
# Full per-player draws stay on disk at or below this length. Longer
# ensembles are stored as q01..q99. Scoring still uses the raw draws.
_FULL_DRAW_MAX = 250
_LINEUP_THRESHOLDS = (125.0, 165.0)
_SALARY_MULTIPLES = (2.0, 3.0, 4.0)
_LOSS_MODES = ("board", "sim_placeholder", "sim_data")
_SIM_MODES = ("sim_placeholder", "sim_data")
_METRICS_INSTALL = "pip3 install -r requirements.txt"


class HoldoutMetricsError(RuntimeError):
    """Scoring metrics were requested and NumPy or SciPy is not installed."""


def require_scoring():
    """Import NumPy and ``nfl.calibration``. Only the metrics path calls this."""
    cached = getattr(require_scoring, "_cached", None)
    if cached is not None:
        return cached
    try:
        import numpy as np
        from nfl import calibration as calibration
    except ImportError as exc:
        raise HoldoutMetricsError(
            "numpy and scipy are required for holdout scoring metrics. "
            + _METRICS_INSTALL
        ) from exc
    require_scoring._cached = (np, calibration)
    return require_scoring._cached


def metrics_requested(*, metrics: bool, seeds: list[int], draws_out) -> bool:
    """New scores run only when asked, or when a flag cannot work without them."""
    return bool(metrics) or len(seeds) > 1 or draws_out is not None

_TD_FIELDS = {
    "rushing touchdowns": "rush_tds",
    "rushing tds": "rush_tds",
    "rush tds": "rush_tds",
    "rush td": "rush_tds",
    "player rush tds": "rush_tds",
    "receiving touchdowns": "rec_tds",
    "receiving tds": "rec_tds",
    "rec tds": "rec_tds",
    "rec td": "rec_tds",
    "player rec tds": "rec_tds",
}


@dataclass
class WeekLoad:
    players: list[Player]
    actual_rows: list[dict]
    sim_inputs: SimInputs | None
    missing: list[str]
    notes: list[str]
    injury_rows: list[dict] = field(default_factory=list)
    kickoff: datetime | None = None
    kickoffs: dict = field(default_factory=dict)
    kickoff_note: str = ""


def parse_weeks(raw: str) -> list[int]:
    """``1-18``, ``2``, or ``1,2,5``. Ranges are inclusive."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("weeks is empty")
    out: list[int] = []
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            left, right = piece.split("-", 1)
            start, end = int(left), int(right)
            if end < start:
                raise ValueError(f"week range {piece} is backwards")
            out.extend(range(start, end + 1))
        else:
            out.append(int(piece))
    weeks = []
    for week in out:
        if week < 1 or week > 18:
            raise ValueError(f"week {week} is outside 1-18")
        if week not in weeks:
            weeks.append(week)
    if not weeks:
        raise ValueError("weeks is empty")
    return weeks


def parse_seasons(raw: str | None, season: int | None) -> list[int]:
    seasons: list[int] = []
    if raw:
        for part in str(raw).split(","):
            piece = part.strip()
            if piece:
                seasons.append(int(piece))
    if season is not None and int(season) not in seasons:
        seasons.append(int(season))
    if not seasons:
        raise ValueError("pass --season or --seasons")
    if any(item < 1 for item in seasons):
        raise ValueError("season must be >= 1")
    return seasons


def parse_seeds(raw: str | None, seed: int | None) -> list[int]:
    """``1,2,3`` or the single ``--seed`` when ``raw`` is empty. At most 5."""
    if raw is None or not str(raw).strip():
        if seed is None:
            raise ValueError("pass --seed or --seeds")
        return [int(seed)]
    out: list[int] = []
    for part in str(raw).split(","):
        piece = part.strip()
        if not piece:
            continue
        out.append(int(piece))
    if not out:
        raise ValueError("seeds is empty")
    if len(out) > 5:
        raise ValueError("seeds supports at most 5")
    return out


def is_pregame_starter(player: Player) -> bool:
    """Depth-chart starter at decision time, including a zero-point bust.

    Same ranks as ``is_starter`` (QB/RB/TE 1, WR 1-3, every DEF) without
    the post-game ``played`` filter.
    """
    pos = (player.position or "").upper()
    if pos in {"D", "DEF"}:
        return True
    ranks = _STARTER_RANKS.get(pos)
    return ranks is not None and player.depth_rank in ranks


def _in_population(player: Player, row: dict, population: str) -> bool:
    if population == "played":
        return is_starter(player, row)
    if population == "pregame":
        return is_pregame_starter(player)
    raise ValueError("population must be pregame or played")


def pick_sensitivity_week(weeks: list[int], override: int | None) -> int | None:
    """Last requested week that is not week 1, unless the caller names one."""
    if override is not None:
        return int(override)
    later = [week for week in weeks if week != 1]
    if later:
        return later[-1]
    return weeks[-1] if weeks else None


def load_week(season: int, week: int, *, seed_prior: bool = False) -> WeekLoad:
    """Depth-chart pool for one week. Prior-season seed is opt-in."""
    players, actual_rows, sim_inputs, missing, notes = _load_live(
        None,
        season=season,
        week=week,
    )
    notes = list(notes)
    if seed_prior and int(week) == 1 and int(season) > 1:
        seeded, note = resolve_sim_inputs(
            path=None,
            season=int(season) - 1,
            weeks=[18],
            team_stats_scope="weekly",
        )
        if seeded is not None and not seeded.empty:
            sim_inputs = seeded
            notes.append(note or f"seed-prior-season: {int(season) - 1} week 18")
        else:
            notes.append(
                "seed-prior-season: no prior-season rows; week 1 inputs unchanged"
            )
    injury_rows, kickoffs, kickoff, kickoff_note = _injury_context(int(season), int(week))
    return WeekLoad(
        players=players,
        actual_rows=list(actual_rows or []),
        sim_inputs=sim_inputs,
        missing=list(missing),
        notes=notes,
        injury_rows=injury_rows,
        kickoff=kickoff,
        kickoffs=kickoffs,
        kickoff_note=kickoff_note,
    )


_GANGSTASH_FETCH_ERRORS = (
    GangstashDataKeyMissing,
    GangstashKeyMissing,
    GangstashTruncated,
    GangstashDataError,
    GangstashError,
)
_KICKOFF_FIELDS = ("kickoff_at", "commence_time", "kickoff", "gameday", "game_date")
_KICKOFF_TEAM_FIELDS = (
    "team_fd",
    "team",
    "home_team_fd",
    "away_team_fd",
    "home_team",
    "away_team",
)


def _fetch_rows(fn) -> list[dict]:
    try:
        fetched, _meta = fn()
    except _GANGSTASH_FETCH_ERRORS:
        return []
    return [row for row in (fetched or []) if isinstance(row, dict)]


def _row_kickoff(row: dict) -> datetime | None:
    for key in _KICKOFF_FIELDS:
        stamp = _kickoff_stamp(row.get(key))
        if stamp is not None:
            return stamp
    return None


def _kickoffs_from_rows(rows: list[dict]) -> dict[str, datetime]:
    """Earliest kickoff on each team. A game row stamps both clubs."""
    out: dict[str, datetime] = {}
    for row in rows:
        stamp = _row_kickoff(row)
        if stamp is None:
            continue
        teams: list[str] = []
        for key in _KICKOFF_TEAM_FIELDS:
            team = _fd_team(row.get(key))
            if team and team not in teams:
                teams.append(team)
        for team in teams:
            prev = out.get(team)
            if prev is None or stamp < prev:
                out[team] = stamp
    return out


def _injury_context(
    season: int, week: int
) -> tuple[list[dict], dict[str, datetime], datetime | None, str]:
    """Injuries plus a per-team kickoff.

    ``depth_charts_weekly.kickoff_at`` is the game stamp. Closing ``kickoff``
    is null on the rows we have seen, so it only fills a team that the
    weekly chart did not. ``game_lines.commence_time`` is the last fill.
    An empty map is ``kickoff missing``, not a silent guess.
    """
    rows = _fetch_rows(
        lambda: fetch_week_injuries(season=int(season), week=int(week))
    )
    kickoffs: dict[str, datetime] = {}
    sources: list[str] = []
    depth_rows = _fetch_rows(
        lambda: fetch_depth_charts_weekly(
            season=int(season), week=int(week), pos_grp=None
        )
    )
    from_depth = _kickoffs_from_rows(depth_rows)
    if from_depth:
        kickoffs.update(from_depth)
        sources.append("depth_charts_weekly.kickoff_at")
    closing_rows = _fetch_rows(
        lambda: fetch_closing_lines(season=int(season), week=int(week))
    )
    for team, stamp in _kickoffs_from_rows(closing_rows).items():
        if team not in kickoffs:
            kickoffs[team] = stamp
            if "closing_lines" not in sources:
                sources.append("closing_lines")
    game_rows = _fetch_rows(
        lambda: fetch_game_lines(season=int(season), week=int(week))
    )
    for team, stamp in _kickoffs_from_rows(game_rows).items():
        if team not in kickoffs:
            kickoffs[team] = stamp
            if "game_lines.commence_time" not in sources:
                sources.append("game_lines.commence_time")
    earliest = min(kickoffs.values()) if kickoffs else None
    if not kickoffs:
        note = "kickoff missing"
    else:
        note = "kickoff from " + ", ".join(sources)
    return rows, kickoffs, earliest, note


def draw_percentile(draws: tuple[float, ...] | list[float], actual: float) -> float:
    """Share of draws at or below ``actual``. Empty draws return 0."""
    n = len(draws)
    if n <= 0:
        return 0.0
    below = sum(1 for value in draws if value <= actual)
    return below / n


def quantile(draws: tuple[float, ...] | list[float], q: float) -> float:
    """Linear quantile. ``q`` is in ``[0, 1]``."""
    xs = sorted(float(value) for value in draws)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * float(q)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def covers_p10_p90(draws: tuple[float, ...] | list[float], actual: float) -> bool:
    if len(draws) < 2:
        return False
    return quantile(draws, 0.10) <= float(actual) <= quantile(draws, 0.90)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0.0 or dy == 0.0:
        return None
    return num / (dx * dy)


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    return pearson(_ranks(xs), _ranks(ys))


def sample_sd(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mean = sum(xs) / n
    var = sum((value - mean) ** 2 for value in xs) / (n - 1)
    return math.sqrt(var)


def _mean(xs: list[float]) -> float | None:
    if not xs:
        return None
    return sum(xs) / len(xs)


def _me_mae(pairs: list[tuple[float, float]]) -> dict:
    if not pairs:
        return {"n": 0, "me": None, "mae": None}
    err = [pred - actual for pred, actual in pairs]
    return {
        "n": len(pairs),
        "me": sum(err) / len(err),
        "mae": sum(abs(value) for value in err) / len(err),
    }


def _round(value, digits: int = 4):
    if value is None:
        return None
    return round(float(value), digits)


def _fd_team(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return require_fd(text).fd
    except UnmappedTeam:
        return text.upper()


def index_points_allowed(
    rows: list[dict], *, season: int, week: int
) -> dict[str, float]:
    """``team_fd → points allowed`` (opponent points scored)."""
    out: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("player_name") or row.get("name") or row.get("player"):
            continue
        raw = row.get("points_allowed")
        if raw is None or raw == "":
            continue
        row_season = row.get("season")
        row_week = row.get("week")
        if row_season not in (None, "") and int(row_season) != int(season):
            continue
        if row_week not in (None, "") and int(row_week) != int(week):
            continue
        team = _fd_team(row.get("team_fd") or row.get("team"))
        if not team:
            continue
        try:
            out[team] = float(raw)
        except (TypeError, ValueError):
            continue
    return out


def _role(players: list[Player], team: str, position: str, rank: int) -> Player | None:
    hits = [
        pl
        for pl in players
        if (pl.team or "").upper() == team
        and _position(pl.position) == position
        and pl.depth_rank == rank
    ]
    if not hits:
        return None
    hits.sort(key=lambda pl: pl.pid)
    return hits[0]


def _pair_roles(
    players: list[Player], team: str
) -> dict[str, tuple[Player, Player] | None]:
    qb = _role(players, team, "QB", 1)
    wr1 = _role(players, team, "WR", 1)
    wr2 = _role(players, team, "WR", 2)
    te1 = _role(players, team, "TE", 1)
    rb1 = _role(players, team, "RB", 1)
    defense = next(
        (
            pl
            for pl in players
            if (pl.team or "").upper() == team and _position(pl.position) == "DEF"
        ),
        None,
    )
    opp = (qb.opponent or "").upper() if qb is not None else ""
    opp_qb = _role(players, opp, "QB", 1) if opp else None
    opp_def = next(
        (
            pl
            for pl in players
            if (pl.team or "").upper() == opp and _position(pl.position) == "DEF"
        ),
        None,
    )
    mapping = {
        "QB-WR1": (qb, wr1),
        "QB-TE1": (qb, te1),
        "QB-RB1": (qb, rb1),
        "QB-oppDEF": (qb, opp_def),
        "QB-oppQB": (qb, opp_qb),
        "WR1-WR2": (wr1, wr2),
    }
    return {
        name: pair if pair[0] is not None and pair[1] is not None else None
        for name, pair in mapping.items()
    }


def _scale_method(model: DataEfficiency, name: str, factor: float) -> None:
    original = getattr(model, name)

    def wrapped(*args, _fn=original, _factor=factor, **kwargs):
        return _fn(*args, **kwargs) * _factor

    setattr(model, name, wrapped)


def _scale_pace(row: TeamStat, factor: float) -> TeamStat:
    changes = {}
    if row.plays_per_game is not None:
        changes["plays_per_game"] = float(row.plays_per_game) * factor
    if row.neutral_plays_per_game is not None:
        changes["neutral_plays_per_game"] = float(row.neutral_plays_per_game) * factor
    if row.seconds_per_play is not None and float(row.seconds_per_play) > 0:
        changes["seconds_per_play"] = float(row.seconds_per_play) / factor
    if (
        row.neutral_seconds_per_play is not None
        and float(row.neutral_seconds_per_play) > 0
    ):
        changes["neutral_seconds_per_play"] = (
            float(row.neutral_seconds_per_play) / factor
        )
    if not changes:
        return row
    return replace(row, **changes)


def _scale_rz(row: TeamStat, factor: float) -> TeamStat:
    if row.red_zone_td_rate is None:
        return row
    return replace(row, red_zone_td_rate=float(row.red_zone_td_rate) * factor)


def _starter_keys(players: list[Player], positions: set[str]) -> set[tuple[str, str]]:
    return {
        ((pl.team or "").upper(), match_key(pl.name))
        for pl in players
        if _position(pl.position) in positions and pl.depth_rank == 1
    }


def _scale_target(row: TargetWeek, factor: float) -> TargetWeek:
    share = row.target_share
    if share is None and row.team_targets and float(row.team_targets) > 0:
        share = float(row.targets) / float(row.team_targets)
    if share is None:
        return row
    return replace(row, target_share=float(share) * factor)


def _scale_carry(row: CarryWeek, factor: float) -> CarryWeek:
    return replace(row, carries=float(row.carries) * factor)


def perturb(
    kind: str,
    factor: float,
    inputs: SimInputs,
    players: list[Player],
    *,
    before_week: int,
) -> tuple[SimInputs, DataEfficiency]:
    """One measurement copy. Production ``DataEfficiency`` is not edited."""
    bundle = inputs
    if kind == "pace":
        bundle = replace(
            inputs,
            team_stats=tuple(_scale_pace(row, factor) for row in inputs.team_stats),
        )
    elif kind == "red_zone_td_rate":
        bundle = replace(
            inputs,
            team_stats=tuple(_scale_rz(row, factor) for row in inputs.team_stats),
            team_weeks=tuple(_scale_rz(row, factor) for row in inputs.team_weeks),
        )
    elif kind == "target_carry_shares":
        targets = _starter_keys(players, {"WR", "TE", "RB"})
        rushes = _starter_keys(players, {"RB"})
        scaled_targets = []
        for row in inputs.targets:
            key = (row.team_fd.upper(), match_key(row.player_name))
            scaled_targets.append(
                _scale_target(row, factor) if key in targets else row
            )
        scaled_carries = []
        for row in inputs.carries:
            key = (row.team_fd.upper(), match_key(row.player_name))
            scaled_carries.append(_scale_carry(row, factor) if key in rushes else row)
        bundle = replace(
            inputs,
            targets=tuple(scaled_targets),
            carries=tuple(scaled_carries),
        )
    model = build_efficiency("data", bundle, before_week=before_week)
    if kind == "pass_multiplier":
        _scale_method(model, "pass_multiplier", factor)
    elif kind == "rush_multiplier":
        _scale_method(model, "rush_multiplier", factor)
    return bundle, model


def closing_prop_points(fields: dict[str, float]) -> float | None:
    """FanDuel points from closing lines. Receptions are 0.5 PPR.

    Yardage bonuses use the same thresholds as the sim: +3 at 300 pass,
    100 rush, and 100 rec, applied to the line itself. Rush and receiving
    TD lines are added when the row has them.
    """
    sc = FANDUEL_NFL.scoring
    pts = 0.0
    n = 0
    if "pass_yds" in fields:
        pts += fields["pass_yds"] * sc["pass_yd"]
        if fields["pass_yds"] >= 300:
            pts += sc["bonus_pass_yd_300"]
        n += 1
    if "pass_tds" in fields:
        pts += fields["pass_tds"] * sc["pass_td"]
        n += 1
    if "rush_yds" in fields:
        pts += fields["rush_yds"] * sc["rush_yd"]
        if fields["rush_yds"] >= 100:
            pts += sc["bonus_rush_yd_100"]
        n += 1
    if "rec_yds" in fields:
        pts += fields["rec_yds"] * sc["rec_yd"]
        if fields["rec_yds"] >= 100:
            pts += sc["bonus_rec_yd_100"]
        n += 1
    if "receptions" in fields:
        pts += fields["receptions"] * sc["rec"]
        n += 1
    if "rush_tds" in fields:
        pts += fields["rush_tds"] * sc["rush_td"]
        n += 1
    if "rec_tds" in fields:
        pts += fields["rec_tds"] * sc["rec_td"]
        n += 1
    if n == 0:
        return None
    return pts


def _prop_name(row: dict) -> str:
    return str(row.get("player_name") or row.get("player") or row.get("name") or "")


def _prop_line(row: dict) -> float | None:
    for key in ("line", "closing_line", "consensus_line", "line_value", "closing"):
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _prop_stat(row: dict) -> str | None:
    raw = str(row.get("prop") or row.get("market") or row.get("stat") or "")
    field = prop_field(raw)
    if field:
        return field
    return _TD_FIELDS.get(norm_prop(raw))


def index_prop_lines(rows: list[dict], weeks: set[int]) -> dict[tuple, dict[str, float]]:
    """``(week, team, match_key) → {stat: line}``."""
    out: dict[tuple, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        week = row.get("week")
        try:
            week_i = int(week)
        except (TypeError, ValueError):
            continue
        if week_i not in weeks:
            continue
        name = _prop_name(row)
        stat = _prop_stat(row)
        line = _prop_line(row)
        if not name or stat is None or line is None:
            continue
        team = _fd_team(row.get("team_fd") or row.get("team"))
        key = (week_i, team, match_key(name))
        out.setdefault(key, {})[stat] = line
    return out


def fetch_prop_rows(season: int) -> tuple[list[dict], str]:
    """Closing props for one season. Missing data is an empty list and a note."""
    try:
        rows, _meta = fetch_props_closing(season=season)
    except (
        GangstashDataKeyMissing,
        GangstashKeyMissing,
        GangstashTruncated,
        GangstashDataError,
        GangstashError,
    ) as exc:
        return [], f"props_closing: skipped ({exc})"
    if not rows:
        return [], "props_closing: no rows"
    return [row for row in rows if isinstance(row, dict)], ""


def _calibration_block(rows: list[tuple[str, float, bool]]) -> dict:
    """``rows`` is ``(position, percentile, covered)``."""
    grouped: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for pos, pct, covered in rows:
        grouped[pos].append((pct, covered))
        grouped["ALL"].append((pct, covered))
    out = {}
    for pos in (*_POS_ORDER, "ALL"):
        bucket = grouped.get(pos) or []
        n = len(bucket)
        if n == 0:
            out[pos] = {"n": 0, "below": {}, "p10_p90": None}
            continue
        below = {}
        for cut in _CAL_CUTS:
            label = str(int(round(cut * 100)))
            below[label] = sum(1 for pct, _covered in bucket if pct <= cut) / n
        out[pos] = {
            "n": n,
            "below": below,
            "p10_p90": sum(1 for _pct, covered in bucket if covered) / n,
        }
    return out


def _error_block(buckets: dict[str, list[tuple[float, float]]]) -> dict:
    out = {}
    for pos in _POS_ORDER:
        out[pos] = _me_mae(buckets.get(pos) or [])
    return out


def _rank_average(weekly: list[float]) -> float | None:
    usable = [value for value in weekly if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def _game_actuals(
    players: list[Player], allowed: dict[str, float]
) -> list[dict]:
    games: dict[str, list[Player]] = defaultdict(list)
    for pl in players:
        game = pl.game or ""
        if "@" not in game:
            continue
        games[game].append(pl)
    out = []
    for game, group in games.items():
        away, home = (part.strip().upper() for part in game.split("@", 1))
        if away not in allowed or home not in allowed:
            continue
        home_pts = allowed[away]
        away_pts = allowed[home]
        home_pl = next((pl for pl in group if (pl.team or "").upper() == home), None)
        spread = home_pl.spread if home_pl is not None else None
        total = home_pl.total if home_pl is not None else None
        margin = home_pts - away_pts
        closing_margin = None if spread is None else -float(spread)
        out.append(
            {
                "game": game,
                "away": away,
                "home": home,
                "margin": margin,
                "total": home_pts + away_pts,
                "closing_margin": closing_margin,
                "closing_total": None if total is None else float(total),
            }
        )
    return out


def _sim_game_sds(sim: GameSim) -> dict[str, tuple[float | None, float | None]]:
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for game, _away, _home, away_pts, home_pts in sim.game_draws:
        grouped[game].append((away_pts, home_pts))
    out = {}
    for game, rows in grouped.items():
        margins = [home - away for away, home in rows]
        totals = [home + away for away, home in rows]
        out[game] = (sample_sd(margins), sample_sd(totals))
    return out


def _pair_correlation(
    draws: dict[str, tuple[float, ...]], left: Player, right: Player
) -> float | None:
    xs = draws.get(left.pid)
    ys = draws.get(right.pid)
    if not xs or not ys:
        return None
    n = min(len(xs), len(ys))
    return pearson(list(xs[:n]), list(ys[:n]))


def _sensitivity(
    players: list[Player],
    inputs: SimInputs,
    baseline: dict[str, float],
    *,
    week: int,
    n: int,
    seed: int,
    sim_mode: str = "off",
) -> dict:
    report = {}
    for kind in _SENSITIVITY:
        report[kind] = {}
        for direction, factor in (("plus", 1.1), ("minus", 0.9)):
            bundle, model = perturb(
                kind, factor, inputs, players, before_week=week
            )
            sim = simulate_games(
                players,
                n=n,
                seed=seed,
                inputs=bundle,
                efficiency=model,
                sim_mode=sim_mode,
            )
            buckets: dict[str, list[float]] = defaultdict(list)
            for pl in players:
                stats = sim.by_pid.get(pl.pid)
                base = baseline.get(pl.pid)
                if stats is None or base is None:
                    continue
                buckets[_position(pl.position)].append(float(stats.mean) - base)
            report[kind][direction] = {
                pos: _mean(buckets.get(pos) or []) for pos in _POS_ORDER
            }
    return report


def _actual_fd(
    player: Player,
    indexed: dict,
    dst_by_team: dict,
) -> tuple[float, dict] | None:
    found = _resolve_actual(player, indexed, {}, dst_by_team)
    if found is None:
        return None
    actual, row, _how = found
    return actual, row


def index_actual_ids(rows: list[dict]) -> dict[str, dict]:
    """``gsis_id → {fd_points, row}``. Later rows overwrite. Name is not required."""
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        gsis = str(row.get("gsis_id") or "").strip()
        fd = row.get("fd_points")
        item = row
        if not gsis or fd is None:
            parsed = parse_player_stat_row(row)
            if parsed is None:
                continue
            gsis = gsis or str(parsed.get("gsis_id") or "").strip()
            if fd is None:
                fd = parsed.get("fd_points")
        if not gsis or fd is None:
            continue
        out[gsis] = {"fd_points": float(fd), "row": item}
    return out


def _resolve_actual(
    player: Player,
    by_name: dict,
    by_id: dict,
    dst_by_team: dict,
) -> tuple[float, dict, str] | None:
    """Box score for one pool row. Id first, then name, then team DST."""
    pid = (player.pid or "").strip()
    if pid and pid in by_id:
        info = by_id[pid]
        return float(info["fd_points"]), info["row"], "id"
    key = ((player.team or "").upper(), match_key(player.name))
    info = by_name.get(key)
    if info is not None:
        return float(info["fd_points"]), info["row"], "name"
    if _position(player.position) == "DEF":
        info = dst_by_team.get((player.team or "").upper())
        if info is not None:
            return float(info["fd_points"]), info["row"], "team"
    return None


_OUT_CODES = frozenset({"O", "IR"})


def _injury_status(row: dict) -> str:
    """Gangstash sends ``report_status``. ``status`` is the older name."""
    return str(row.get("report_status") or row.get("status") or "").strip()


def _injury_name(row: dict) -> str:
    """Gangstash sends ``full_name``. ``player_name`` is the older name."""
    return str(
        row.get("full_name") or row.get("player_name") or row.get("name") or ""
    ).strip()


def _injury_ids(row: dict) -> set[str]:
    found = set()
    for key in ("gsis_id", "player_key", "player_id"):
        text = str(row.get(key) or "").strip()
        if text:
            found.add(text)
    return found


def _injury_hit(player: Player, rows: list[dict]) -> dict | None:
    """Injury row for this player. Id wins over the name."""
    pid = (player.pid or "").strip()
    name_key = (_fd_team(player.team), match_key(player.name))
    by_name = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if pid and pid in _injury_ids(row):
            return row
        team = _fd_team(row.get("team_fd") or row.get("team"))
        name = _injury_name(row)
        if name and (team, match_key(name)) == name_key and by_name is None:
            by_name = row
    return by_name


def _kickoff_for(
    player: Player,
    kickoffs: dict | None,
    fallback: datetime | None,
) -> datetime | None:
    team = _fd_team(player.team)
    if kickoffs and team in kickoffs:
        return kickoffs[team]
    return fallback


def _missing_starter_fate(
    player: Player,
    rows: list[dict],
    kickoff: datetime | None,
) -> str:
    """What to do with a pregame starter who has no box score.

    ``exclude`` is pre-kickoff Out or IR. A row with no ``date_modified``
    is the weekly report and counts as pre-game. A timestamp needs that
    player's kickoff; a missing kickoff is ``unresolved_kickoff`` and the
    player stays. No injury row at all is ``zero_no_injury``.
    """
    row = _injury_hit(player, rows)
    if row is None:
        return "zero_no_injury"
    if injury_code(_injury_status(row)) not in _OUT_CODES:
        return "zero"
    raw = row.get("date_modified")
    if raw in (None, ""):
        return "exclude"
    stamp = parse_stamp(raw)
    if stamp is None or kickoff is None:
        return "unresolved_kickoff"
    if stamp < kickoff:
        return "exclude"
    return "zero"


def _excluded_out(player: Player, rows: list[dict], kickoff: datetime | None) -> bool:
    """True when a missing-stats starter is Out or IR before kickoff."""
    return _missing_starter_fate(player, rows, kickoff) == "exclude"


def _draw_length(sim: GameSim, pid: str) -> int:
    return len(sim.draws.get(pid) or ())


def _draws_aligned(sims: dict, pid: str, n_draws: int) -> bool:
    return all(_draw_length(sim, pid) == int(n_draws) for sim in sims.values())


def _new_seed_bag() -> dict:
    positions = (*_POS_ORDER, "ALL")
    return {
        "starter_abs": {mode: {pos: [] for pos in positions} for mode in _LOSS_MODES},
        "rank": {mode: {pos: [] for pos in _POS_ORDER} for mode in _LOSS_MODES},
        "cover10": {mode: [] for mode in _SIM_MODES},
        "cover25": {mode: [] for mode in _SIM_MODES},
        "crps": {mode: [] for mode in _SIM_MODES},
        "pit": {mode: [] for mode in _SIM_MODES},
        "total_sd": {mode: [] for mode in _SIM_MODES},
        "margin_sd": {mode: [] for mode in _SIM_MODES},
        "sim_corr": {mode: {name: [] for name in _PAIRS} for mode in _SIM_MODES},
        "resid_actual": {mode: {name: [] for name in _PAIRS} for mode in _SIM_MODES},
        "resid_sim": {mode: {name: [] for name in _PAIRS} for mode in _SIM_MODES},
    }


def _new_row_sink() -> dict:
    return {
        "block": [],
        "starter": [],
        "salary": [],
        "actual": [],
        "abs": {mode: [] for mode in _LOSS_MODES},
        "crps": {mode: [] for mode in _SIM_MODES},
        "cover10": {mode: [] for mode in _SIM_MODES},
        "cover25": {mode: [] for mode in _SIM_MODES},
        "pit": {mode: [] for mode in _SIM_MODES},
        "price_block": [],
        "brier": {mode: {mult: [] for mult in _SALARY_MULTIPLES} for mode in _SIM_MODES},
        "log_score": {mode: {mult: [] for mult in _SALARY_MULTIPLES} for mode in _SIM_MODES},
    }


def _pack_draws(series):
    np, calibration = require_scoring()
    if int(series.size) > _FULL_DRAW_MAX:
        return "quantiles", np.quantile(series, calibration.QUANTILES)
    return "draws", np.asarray(series, dtype=np.float64)


def _record_pairs(bag: dict, players: list[Player], joined_rows: list[dict], sims: dict) -> None:
    np, _calibration = require_scoring()
    actual_by_pid = {item["player"].pid: item["actual"] for item in joined_rows}
    mean_by_pid: dict[str, dict[str, float]] = {}
    for item in joined_rows:
        pl = item["player"]
        mean_by_pid[pl.pid] = {}
        for mode, sim in sims.items():
            series = sim.draws.get(pl.pid)
            if series:
                mean_by_pid[pl.pid][mode] = float(np.mean(np.asarray(series, dtype=float)))
    teams = sorted({(pl.team or "").upper() for pl in players if pl.team})
    for team in teams:
        roles = _pair_roles(players, team)
        qb = _role(players, team, "QB", 1)
        opp = (qb.opponent or "").upper() if qb is not None else ""
        for name, pair in roles.items():
            if pair is None:
                continue
            if name == "QB-oppQB" and (not opp or team >= opp):
                continue
            left, right = pair
            left_actual = actual_by_pid.get(left.pid)
            right_actual = actual_by_pid.get(right.pid)
            for mode, sim in sims.items():
                rho = _pair_correlation(sim.draws, left, right)
                if rho is not None:
                    bag["sim_corr"][mode][name].append(rho)
                    bag["resid_sim"][mode][name].append(rho)
                left_mean = mean_by_pid.get(left.pid, {}).get(mode)
                right_mean = mean_by_pid.get(right.pid, {}).get(mode)
                if (
                    left_actual is None
                    or right_actual is None
                    or left_mean is None
                    or right_mean is None
                ):
                    continue
                bag["resid_actual"][mode][name].append(
                    (left_actual - left_mean, right_actual - right_mean)
                )


def _absorb_seed(
    bag: dict,
    joined_rows: list[dict],
    players: list[Player],
    sims: dict,
    week_games: list[dict],
    *,
    rng,
    row_sink: dict | None,
    draw_sink: list | None,
    season: int,
    week: int,
    seed_i: int,
    n_draws: int,
) -> None:
    """One seed's headline pieces. ``row_sink`` is the primary seed only."""
    np, calibration = require_scoring()
    block = f"{int(season)}-{int(week)}"
    by_pos = {
        mode: {pos: ([], []) for pos in _POS_ORDER} for mode in ("board", *_SIM_MODES)
    }
    shared: list[dict] = []
    for item in joined_rows:
        pl = item["player"]
        if not _draws_aligned(sims, pl.pid, n_draws):
            continue
        actual = float(item["actual"])
        pos = item["position"]
        board = float(item["board"])
        by_pos["board"][pos][0].append(board)
        by_pos["board"][pos][1].append(actual)
        if item["starter"]:
            err = abs(board - actual)
            bag["starter_abs"]["board"][pos].append(err)
            bag["starter_abs"]["board"]["ALL"].append(err)
        series = {}
        preds = {}
        ready = True
        for mode in _SIM_MODES:
            sim = sims[mode]
            stats = sim.by_pid.get(pl.pid)
            arr = np.asarray(sim.draws.get(pl.pid) or (), dtype=float)
            if stats is None or int(arr.size) != int(n_draws):
                ready = False
                break
            series[mode] = arr
            preds[mode] = float(stats.mean)
            by_pos[mode][pos][0].append(preds[mode])
            by_pos[mode][pos][1].append(actual)
            if item["starter"]:
                err = abs(preds[mode] - actual)
                bag["starter_abs"][mode][pos].append(err)
                bag["starter_abs"][mode]["ALL"].append(err)
        if ready:
            shared.append(
                {
                    "player": pl,
                    "actual": actual,
                    "board": board,
                    "position": pos,
                    "starter": bool(item["starter"]),
                    "salary": int(pl.salary or 0),
                    "series": series,
                    "preds": preds,
                }
            )
    for mode in ("board", *_SIM_MODES):
        for pos in _POS_ORDER:
            rho = spearman(by_pos[mode][pos][0], by_pos[mode][pos][1])
            if rho is not None:
                bag["rank"][mode][pos].append(rho)
    actual_game_keys = {game["game"] for game in week_games}
    for mode in _SIM_MODES:
        sds = _sim_game_sds(sims[mode])
        for game, (margin_sd, total_sd) in sds.items():
            if game not in actual_game_keys:
                continue
            if margin_sd is not None:
                bag["margin_sd"][mode].append(margin_sd)
            if total_sd is not None:
                bag["total_sd"][mode].append(total_sd)
    if shared:
        y = np.asarray([row["actual"] for row in shared], dtype=float)
        starter = np.asarray([row["starter"] for row in shared], dtype=bool)
        salary = np.asarray([row["salary"] for row in shared], dtype=float)
        if row_sink is not None:
            row_sink["block"].extend([block] * len(shared))
            row_sink["starter"].extend(bool(flag) for flag in starter)
            row_sink["salary"].extend(salary.tolist())
            row_sink["actual"].extend(y.tolist())
            row_sink["abs"]["board"].extend(
                np.abs(np.asarray([row["board"] for row in shared], dtype=float) - y).tolist()
            )
        for mode in _SIM_MODES:
            draws = np.stack([row["series"][mode] for row in shared])
            pred = np.asarray([row["preds"][mode] for row in shared], dtype=float)
            crps = calibration.crps_ensemble(draws, y)
            hit10 = calibration.interval_hit(draws, y, 0.10, 0.90)
            hit25 = calibration.interval_hit(draws, y, 0.25, 0.75)
            pits = calibration.pit(draws, y, rng)
            if np.any(starter):
                bag["cover10"][mode].extend(hit10[starter].astype(float).tolist())
                bag["cover25"][mode].extend(hit25[starter].astype(float).tolist())
                bag["crps"][mode].extend(crps[starter].astype(float).tolist())
                bag["pit"][mode].extend(pits[starter].astype(float).tolist())
            if row_sink is not None:
                row_sink["abs"][mode].extend(np.abs(pred - y).tolist())
                row_sink["crps"][mode].extend(crps.tolist())
                row_sink["cover10"][mode].extend(hit10.astype(float).tolist())
                row_sink["cover25"][mode].extend(hit25.astype(float).tolist())
                row_sink["pit"][mode].extend(pits.tolist())
                priced = calibration.salary_multiple_scores(
                    draws, y, salary, _SALARY_MULTIPLES
                )
                if priced["n"]:
                    if mode == _SIM_MODES[0]:
                        row_sink["price_block"].extend([block] * priced["n"])
                    for offset, mult in enumerate(priced["multiples"]):
                        row_sink["brier"][mode][mult].extend(priced["brier"][:, offset].tolist())
                        row_sink["log_score"][mode][mult].extend(
                            priced["log_score"][:, offset].tolist()
                        )
            if draw_sink is not None:
                for i, row in enumerate(shared):
                    series = row["series"][mode]
                    kind, values = _pack_draws(series)
                    draw_sink.append(
                        {
                            "kind": kind,
                            "values": values,
                            "actual": float(y[i]),
                            "mean": float(np.mean(series)),
                            "salary": int(row["salary"]),
                            "season": int(season),
                            "week": int(week),
                            "seed": int(seed_i),
                            "starter": bool(row["starter"]),
                            "mode": mode,
                            "pid": row["player"].pid,
                            "position": row["position"],
                            "block": block,
                        }
                    )
    _record_pairs(bag, players, joined_rows, sims)


def _finalize_seed(bag: dict) -> dict[str, float]:
    _np, calibration = require_scoring()
    out: dict[str, float] = {}
    for mode in _LOSS_MODES:
        for pos in (*_POS_ORDER, "ALL"):
            xs = bag["starter_abs"][mode][pos]
            if xs:
                out[f"starter_mae.{mode}.{pos}"] = sum(xs) / len(xs)
        for pos in _POS_ORDER:
            xs = bag["rank"][mode][pos]
            if xs:
                out[f"rank.{mode}.{pos}"] = sum(xs) / len(xs)
    for mode in _SIM_MODES:
        if bag["cover10"][mode]:
            out[f"coverage.{mode}.p10_p90"] = sum(bag["cover10"][mode]) / len(bag["cover10"][mode])
        if bag["cover25"][mode]:
            out[f"coverage.{mode}.p25_p75"] = sum(bag["cover25"][mode]) / len(bag["cover25"][mode])
        if bag["crps"][mode]:
            out[f"crps.{mode}"] = sum(bag["crps"][mode]) / len(bag["crps"][mode])
        if bag["pit"][mode]:
            out[f"pit_chi2_p.{mode}"] = float(
                calibration.pit_histogram(bag["pit"][mode])["p"]
            )
        if bag["total_sd"][mode]:
            out[f"game_total_sd.{mode}"] = sum(bag["total_sd"][mode]) / len(bag["total_sd"][mode])
        if bag["margin_sd"][mode]:
            out[f"game_margin_sd.{mode}"] = sum(bag["margin_sd"][mode]) / len(bag["margin_sd"][mode])
        for name in _PAIRS:
            sim_list = bag["resid_sim"][mode][name]
            act_pairs = bag["resid_actual"][mode][name]
            c_sim = _mean(sim_list)
            c_act = pearson(
                [pair[0] for pair in act_pairs],
                [pair[1] for pair in act_pairs],
            )
            if c_sim is not None:
                out[f"resid_sim.{mode}.{name}"] = c_sim
                out[f"corr_sim.{mode}.{name}"] = _mean(bag["sim_corr"][mode][name])
            if c_act is not None:
                out[f"resid_actual.{mode}.{name}"] = c_act
            checked = calibration.fisher_z(c_sim, c_act, len(act_pairs))
            if checked["z"] is not None:
                out[f"fisher_z.{mode}.{name}"] = checked["z"]
    return out


def _residual_report(bag: dict) -> dict:
    _np, calibration = require_scoring()
    out = {}
    for name in _PAIRS:
        out[name] = {}
        for mode in _SIM_MODES:
            act_pairs = bag["resid_actual"][mode][name]
            c_act = pearson(
                [pair[0] for pair in act_pairs],
                [pair[1] for pair in act_pairs],
            )
            c_sim = _mean(bag["resid_sim"][mode][name])
            checked = calibration.fisher_z(c_sim, c_act, len(act_pairs))
            out[name][mode] = {
                "actual": c_act,
                "n_actual": len(act_pairs),
                "sim": c_sim,
                "n_sim": len(bag["resid_sim"][mode][name]),
                "se": checked["se"],
                "z": checked["z"],
                "flag": checked["flag"],
            }
    return out


def _mask_boot(values, starter, blocks, *, starters_only: bool) -> dict:
    np, calibration = require_scoring()
    vals = np.asarray(values, dtype=float)
    flags = np.asarray(starter, dtype=bool)
    labels = np.asarray(blocks)
    if starters_only:
        vals = vals[flags]
        labels = labels[flags]
    return calibration.block_bootstrap(vals, labels, n_boot=calibration.N_BOOT, seed=0)


def _scores_report(row_sink: dict) -> dict:
    """Block-bootstrap the primary seed. Lineup totals are omitted here."""
    if not row_sink["block"]:
        return {
            "note": "no joined rows",
            "lineup": _lineup_note(),
            "salary_thresholds": {"n": 0, "note": "no priced rows"},
        }
    out = {
        "orientation": (
            "crps and brier are losses (lower is better). "
            "log_score is o*log(p)+(1-o)*log(1-p) after clipping p to "
            "[1e-6, 1-1e-6] (higher is better)."
        ),
        "pools": {},
        "salary_thresholds": _salary_block(row_sink),
        "lineup": _lineup_note(),
    }
    for pool, starters_only in (("starters", True), ("full", False)):
        pool_out = {}
        for mode in _SIM_MODES:
            hist_values = row_sink["pit"][mode]
            flags = row_sink["starter"]
            if starters_only:
                hist_values = [
                    value
                    for value, flag in zip(row_sink["pit"][mode], flags)
                    if flag
                ]
            pool_out[mode] = {
                "crps": _mask_boot(row_sink["crps"][mode], flags, row_sink["block"], starters_only=starters_only),
                "p10_p90": _mask_boot(row_sink["cover10"][mode], flags, row_sink["block"], starters_only=starters_only),
                "p25_p75": _mask_boot(row_sink["cover25"][mode], flags, row_sink["block"], starters_only=starters_only),
                "pit_mean": _mask_boot(row_sink["pit"][mode], flags, row_sink["block"], starters_only=starters_only),
                "pit_histogram": require_scoring()[1].pit_histogram(hist_values),
            }
        out["pools"][pool] = pool_out
    return out


def _salary_block(row_sink: dict) -> dict:
    _np, calibration = require_scoring()
    n = len(row_sink["price_block"])
    if n == 0:
        return {
            "n": 0,
            "multiples": [float(m) for m in _SALARY_MULTIPLES],
            "note": "no salary on the depth-chart pool; thresholds need salary > 0",
            "levels": {},
        }
    levels = {}
    for mode in _SIM_MODES:
        levels[mode] = {}
        for mult in _SALARY_MULTIPLES:
            label = f"{int(mult)}x"
            levels[mode][label] = {
                "brier": calibration.block_bootstrap(
                    row_sink["brier"][mode][mult],
                    row_sink["price_block"],
                    n_boot=calibration.N_BOOT,
                    seed=0,
                ),
                "log_score": calibration.block_bootstrap(
                    row_sink["log_score"][mode][mult],
                    row_sink["price_block"],
                    n_boot=calibration.N_BOOT,
                    seed=0,
                ),
            }
    return {
        "n": n,
        "multiples": [float(m) for m in _SALARY_MULTIPLES],
        "note": "threshold = multiple * salary / 1000",
        "levels": levels,
    }


def _lineup_note() -> dict:
    return {
        "thresholds": [float(t) for t in _LINEUP_THRESHOLDS],
        "note": (
            "no lineup draws on the depth-chart holdout; "
            "salary is omitted and the lineup solver is not run"
        ),
        "levels": {},
    }


def _paired_report(row_sink: dict) -> dict:
    if not row_sink["block"]:
        return {}
    np, calibration = require_scoring()
    out = {}
    comparisons = (
        ("board", "sim_placeholder"),
        ("board", "sim_data"),
        ("sim_placeholder", "sim_data"),
    )
    for pool, starters_only in (("starters", True), ("full", False)):
        flags = np.asarray(row_sink["starter"], dtype=bool)
        blocks = np.asarray(row_sink["block"])
        if starters_only:
            keep = flags
        else:
            keep = np.ones(flags.shape[0], dtype=bool)
        pool_out = {}
        kept_blocks = blocks[keep]
        for left, right in comparisons:
            key = f"{left}_minus_{right}"
            entry = {
                "abs_error": calibration.paired_difference(
                    np.asarray(row_sink["abs"][left], dtype=float)[keep],
                    np.asarray(row_sink["abs"][right], dtype=float)[keep],
                    kept_blocks,
                    n_boot=calibration.N_BOOT,
                    seed=0,
                )
            }
            if left in _SIM_MODES and right in _SIM_MODES:
                entry["crps"] = calibration.paired_difference(
                    np.asarray(row_sink["crps"][left], dtype=float)[keep],
                    np.asarray(row_sink["crps"][right], dtype=float)[keep],
                    kept_blocks,
                    n_boot=calibration.N_BOOT,
                    seed=0,
                )
            pool_out[key] = entry
        out[pool] = pool_out
    return out


def write_draw_archive(path: Path, rows: list[dict]) -> dict:
    """Persist draws, or q01..q99 when each row is longer than ``_FULL_DRAW_MAX``."""
    np, calibration = require_scoring()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        np.savez_compressed(path, kind=np.array(["empty"]))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"path": str(path), "kind": "empty", "n_rows": 0, "sha256": digest}
    kind = rows[0]["kind"]
    values = np.stack([np.asarray(row["values"], dtype=np.float64) for row in rows])
    payload = {
        "kind": np.array([kind]),
        "values": values,
        "actual": np.asarray([row["actual"] for row in rows], dtype=np.float64),
        "mean": np.asarray([row["mean"] for row in rows], dtype=np.float64),
        "salary": np.asarray([row["salary"] for row in rows], dtype=np.int64),
        "season": np.asarray([row["season"] for row in rows], dtype=np.int32),
        "week": np.asarray([row["week"] for row in rows], dtype=np.int32),
        "seed": np.asarray([row["seed"] for row in rows], dtype=np.int32),
        "starter": np.asarray([row["starter"] for row in rows], dtype=np.bool_),
        "mode": np.asarray([row["mode"] for row in rows]),
        "pid": np.asarray([row["pid"] for row in rows]),
        "position": np.asarray([row["position"] for row in rows]),
        "block": np.asarray([row["block"] for row in rows]),
    }
    if kind == "quantiles":
        payload["quantile"] = np.asarray(calibration.QUANTILES, dtype=np.float64)
    np.savez_compressed(path, **payload)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    info = {
        "path": str(path),
        "kind": kind,
        "n_rows": len(rows),
        "sha256": digest,
    }
    if kind == "draws":
        info["n_draws"] = int(values.shape[1])
    else:
        info["quantiles"] = "q01..q99"
    return info


def git_state(root: Path | None = None) -> dict:
    repo = root or Path(__file__).resolve().parents[1]
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=repo,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return {"git_sha": None, "git_dirty": None}
    return {"git_sha": sha, "git_dirty": dirty}


def build_manifest(*, argv: list[str], report: dict, pulls: list[dict], draws: dict | None) -> dict:
    git = git_state()
    return {
        "git_sha": git["git_sha"],
        "git_dirty": git["git_dirty"],
        "seeds": list(report.get("seeds") or []),
        "seed": report.get("seed"),
        "draws": {
            "n": report.get("n"),
            **(draws or {}),
        },
        "population": report.get("population"),
        "argv": list(argv),
        "python": platform.python_version(),
        "numpy": _loaded_version("numpy"),
        "scipy": _loaded_version("scipy"),
        "datasets": list(pulls),
    }


def _loaded_version(name: str) -> str | None:
    """Version of a library already imported by the metrics path. Does not import it."""
    mod = sys.modules.get(name)
    if mod is None:
        return None
    version = getattr(mod, "__version__", None)
    return None if version is None else str(version)


def _mc_block(per_seed: list[dict]) -> dict:
    _np, calibration = require_scoring()
    keys = sorted({key for item in per_seed for key in item})
    out = {}
    for key in keys:
        vals = [item[key] for item in per_seed if key in item and item[key] is not None]
        if vals:
            out[key] = calibration.monte_carlo_summary(vals)
    return out


def run_holdout(
    seasons: list[int],
    weeks: list[int],
    *,
    n: int = 3000,
    seed: int = 1,
    seeds: list[int] | None = None,
    population: str = "pregame",
    seed_prior_season: bool = False,
    sensitivity_week: int | None = None,
    run_sensitivity: bool = True,
    load=None,
    prop_fetch=None,
    draws_out: str | Path | None = None,
    metrics: bool = False,
    sim_mode: str = "off",
) -> dict:
    """Score every requested week. Network stays inside ``load`` and props."""
    score_mode = parse_sim_mode(sim_mode)
    if population not in ("pregame", "played"):
        raise ValueError("population must be pregame or played")
    seed_list = [int(item) for item in (seeds if seeds is not None else [seed])]
    if not seed_list:
        raise ValueError("seeds is empty")
    if len(seed_list) > 5:
        raise ValueError("seeds supports at most 5")
    primary = seed_list[0]
    want_metrics = metrics_requested(
        metrics=metrics, seeds=seed_list, draws_out=draws_out
    )
    if want_metrics:
        np, _scoring = require_scoring()
        pit_rng = [np.random.default_rng(10_000 + int(item)) for item in seed_list]
        seed_bags = [_new_seed_bag() for _ in seed_list]
        row_sink = _new_row_sink()
        draw_sink: list[dict] = []
    else:
        pit_rng = []
        seed_bags = []
        row_sink = None
        draw_sink = []
    loader = load or load_week
    props_of = prop_fetch or fetch_prop_rows
    draws_n = max(1, int(n))
    sens_week = pick_sensitivity_week(weeks, sensitivity_week) if run_sensitivity else None
    errors = {
        pool: {mode: defaultdict(list) for mode in ("board", "sim_placeholder", "sim_data")}
        for pool in ("starters", "full")
    }
    calibration = {
        mode: {pool: [] for pool in ("starters", "full")}
        for mode in ("sim_placeholder", "sim_data")
    }
    rank_week: dict[str, dict[str, list[float]]] = {
        mode: {pos: [] for pos in _POS_ORDER}
        for mode in ("board", "sim_placeholder", "sim_data")
    }
    actual_pairs: dict[str, list[tuple[float, float]]] = {name: [] for name in _PAIRS}
    sim_pairs = {
        mode: {name: [] for name in _PAIRS}
        for mode in ("sim_placeholder", "sim_data")
    }
    actual_margins: list[float] = []
    actual_totals: list[float] = []
    residual_margins: list[float] = []
    residual_totals: list[float] = []
    sim_margin_sd = {"sim_placeholder": [], "sim_data": []}
    sim_total_sd = {"sim_placeholder": [], "sim_data": []}
    prop_joined: list[tuple[str, float, float, float]] = []
    prop_notes: list[str] = []
    failed: list[dict] = []
    scored: list[dict] = []
    join_counts = _empty_join()
    draw_skip_rows: list[dict] = []
    kickoff_weeks: list[dict] = []
    headline = {"with": _empty_headline(), "without": _empty_headline()}
    sens_payload = None
    sens_target = None
    week_set = set(weeks)

    for season in seasons:
        prop_rows, prop_note = props_of(int(season))
        if prop_note:
            prop_notes.append(f"{season}: {prop_note}")
        prop_index = index_prop_lines(prop_rows, week_set)
        for week in weeks:
            print(f"holdout season {season} week {week}", file=sys.stderr)
            try:
                loaded = loader(
                    int(season),
                    int(week),
                    seed_prior=bool(seed_prior_season) and int(week) == 1,
                )
            except (
                DepthPoolError,
                LinesError,
                SimInputError,
                GangstashDataKeyMissing,
                GangstashKeyMissing,
                GangstashTruncated,
                GangstashDataError,
                GangstashError,
            ) as exc:
                failed.append({"season": int(season), "week": int(week), "error": str(exc)})
                print(f"holdout skip {season} week {week}: {exc}", file=sys.stderr)
                continue
            players = loaded.players
            indexed = index_actual_rows(loaded.actual_rows)
            dst_by_team = index_dst_rows(
                loaded.actual_rows, season=season, week=week
            )
            placeholder_model = build_efficiency("placeholder", loaded.sim_inputs)
            # Primary seed is simulated first, in the same order as a one-seed run.
            primary_placeholder = simulate_games(
                players,
                n=draws_n,
                seed=int(primary),
                inputs=loaded.sim_inputs,
                efficiency=placeholder_model,
                sim_mode=score_mode,
            )
            data_model, data_mode, data_note = resolve_run_efficiency(
                "data",
                loaded.sim_inputs,
                before_week=week,
            )
            primary_data = simulate_games(
                players,
                n=draws_n,
                seed=int(primary),
                inputs=loaded.sim_inputs,
                efficiency=data_model,
                sim_mode=score_mode,
            )
            if data_note:
                print(data_note, file=sys.stderr)
            sims_by_seed = [
                {
                    "sim_placeholder": primary_placeholder,
                    "sim_data": primary_data,
                }
            ]
            for seed_i in seed_list[1:]:
                sims_by_seed.append(
                    {
                        "sim_placeholder": simulate_games(
                            players,
                            n=draws_n,
                            seed=int(seed_i),
                            inputs=loaded.sim_inputs,
                            efficiency=placeholder_model,
                            sim_mode=score_mode,
                        ),
                        "sim_data": simulate_games(
                            players,
                            n=draws_n,
                            seed=int(seed_i),
                            inputs=loaded.sim_inputs,
                            efficiency=data_model,
                            sim_mode=score_mode,
                        ),
                    }
                )
            sims = sims_by_seed[0]
            data_sim = sims["sim_data"]
            # Id match and the duplicate-draw skip are pregame only.
            # ``played`` stays on the name join so it can match main.
            by_id = index_actual_ids(loaded.actual_rows) if population == "pregame" else {}
            joined_rows = []
            week_skips: list[dict] = []
            injury_rows = list(getattr(loaded, "injury_rows", None) or [])
            kickoff = getattr(loaded, "kickoff", None)
            kickoffs = dict(getattr(loaded, "kickoffs", None) or {})
            kickoff_note = str(getattr(loaded, "kickoff_note", "") or "")
            if population == "pregame":
                week_note = kickoff_note or (
                    "kickoff from caller" if (kickoff is not None or kickoffs) else "kickoff missing"
                )
                kickoff_weeks.append(
                    {
                        "season": int(season),
                        "week": int(week),
                        "note": week_note,
                        "teams": len(kickoffs),
                    }
                )
                if week_note == "kickoff missing":
                    print(
                        f"holdout kickoff missing {season} week {week}; "
                        "Out/IR rows with date_modified stay in",
                        file=sys.stderr,
                    )
            for pl in players:
                found = _resolve_actual(pl, indexed, by_id, dst_by_team)
                if found is None:
                    if population != "pregame" or not is_pregame_starter(pl):
                        continue
                    fate = _missing_starter_fate(
                        pl, injury_rows, _kickoff_for(pl, kickoffs, kickoff)
                    )
                    pos = _position(pl.position)
                    if fate == "exclude":
                        _bump_join(join_counts, "excluded_as_out", pos)
                        continue
                    if fate == "unresolved_kickoff":
                        _bump_join(join_counts, "out_unresolved_no_kickoff", pos)
                        actual, row, how = 0.0, {}, "unresolved"
                    elif fate == "zero_no_injury":
                        actual, row, how = 0.0, {}, "zero_no_injury"
                    else:
                        actual, row, how = 0.0, {}, "zero"
                else:
                    actual, row, how = found
                if population == "pregame" and not _draws_aligned(sims, pl.pid, draws_n):
                    week_skips.append(
                        {
                            "season": int(season),
                            "week": int(week),
                            "pid": pl.pid,
                            "name": pl.name,
                            "position": _position(pl.position),
                            "n_draws": int(draws_n),
                            "got": _draw_length(sims["sim_data"], pl.pid),
                        }
                    )
                    continue
                board = float(score_player(pl))
                means = {}
                pcts = {}
                covers = {}
                for mode, sim in sims.items():
                    stats = sim.by_pid.get(pl.pid)
                    if stats is None:
                        continue
                    means[mode] = float(stats.mean)
                    series = sim.draws.get(pl.pid) or ()
                    pcts[mode] = draw_percentile(series, actual)
                    covers[mode] = covers_p10_p90(series, actual)
                if "sim_data" not in means or "sim_placeholder" not in means:
                    continue
                if how == "id":
                    join_counts["id_matched"] += 1
                elif how == "name":
                    join_counts["name_matched"] += 1
                elif how == "zero":
                    _bump_join(join_counts, "kept_as_zero", _position(pl.position))
                elif how == "zero_no_injury":
                    _bump_join(join_counts, "kept_zero_no_injury_row", _position(pl.position))
                pos = _position(pl.position)
                starter = _in_population(pl, row, population)
                no_injury = how == "zero_no_injury"
                item = {
                    "position": pos,
                    "board": board,
                    "actual": actual,
                    "starter": starter,
                    "no_injury_row": no_injury,
                    "means": means,
                    "pcts": pcts,
                    "covers": covers,
                    "player": pl,
                }
                if starter:
                    headline["with"]["board"].append((board, actual))
                    headline["with"]["data"].append((means["sim_data"], actual))
                    headline["with"]["cover"].append(bool(covers["sim_data"]))
                    if not no_injury:
                        headline["without"]["board"].append((board, actual))
                        headline["without"]["data"].append((means["sim_data"], actual))
                        headline["without"]["cover"].append(bool(covers["sim_data"]))
                joined_rows.append(item)
                for pool, keep in (("full", True), ("starters", starter)):
                    if not keep:
                        continue
                    errors[pool]["board"][pos].append((board, actual))
                    for mode in ("sim_placeholder", "sim_data"):
                        errors[pool][mode][pos].append((means[mode], actual))
                        calibration[mode][pool].append(
                            (pos, pcts[mode], covers[mode])
                        )
            if week_skips:
                who = ", ".join(
                    f"{row['name']} ({row['position']} {row['pid']}, got {row['got']})"
                    for row in week_skips
                )
                print(
                    f"holdout skip draws {season} week {week}: "
                    f"{len(week_skips)} rows length != {draws_n}: {who}",
                    file=sys.stderr,
                )
                draw_skip_rows.extend(week_skips)
            by_pos: dict[str, dict[str, list[tuple[float, float]]]] = {
                mode: defaultdict(list)
                for mode in ("board", "sim_placeholder", "sim_data")
            }
            for item in joined_rows:
                pos = item["position"]
                by_pos["board"][pos].append((item["board"], item["actual"]))
                for mode in ("sim_placeholder", "sim_data"):
                    by_pos[mode][pos].append((item["means"][mode], item["actual"]))
            for mode in by_pos:
                for pos in _POS_ORDER:
                    rho = spearman(
                        [pair[0] for pair in by_pos[mode][pos]],
                        [pair[1] for pair in by_pos[mode][pos]],
                    )
                    if rho is not None:
                        rank_week[mode][pos].append(rho)
            teams = sorted({(pl.team or "").upper() for pl in players if pl.team})
            actual_by_pid = {
                item["player"].pid: item["actual"] for item in joined_rows
            }
            for team in teams:
                roles = _pair_roles(players, team)
                qb = _role(players, team, "QB", 1)
                opp = (qb.opponent or "").upper() if qb is not None else ""
                for name, pair in roles.items():
                    if pair is None:
                        continue
                    if name == "QB-oppQB" and (not opp or team >= opp):
                        continue
                    left, right = pair
                    left_actual = actual_by_pid.get(left.pid)
                    right_actual = actual_by_pid.get(right.pid)
                    if left_actual is not None and right_actual is not None:
                        actual_pairs[name].append((left_actual, right_actual))
                    for mode, sim in sims.items():
                        rho = _pair_correlation(sim.draws, left, right)
                        if rho is not None:
                            sim_pairs[mode][name].append(rho)
            allowed = index_points_allowed(
                loaded.actual_rows, season=season, week=week
            )
            week_games = _game_actuals(players, allowed)
            for game in week_games:
                actual_margins.append(game["margin"])
                actual_totals.append(game["total"])
                if game["closing_margin"] is not None:
                    residual_margins.append(game["margin"] - game["closing_margin"])
                if game["closing_total"] is not None:
                    residual_totals.append(game["total"] - game["closing_total"])
            actual_game_keys = {game["game"] for game in week_games}
            for mode, sim in sims.items():
                sds = _sim_game_sds(sim)
                for game, (margin_sd, total_sd) in sds.items():
                    if game not in actual_game_keys:
                        continue
                    if margin_sd is not None:
                        sim_margin_sd[mode].append(margin_sd)
                    if total_sd is not None:
                        sim_total_sd[mode].append(total_sd)
            for item in joined_rows:
                pl = item["player"]
                key = (int(week), (pl.team or "").upper(), match_key(pl.name))
                fields = prop_index.get(key)
                if not fields:
                    bare = (int(week), "", match_key(pl.name))
                    fields = prop_index.get(bare)
                if not fields:
                    continue
                projected = closing_prop_points(fields)
                if projected is None:
                    continue
                prop_joined.append(
                    (
                        item["position"],
                        projected,
                        item["means"]["sim_data"],
                        item["actual"],
                    )
                )
            scored.append(
                {
                    "season": int(season),
                    "week": int(week),
                    "n_players": len(players),
                    "n_joined": len(joined_rows),
                    "missing": list(loaded.missing),
                    "notes": list(loaded.notes),
                    "sim_efficiency": data_mode,
                }
            )
            if (
                sens_week is not None
                and int(week) == int(sens_week)
                and loaded.sim_inputs is not None
            ):
                sens_target = {"season": int(season), "week": int(week)}
                baseline = {
                    item["player"].pid: item["means"]["sim_data"]
                    for item in joined_rows
                }
                # Sensitivity is the change in every player's projection,
                # including players with no actual row.
                for pl in players:
                    stats = data_sim.by_pid.get(pl.pid)
                    if stats is not None:
                        baseline[pl.pid] = float(stats.mean)
                sens_payload = _sensitivity(
                    players,
                    loaded.sim_inputs,
                    baseline,
                    week=int(week),
                    n=draws_n,
                    seed=int(primary),
                    sim_mode=score_mode,
                )
            if want_metrics:
                for offset, seed_i in enumerate(seed_list):
                    _absorb_seed(
                        seed_bags[offset],
                        joined_rows,
                        players,
                        sims_by_seed[offset],
                        week_games,
                        rng=pit_rng[offset],
                        row_sink=row_sink if offset == 0 else None,
                        draw_sink=draw_sink,
                        season=int(season),
                        week=int(week),
                        seed_i=int(seed_i),
                        n_draws=draws_n,
                    )

    prop_by_pos: dict[str, list[tuple[float, float]]] = defaultdict(list)
    sim_on_props: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for pos, prop_pts, sim_pts, actual in prop_joined:
        prop_by_pos[pos].append((prop_pts, actual))
        sim_on_props[pos].append((sim_pts, actual))
    prop_rank = {}
    sim_prop_rank = {}
    for pos in _POS_ORDER:
        prop_rank[pos] = spearman(
            [pair[0] for pair in prop_by_pos[pos]],
            [pair[1] for pair in prop_by_pos[pos]],
        )
        sim_prop_rank[pos] = spearman(
            [pair[0] for pair in sim_on_props[pos]],
            [pair[1] for pair in sim_on_props[pos]],
        )

    report = {
        "seasons": [int(season) for season in seasons],
        "weeks": [int(week) for week in weeks],
        "n": draws_n,
        "seed": int(primary),
        "seeds": [int(item) for item in seed_list],
        **({"sim_mode": score_mode} if score_mode != "off" else {}),
        "population": population,
        "join": join_counts,
        "kickoff": {
            "weeks": kickoff_weeks,
            "missing": [
                {"season": row["season"], "week": row["week"]}
                for row in kickoff_weeks
                if row.get("note") == "kickoff missing"
            ],
        },
        "headline": {
            "with_kept_zero_no_injury_row": _headline_cut(headline["with"]),
            "without_kept_zero_no_injury_row": _headline_cut(headline["without"]),
        },
        "draw_skips": {
            "n": len(draw_skip_rows),
            "n_draws": int(draws_n),
            "rows": draw_skip_rows,
        },
        "seed_prior_season": bool(seed_prior_season),
        "prior_weeks_rule": "weeks 1..W-1 only; week 1 is empty unless --seed-prior-season",
        "scored": scored,
        "failed": failed,
        "errors": {
            pool: {
                mode: _error_block(errors[pool][mode])
                for mode in ("board", "sim_placeholder", "sim_data")
            }
            for pool in ("starters", "full")
        },
        "calibration": {
            mode: {
                pool: _calibration_block(calibration[mode][pool])
                for pool in ("starters", "full")
            }
            for mode in ("sim_placeholder", "sim_data")
        },
        "game_variance": {
            "n_games": len(actual_margins),
            "actual_margin_sd": sample_sd(actual_margins),
            "actual_total_sd": sample_sd(actual_totals),
            "closing_margin_residual_sd": sample_sd(residual_margins),
            "closing_total_residual_sd": sample_sd(residual_totals),
            "n_closing_margins": len(residual_margins),
            "n_closing_totals": len(residual_totals),
            "margin": "home_pts - away_pts; home points are the away defense points_allowed",
            "closing_margin": "-home_spread (negative spread means home is favored)",
            "residual": "actual minus closing",
            "sim_margin_sd": {
                mode: _mean(sim_margin_sd[mode]) for mode in sim_margin_sd
            },
            "sim_total_sd": {
                mode: _mean(sim_total_sd[mode]) for mode in sim_total_sd
            },
        },
        "correlations": {
            name: {
                "actual": pearson(
                    [pair[0] for pair in actual_pairs[name]],
                    [pair[1] for pair in actual_pairs[name]],
                ),
                "n_actual": len(actual_pairs[name]),
                "sim_data": _mean(sim_pairs["sim_data"][name]),
                "n_sim_data": len(sim_pairs["sim_data"][name]),
                "sim_placeholder": _mean(sim_pairs["sim_placeholder"][name]),
                "n_sim_placeholder": len(sim_pairs["sim_placeholder"][name]),
            }
            for name in _PAIRS
        },
        "ranking": {
            mode: {pos: _rank_average(rank_week[mode][pos]) for pos in _POS_ORDER}
            for mode in ("board", "sim_placeholder", "sim_data")
        },
        "sensitivity": {
            "season": None if sens_target is None else sens_target["season"],
            "week": None if sens_target is None else sens_target["week"],
            "note": (
                "each input moved ±10% on one week; mean change in the data-sim "
                "projection by position. Target and carry shares move the depth-1 "
                "player only, then the sim renormalizes the team."
                if sens_payload is not None
                else "sensitivity skipped (no scored week with sim inputs)"
            ),
            "inputs": sens_payload or {},
        },
        "props": {
            "available": bool(prop_joined),
            "notes": prop_notes,
            "n": len(prop_joined),
            "by_position": {
                pos: {
                    "props": _me_mae(prop_by_pos.get(pos) or []),
                    "sim_data": _me_mae(sim_on_props.get(pos) or []),
                    "props_spearman": prop_rank[pos],
                    "sim_data_spearman": sim_prop_rank[pos],
                }
                for pos in _POS_ORDER
            },
        },
    }
    if want_metrics:
        per_seed = [_finalize_seed(bag) for bag in seed_bags]
        draws_info = {
            "kind": draw_sink[0]["kind"] if draw_sink else "empty",
            "n_rows": len(draw_sink),
        }
        if draws_out is not None:
            archive = write_draw_archive(Path(draws_out), draw_sink)
            draws_info = {
                "kind": archive["kind"],
                "n_rows": archive["n_rows"],
                "sha256": archive["sha256"],
            }
            if "n_draws" in archive:
                draws_info["n_draws"] = archive["n_draws"]
            if "quantiles" in archive:
                draws_info["quantiles"] = archive["quantiles"]
        report["scores"] = _scores_report(row_sink)
        report["paired"] = _paired_report(row_sink)
        report["residual_correlations"] = (
            _residual_report(seed_bags[0]) if seed_bags else {}
        )
        report["mc_se"] = {
            "n_seeds": len(seed_list),
            "seeds": [int(item) for item in seed_list],
            "metrics": _mc_block(per_seed),
        }
        report["draws"] = draws_info
    report["population_note"] = (
        "pregame keeps depth-chart starters (QB/RB/TE rank 1, WR ranks 1-3, DEF) "
        "including zero-point busts. played is the legacy is_starter filter."
    )
    return _round_report(report)


def _round_report(value):
    if isinstance(value, float):
        return _round(value)
    if isinstance(value, dict):
        return {key: _round_report(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_report(item) for item in value]
    return value


def format_report(report: dict) -> str:
    lines = [
        "holdout seasons {seasons} weeks {weeks}  n {n}  seed {seed}".format(
            seasons=",".join(str(s) for s in report["seasons"]),
            weeks=_weeks_text(report["weeks"]),
            n=report["n"],
            seed=report["seed"],
        )
    ]
    if report.get("sim_mode") not in (None, "", "off"):
        lines.append("sim-mode: %s" % report["sim_mode"])
    if report.get("seed_prior_season"):
        lines.append("seed-prior-season: on")
    else:
        lines.append("seed-prior-season: off (week 1 has no season-to-date inputs)")
    if report["failed"]:
        lines.append(
            "failed: "
            + ", ".join(
                f"{row['season']}w{row['week']}" for row in report["failed"]
            )
        )
    for pool in ("starters", "full"):
        lines.append(f"pool: {pool}")
        lines.append(
            f"{'pos':<5} {'n':>4} {'board_me':>9} {'board_mae':>10} "
            f"{'ph_me':>8} {'ph_mae':>8} {'data_me':>8} {'data_mae':>8}"
        )
        block = report["errors"][pool]
        for pos in _POS_ORDER:
            board = block["board"][pos]
            placeholder = block["sim_placeholder"][pos]
            data = block["sim_data"][pos]
            lines.append(
                f"{pos:<5} {board['n']:4d} {_fmt(board['me'])} {_fmt(board['mae'], 10)} "
                f"{_fmt(placeholder['me'], 8)} {_fmt(placeholder['mae'], 8)} "
                f"{_fmt(data['me'], 8)} {_fmt(data['mae'], 8)}"
            )
    lines.append("calibration (share of actuals at or below that sim percentile)")
    for mode in ("sim_data", "sim_placeholder"):
        for pool in ("full", "starters"):
            block = report["calibration"][mode][pool]
            for pos in (*_POS_ORDER, "ALL"):
                row = block[pos]
                if row["n"] == 0:
                    continue
                below = row["below"]
                cuts = " ".join(
                    f"p{cut} {100.0 * below[str(cut)]:4.1f}%"
                    for cut in (10, 20, 30, 40, 50, 60, 70, 80, 90)
                )
                cover = row["p10_p90"]
                cover_s = "n/a" if cover is None else f"{100.0 * cover:4.1f}%"
                lines.append(
                    f"{mode} {pool} {pos:<3} n {row['n']:4d}  {cuts}  "
                    f"p10-p90 {cover_s} (ideal 80%)"
                )
    var = report["game_variance"]
    lines.append(
        "game variance  n {n}  actual margin sd {am}  actual total sd {at}  "
        "closing margin residual sd {cm}  closing total residual sd {ct}".format(
            n=var["n_games"],
            am=_fmt(var["actual_margin_sd"], 0).strip(),
            at=_fmt(var["actual_total_sd"], 0).strip(),
            cm=_fmt(var["closing_margin_residual_sd"], 0).strip(),
            ct=_fmt(var["closing_total_residual_sd"], 0).strip(),
        )
    )
    for mode in ("sim_data", "sim_placeholder"):
        lines.append(
            f"  {mode} mean margin sd {_fmt(var['sim_margin_sd'][mode], 0).strip()}  "
            f"mean total sd {_fmt(var['sim_total_sd'][mode], 0).strip()}"
        )
    lines.append("correlations (actual Pearson; sim is the mean within-game Pearson)")
    for name in _PAIRS:
        row = report["correlations"][name]
        lines.append(
            f"  {name:<10} actual {_fmt(row['actual'], 7)} n {row['n_actual']:4d}  "
            f"sim-data {_fmt(row['sim_data'], 7)} n {row['n_sim_data']:4d}  "
            f"sim-placeholder {_fmt(row['sim_placeholder'], 7)}"
        )
    lines.append("ranking (mean weekly Spearman of projection vs actual)")
    for mode in ("board", "sim_data", "sim_placeholder"):
        bits = " ".join(
            f"{pos} {_fmt(report['ranking'][mode][pos], 6)}" for pos in _POS_ORDER
        )
        lines.append(f"  {mode}: {bits}")
    sens = report["sensitivity"]
    lines.append(
        f"sensitivity season {sens['season']} week {sens['week']}  {sens['note']}"
    )
    for kind, directions in (sens.get("inputs") or {}).items():
        for direction in ("plus", "minus"):
            bits = " ".join(
                f"{pos} {_fmt((directions.get(direction) or {}).get(pos), 7)}"
                for pos in _POS_ORDER
            )
            sign = "+10%" if direction == "plus" else "-10%"
            lines.append(f"  {kind} {sign}: {bits}")
    props = report["props"]
    if props["available"]:
        lines.append(f"props_closing n {props['n']} (players with a closing line)")
        for pos in _POS_ORDER:
            row = props["by_position"][pos]
            if row["props"]["n"] == 0:
                continue
            lines.append(
                f"  {pos:<3} props mae {_fmt(row['props']['mae'], 7)} "
                f"spearman {_fmt(row['props_spearman'], 7)}  "
                f"sim-data mae {_fmt(row['sim_data']['mae'], 7)} "
                f"spearman {_fmt(row['sim_data_spearman'], 7)}  "
                f"n {row['props']['n']}"
            )
    else:
        note = "; ".join(props["notes"]) if props["notes"] else "no rows"
        lines.append(f"props_closing: skipped ({note})")
    lines.append(f"population: {report.get('population', 'pregame')}")
    join = report.get("join") or {}
    if join:
        zero_bits = " ".join(
            f"{pos} {(join.get('kept_as_zero_by_pos') or {}).get(pos, 0)}"
            for pos in _POS_ORDER
            if (join.get("kept_as_zero_by_pos") or {}).get(pos, 0)
        )
        out_bits = " ".join(
            f"{pos} {(join.get('excluded_as_out_by_pos') or {}).get(pos, 0)}"
            for pos in _POS_ORDER
            if (join.get("excluded_as_out_by_pos") or {}).get(pos, 0)
        )
        lines.append(
            "join  id-matched {id_n}  name-matched {name_n}  "
            "kept-as-zero {zero}  kept-zero-no-injury-row {bare}  "
            "excluded-as-Out {out}  out-unresolved-no-kickoff {open_kick}".format(
                id_n=join.get("id_matched", 0),
                name_n=join.get("name_matched", 0),
                zero=join.get("kept_as_zero", 0),
                bare=join.get("kept_zero_no_injury_row", 0),
                out=join.get("excluded_as_out", 0),
                open_kick=join.get("out_unresolved_no_kickoff", 0),
            )
        )
        if zero_bits:
            lines.append(f"  kept-as-zero by pos  {zero_bits}")
        bare_bits = " ".join(
            f"{pos} {(join.get('kept_zero_no_injury_row_by_pos') or {}).get(pos, 0)}"
            for pos in _POS_ORDER
            if (join.get("kept_zero_no_injury_row_by_pos") or {}).get(pos, 0)
        )
        if bare_bits:
            lines.append(f"  kept-zero-no-injury-row by pos  {bare_bits}")
        if out_bits:
            lines.append(f"  excluded-as-Out by pos  {out_bits}")
    kickoff = report.get("kickoff") or {}
    missing_kick = kickoff.get("missing") or []
    if missing_kick:
        bits = ", ".join(f"{row['season']}w{row['week']}" for row in missing_kick)
        lines.append(
            f"kickoff missing {bits}; Out/IR rows with date_modified stay in"
        )
    headline = report.get("headline") or {}
    if headline:
        for key, label in (
            ("with_kept_zero_no_injury_row", "starters"),
            ("without_kept_zero_no_injury_row", "starters without kept-zero-no-injury-row"),
        ):
            cut = headline.get(key) or {}
            if not cut:
                continue
            lines.append(
                f"headline {label}  n {cut.get('n', 0)}  "
                f"board MAE {_fmt(cut.get('board_mae'), 9, 4)}  "
                f"data MAE {_fmt(cut.get('data_mae'), 9, 4)}  "
                f"p10-p90 {_fmt(cut.get('p10_p90'), 9, 4)}"
            )
    skips = report.get("draw_skips") or {}
    if int(skips.get("n") or 0):
        who = ", ".join(
            f"{row.get('name')} ({row.get('position')} {row.get('pid')})"
            for row in (skips.get("rows") or [])
        )
        lines.append(
            f"draw skips {skips.get('n')} rows length != {skips.get('n_draws')}: {who}"
        )
    seeds = report.get("seeds") or [report.get("seed")]
    lines.append("seeds: " + ",".join(str(item) for item in seeds))
    scores = report.get("scores") or {}
    pools = scores.get("pools") or {}
    if pools:
        lines.append(
            "scores (CRPS and coverage use a week-block bootstrap; "
            "log score is higher-better)"
        )
        for pool in ("starters", "full"):
            for mode in _SIM_MODES:
                row = (pools.get(pool) or {}).get(mode) or {}
                if not row:
                    continue
                crps = row.get("crps") or {}
                cover = row.get("p10_p90") or {}
                cover25 = row.get("p25_p75") or {}
                hist = row.get("pit_histogram") or {}
                note = crps.get("note") or ""
                lines.append(
                    f"  {mode} {pool} crps {_fmt(crps.get('estimate'), 7)} "
                    f"p10-p90 {_fmt(cover.get('estimate'), 7)} "
                    f"p25-p75 {_fmt(cover25.get('estimate'), 7)} "
                    f"pit p {_fmt(hist.get('p'), 7)} "
                    f"blocks {crps.get('n_blocks', 0)} {note}".rstrip()
                )
    salary = scores.get("salary_thresholds") or {}
    if salary:
        lines.append(
            f"salary thresholds n {salary.get('n', 0)}  {salary.get('note', '')}".rstrip()
        )
    lineup = scores.get("lineup") or {}
    if lineup.get("note"):
        lines.append(f"lineup: {lineup['note']}")
    paired = (report.get("paired") or {}).get("starters") or {}
    if paired:
        lines.append("paired starters (loss a minus b, week-block 95% CI)")
        for key, entry in paired.items():
            for loss_name in ("abs_error", "crps"):
                row = entry.get(loss_name)
                if not row:
                    continue
                ci = row.get("ci95") or [None, None]
                flag = "excludes 0" if row.get("excludes_zero") else "includes 0"
                lines.append(
                    f"  {key} {loss_name} {_fmt(row.get('estimate'), 7)} "
                    f"CI {_fmt(ci[0], 9, 4)} {_fmt(ci[1], 9, 4)} {flag}"
                )
    residual = report.get("residual_correlations") or {}
    if residual:
        lines.append(
            "residual correlations (value minus draw mean; "
            "sim is the mean within-game Pearson; flag when Fisher z > 4)"
        )
        for name in _PAIRS:
            data = (residual.get(name) or {}).get("sim_data") or {}
            if not data:
                continue
            flag = " FLAG" if data.get("flag") else ""
            lines.append(
                f"  {name:<10} actual {_fmt(data.get('actual'), 7)} "
                f"sim {_fmt(data.get('sim'), 7)} "
                f"z {_fmt(data.get('z'), 7)} n {data.get('n_actual', 0)}{flag}"
            )
    mc = report.get("mc_se") or {}
    if int(mc.get("n_seeds") or 0) > 1:
        lines.append(
            f"monte carlo SE across {mc['n_seeds']} seeds "
            "(mean, se, Student-t 95% half-width)"
        )
        metrics = mc.get("metrics") or {}
        for key in (
            "starter_mae.board.ALL",
            "starter_mae.sim_placeholder.ALL",
            "starter_mae.sim_data.ALL",
            "coverage.sim_data.p10_p90",
            "coverage.sim_data.p25_p75",
            "game_total_sd.sim_data",
            "crps.sim_data",
        ):
            row = metrics.get(key)
            if not row:
                continue
            lines.append(
                f"  {key} mean {_fmt(row.get('mean'), 7)} "
                f"se {_fmt(row.get('se'), 9, 4)} "
                f"half {_fmt(row.get('half_width_95'), 9, 4)}"
            )
    return "\n".join(lines)


def _weeks_text(weeks: list[int]) -> str:
    if not weeks:
        return ""
    if weeks == list(range(weeks[0], weeks[-1] + 1)):
        if len(weeks) == 1:
            return str(weeks[0])
        return f"{weeks[0]}-{weeks[-1]}"
    return ",".join(str(week) for week in weeks)


def _empty_join() -> dict:
    return {
        "id_matched": 0,
        "name_matched": 0,
        "kept_as_zero": 0,
        "kept_zero_no_injury_row": 0,
        "excluded_as_out": 0,
        "out_unresolved_no_kickoff": 0,
        "kept_as_zero_by_pos": {pos: 0 for pos in _POS_ORDER},
        "kept_zero_no_injury_row_by_pos": {pos: 0 for pos in _POS_ORDER},
        "excluded_as_out_by_pos": {pos: 0 for pos in _POS_ORDER},
        "out_unresolved_no_kickoff_by_pos": {pos: 0 for pos in _POS_ORDER},
    }


def _empty_headline() -> dict:
    return {"board": [], "data": [], "cover": []}


def _headline_cut(bag: dict) -> dict:
    board = _me_mae(bag["board"])
    data = _me_mae(bag["data"])
    covers = bag["cover"]
    n = len(covers)
    return {
        "n": n,
        "board_mae": board["mae"],
        "data_mae": data["mae"],
        "p10_p90": (sum(1 for flag in covers if flag) / n) if n else None,
    }


def _bump_join(counts: dict, key: str, pos: str) -> None:
    counts[key] += 1
    by_pos = counts.get(f"{key}_by_pos") or {}
    if pos in by_pos:
        by_pos[pos] += 1


def _fmt(value, width: int = 9, digits: int = 2) -> str:
    if value is None:
        text = "n/a"
    else:
        text = f"{float(value):.{int(digits)}f}"
    return f"{text:>{width}}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument(
        "--seasons",
        default=None,
        help="comma list, e.g. 2024,2025",
    )
    ap.add_argument(
        "--weeks",
        default="1-18",
        help="1-18, a single week, or a comma list (default 1-18)",
    )
    ap.add_argument(
        "--n",
        type=int,
        default=3000,
        help="sim draws per week (default 3000)",
    )
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument(
        "--sim-mode",
        default="off",
        help="off (default) keeps the current draws. team draws a joint "
        "total and spread and scales production with the team score.",
    )
    ap.add_argument(
        "--seeds",
        default=None,
        help="comma-separated sim seeds, e.g. 1,2,3 (at most 5). "
        "Default is --seed. More than one seed requests scoring metrics "
        "and a Monte Carlo SE.",
    )
    ap.add_argument(
        "--metrics",
        action="store_true",
        help="PIT, CRPS, coverage, Brier/log score, paired tests, residual "
        "Fisher z, and Monte Carlo SE. Requires numpy and scipy "
        f"({_METRICS_INSTALL}). Also on when --seeds has more than one "
        "value or --draws-out is set.",
    )
    ap.add_argument(
        "--population",
        choices=("pregame", "played"),
        default="pregame",
        help="starter pool: pregame depth chart (default) or played "
        "(legacy positive counting stats)",
    )
    ap.add_argument("--json-out", default=None, help="write the report JSON here")
    ap.add_argument(
        "--draws-out",
        default=None,
        help="npz of per-player draws, or q01..q99 when n is large. "
        "Requests scoring metrics. Default when metrics are on and "
        "--json-out is set: next to that JSON.",
    )
    ap.add_argument(
        "--manifest-out",
        default=None,
        help="run manifest JSON (default: next to --json-out)",
    )
    ap.add_argument(
        "--seed-prior-season",
        action="store_true",
        help="week 1 sim inputs from the prior season's week 18 (default off)",
    )
    ap.add_argument(
        "--sensitivity-week",
        type=int,
        default=None,
        help="week to perturb ±10%% (default: last requested week other than week 1)",
    )
    args = ap.parse_args(argv)
    try:
        seasons = parse_seasons(args.seasons, args.season)
        weeks = parse_weeks(args.weeks)
        seeds = parse_seeds(args.seeds, args.seed)
        parse_sim_mode(args.sim_mode)
    except ValueError as exc:
        print(f"choke HOLDOUT: {exc}", file=sys.stderr)
        return 1
    if args.n < 1:
        print("choke HOLDOUT: n must be >= 1", file=sys.stderr)
        return 1
    json_path = Path(args.json_out) if args.json_out else None
    want_metrics = metrics_requested(
        metrics=args.metrics, seeds=seeds, draws_out=args.draws_out
    )
    if args.draws_out:
        draws_path = Path(args.draws_out)
    elif want_metrics and json_path is not None:
        draws_path = json_path.with_suffix(".draws.npz")
    else:
        draws_path = None
    if args.manifest_out:
        manifest_path = Path(args.manifest_out)
    elif json_path is not None:
        manifest_path = json_path.with_suffix(".manifest.json")
    else:
        manifest_path = None
    cli = list(sys.argv[1:] if argv is None else argv)
    try:
        with capture_pulls() as pulls:
            report = run_holdout(
                seasons,
                weeks,
                n=args.n,
                seed=seeds[0],
                seeds=seeds,
                population=args.population,
                seed_prior_season=args.seed_prior_season,
                sensitivity_week=args.sensitivity_week,
                draws_out=draws_path,
                metrics=want_metrics,
                sim_mode=args.sim_mode,
            )
    except HoldoutMetricsError as exc:
        print(f"choke HOLDOUT_METRICS: {exc}", file=sys.stderr)
        return 1
    text = format_report(report)
    print(text)
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"json: {json_path}", file=sys.stderr)
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = build_manifest(
            argv=cli,
            report=report,
            pulls=pulls,
            draws={
                "path": None if draws_path is None else str(draws_path),
                "sha256": (report.get("draws") or {}).get("sha256"),
                "kind": (report.get("draws") or {}).get("kind"),
                "n_rows": (report.get("draws") or {}).get("n_rows"),
                "quantiles": (report.get("draws") or {}).get("quantiles"),
            },
        )
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        print(f"manifest: {manifest_path}", file=sys.stderr)
    if not report["scored"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
