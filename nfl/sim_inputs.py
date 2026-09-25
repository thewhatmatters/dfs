"""Inputs for the layered NFL Monte Carlo.

The sim does not fetch these. Gangstash `/data` readers live in the
sources client (open PR adding `nfl/gangstash_data.py`); this module is
only the shape `simulate_games` consumes. Tests load JSON fixtures.
No network.

Rates are fractions in ``[0, 1]`` (``0.58`` = 58% pass). ``from_row``
accepts a percent (``> 1.5``) and divides by 100.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class SimInputError(ValueError):
    """Local sim-input JSON is missing or not the expected shape."""


def pooled_epa_var(
    sum_: float | None,
    sq_sum: float | None,
    n: int | None,
) -> float | None:
    """Unbiased per-play variance ``(sq_sum - sum^2/n) / (n-1)``.

    Returns None when the count is under 2 or the sums are missing.
    A tiny negative result from float noise is treated as 0.
    """
    if sum_ is None or sq_sum is None or n is None:
        return None
    count = int(n)
    if count < 2:
        return None
    var = (float(sq_sum) - (float(sum_) ** 2) / count) / (count - 1)
    if var < 0:
        if var > -1e-6:
            return 0.0
        return None
    return var


def _unit_rate(val: float | None) -> float | None:
    if val is None:
        return None
    if val > 1.5:
        return val / 100.0
    return val


def _pick(row: dict, *keys: str):
    for key in keys:
        if key not in row:
            continue
        val = row[key]
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        return val
    return None


def _str(val) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _int(val) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _float(val) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class TeamStat:
    """One team-side row (offense or defense).

    Offense rows feed pass rate and that team's scoring dispersion.
    Defense rows feed the opponent's scoring dispersion only.
    """

    team_fd: str
    side: str = "offense"
    pass_rate: float | None = None
    neutral_pass_rate: float | None = None
    proe: float | None = None
    epa_per_play: float | None = None
    epa_var: float | None = None
    n: int | None = None
    epa_sum: float | None = None
    epa_sq_sum: float | None = None
    pass_n: int | None = None
    rush_n: int | None = None
    pass_epa_sum: float | None = None
    pass_epa_sq_sum: float | None = None
    rush_epa_sum: float | None = None
    rush_epa_sq_sum: float | None = None
    pass_epa_var: float | None = None
    rush_epa_var: float | None = None

    @property
    def is_offense(self) -> bool:
        return (self.side or "").strip().lower() in {"", "offense", "off", "o"}

    @property
    def is_defense(self) -> bool:
        return (self.side or "").strip().lower() in {"defense", "def", "d"}

    def epa_variance(self) -> float | None:
        """Per-play EPA variance. Sums win over a precomputed ``epa_var``.

        When the overall sums are missing, fall back to ``epa_var``, then
        to a play-weighted blend of pass and rush variances.
        """
        overall = pooled_epa_var(self.epa_sum, self.epa_sq_sum, self.n)
        if overall is not None:
            return overall
        if self.epa_var is not None and self.epa_var >= 0:
            return float(self.epa_var)
        weighted: list[tuple[int, float]] = []
        for count, sum_, sq, fallback in (
            (self.pass_n, self.pass_epa_sum, self.pass_epa_sq_sum, self.pass_epa_var),
            (self.rush_n, self.rush_epa_sum, self.rush_epa_sq_sum, self.rush_epa_var),
        ):
            var = pooled_epa_var(sum_, sq, count)
            if var is None and fallback is not None and fallback >= 0:
                var = float(fallback)
            if var is None or not count or int(count) <= 0:
                continue
            weighted.append((int(count), var))
        if not weighted:
            return None
        denom = sum(n for n, _v in weighted)
        return sum(n * v for n, v in weighted) / denom


@dataclass(frozen=True)
class TargetWeek:
    """One player-week of targets. Shares are fractions of team targets."""

    season: int
    week: int
    position: str
    player_name: str
    team_fd: str
    targets: float
    target_share: float | None = None
    team_targets: float | None = None
    team_pass_attempts: float | None = None
    gsis_id: str | None = None
    player_id: str | None = None


@dataclass(frozen=True)
class SnapWeek:
    """One player-week of offensive snap share.

    ``offense_pct`` is a fraction in ``[0, 1]``. Absence is legal — RB
    rush share then uses the depth role weight.
    """

    season: int
    week: int
    position: str
    player_name: str
    team_fd: str
    offense_pct: float | None = None
    gsis_id: str | None = None
    player_id: str | None = None


@dataclass(frozen=True)
class SimInputs:
    """Bundle passed into ``simulate_games``. Empty means today's fallback."""

    team_stats: tuple[TeamStat, ...] = ()
    targets: tuple[TargetWeek, ...] = ()
    snaps: tuple[SnapWeek, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.team_stats and not self.targets and not self.snaps


def team_stat_from_row(row: dict) -> TeamStat | None:
    """Map a gangstash-shaped team_stats row. None if ``team_fd`` is absent."""
    if not isinstance(row, dict):
        return None
    team = _str(_pick(row, "team_fd", "team")).upper()
    if not team:
        return None
    side = _str(_pick(row, "side")) or "offense"
    return TeamStat(
        team_fd=team,
        side=side,
        pass_rate=_unit_rate(_float(_pick(row, "pass_rate"))),
        neutral_pass_rate=_unit_rate(_float(_pick(row, "neutral_pass_rate"))),
        proe=_unit_rate(_float(_pick(row, "proe"))),
        epa_per_play=_float(_pick(row, "epa_per_play")),
        epa_var=_float(_pick(row, "epa_var")),
        n=_int(_pick(row, "n")),
        epa_sum=_float(_pick(row, "epa_sum")),
        epa_sq_sum=_float(_pick(row, "epa_sq_sum")),
        pass_n=_int(_pick(row, "pass_n", "n_pass")),
        rush_n=_int(_pick(row, "rush_n", "n_rush")),
        pass_epa_sum=_float(_pick(row, "pass_epa_sum", "epa_sum_pass")),
        pass_epa_sq_sum=_float(_pick(row, "pass_epa_sq_sum", "epa_sq_sum_pass")),
        rush_epa_sum=_float(_pick(row, "rush_epa_sum", "epa_sum_rush")),
        rush_epa_sq_sum=_float(_pick(row, "rush_epa_sq_sum", "epa_sq_sum_rush")),
        pass_epa_var=_float(_pick(row, "pass_epa_var")),
        rush_epa_var=_float(_pick(row, "rush_epa_var")),
    )


def target_week_from_row(row: dict) -> TargetWeek | None:
    if not isinstance(row, dict):
        return None
    name = _str(_pick(row, "player_name", "name", "player"))
    team = _str(_pick(row, "team_fd", "team")).upper()
    if not name or not team:
        return None
    week = _int(_pick(row, "week"))
    season = _int(_pick(row, "season")) or 0
    targets = _float(_pick(row, "targets"))
    if week is None or targets is None:
        return None
    pos = _str(_pick(row, "position", "pos")).upper() or "WR"
    return TargetWeek(
        season=season,
        week=week,
        position=pos,
        player_name=name,
        team_fd=team,
        targets=targets,
        target_share=_unit_rate(_float(_pick(row, "target_share"))),
        team_targets=_float(_pick(row, "team_targets")),
        team_pass_attempts=_float(_pick(row, "team_pass_attempts")),
        gsis_id=_str(_pick(row, "gsis_id")) or None,
        player_id=_str(_pick(row, "player_id")) or None,
    )


def snap_week_from_row(row: dict) -> SnapWeek | None:
    if not isinstance(row, dict):
        return None
    name = _str(_pick(row, "player_name", "name", "player"))
    team = _str(_pick(row, "team_fd", "team")).upper()
    if not name or not team:
        return None
    week = _int(_pick(row, "week"))
    if week is None:
        return None
    pos = _str(_pick(row, "position", "pos")).upper() or "RB"
    return SnapWeek(
        season=_int(_pick(row, "season")) or 0,
        week=week,
        position=pos,
        player_name=name,
        team_fd=team,
        offense_pct=_unit_rate(_float(_pick(row, "offense_pct", "snap_share"))),
        gsis_id=_str(_pick(row, "gsis_id")) or None,
        player_id=_str(_pick(row, "player_id")) or None,
    )


def sim_inputs_from_records(
    team_stats: list[dict] | None = None,
    targets: list[dict] | None = None,
    snaps: list[dict] | None = None,
) -> SimInputs:
    """Build inputs from gangstash-shaped row dicts. Skips blank rows."""
    stats = tuple(
        row
        for row in (team_stat_from_row(r) for r in (team_stats or []))
        if row is not None
    )
    weeks = tuple(
        row
        for row in (target_week_from_row(r) for r in (targets or []))
        if row is not None
    )
    snap_rows = tuple(
        row
        for row in (snap_week_from_row(r) for r in (snaps or []))
        if row is not None
    )
    return SimInputs(team_stats=stats, targets=weeks, snaps=snap_rows)


def load_sim_inputs(path: str | Path) -> SimInputs:
    """Read a fixture or cache dump. ``{team_stats, targets, snaps}`` lists."""
    file = Path(path)
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SimInputError(f"sim inputs {file}: {e}") from e
    if not isinstance(payload, dict):
        raise SimInputError(f"sim inputs {file} is not an object")
    for key in ("team_stats", "targets", "snaps"):
        if key in payload and not isinstance(payload[key], list):
            raise SimInputError(f"sim inputs {file} field {key} is not a list")
    return sim_inputs_from_records(
        payload.get("team_stats") or [],
        payload.get("targets") or [],
        payload.get("snaps") or [],
    )
