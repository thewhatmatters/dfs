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

The report pools starters and the full pool: mean error and MAE by position
for the board, the placeholder sim, and the data sim. It also reports sim
calibration, game margin and total variance, role correlations, Spearman
rank accuracy, a one-week ±10% sensitivity, and a props_closing baseline
when that dataset has rows. ``props_closing`` starts at 2026 week 3; earlier
seasons skip that section.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from nfl.backtest import (
    DepthPoolError,
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
)
from nfl.gangstash_data import fetch_props_closing
from nfl.lines import LinesError
from nfl.names import match_key
from nfl.players import Player
from nfl.projections import score_player
from nfl.props import norm_prop, prop_field
from nfl.rules import FANDUEL_NFL
from nfl.sim import GameSim, simulate_games
from nfl.sim_efficiency import DataEfficiency, build_efficiency, resolve_run_efficiency
from nfl.sim_feed import resolve_sim_inputs
from nfl.sim_inputs import CarryWeek, SimInputError, SimInputs, TargetWeek, TeamStat
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
    return WeekLoad(
        players=players,
        actual_rows=list(actual_rows or []),
        sim_inputs=sim_inputs,
        missing=list(missing),
        notes=notes,
    )


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
    key = ((player.team or "").upper(), match_key(player.name))
    info = indexed.get(key)
    if info is None and _position(player.position) == "DEF":
        info = dst_by_team.get((player.team or "").upper())
    if info is None:
        return None
    return float(info["fd_points"]), info["row"]


def run_holdout(
    seasons: list[int],
    weeks: list[int],
    *,
    n: int = 3000,
    seed: int = 1,
    seed_prior_season: bool = False,
    sensitivity_week: int | None = None,
    run_sensitivity: bool = True,
    load=None,
    prop_fetch=None,
) -> dict:
    """Score every requested week. Network stays inside ``load`` and props."""
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
            placeholder = simulate_games(
                players,
                n=draws_n,
                seed=int(seed),
                inputs=loaded.sim_inputs,
                efficiency=build_efficiency("placeholder", loaded.sim_inputs),
            )
            data_model, data_mode, data_note = resolve_run_efficiency(
                "data",
                loaded.sim_inputs,
                before_week=week,
            )
            data_sim = simulate_games(
                players,
                n=draws_n,
                seed=int(seed),
                inputs=loaded.sim_inputs,
                efficiency=data_model,
            )
            if data_note:
                print(data_note, file=sys.stderr)
            sims = {
                "sim_placeholder": placeholder,
                "sim_data": data_sim,
            }
            joined_rows = []
            for pl in players:
                found = _actual_fd(pl, indexed, dst_by_team)
                if found is None:
                    continue
                actual, row = found
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
                pos = _position(pl.position)
                starter = is_starter(pl, row)
                item = {
                    "position": pos,
                    "board": board,
                    "actual": actual,
                    "starter": starter,
                    "means": means,
                    "pcts": pcts,
                    "covers": covers,
                    "player": pl,
                }
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
                    seed=int(seed),
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
        "seed": int(seed),
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
    return "\n".join(lines)


def _weeks_text(weeks: list[int]) -> str:
    if not weeks:
        return ""
    if weeks == list(range(weeks[0], weeks[-1] + 1)):
        if len(weeks) == 1:
            return str(weeks[0])
        return f"{weeks[0]}-{weeks[-1]}"
    return ",".join(str(week) for week in weeks)


def _fmt(value, width: int = 9) -> str:
    if value is None:
        text = "n/a"
    else:
        text = f"{float(value):.2f}"
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
    ap.add_argument("--json-out", default=None, help="write the report JSON here")
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
    except ValueError as exc:
        print(f"choke HOLDOUT: {exc}", file=sys.stderr)
        return 1
    if args.n < 1:
        print("choke HOLDOUT: n must be >= 1", file=sys.stderr)
        return 1
    report = run_holdout(
        seasons,
        weeks,
        n=args.n,
        seed=args.seed,
        seed_prior_season=args.seed_prior_season,
        sensitivity_week=args.sensitivity_week,
    )
    text = format_report(report)
    print(text)
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"json: {path}", file=sys.stderr)
    if not report["scored"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
