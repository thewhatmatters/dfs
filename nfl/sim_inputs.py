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

from nfl.names import match_key


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
    week: int | None = None
    pass_epa_per_play: float | None = None
    rush_epa_per_play: float | None = None
    pass_success_rate: float | None = None
    rush_success_rate: float | None = None
    success_rate: float | None = None
    explosive_rate: float | None = None
    red_zone_td_rate: float | None = None
    third_down_rate: float | None = None
    early_down_pass_epa_per_play: float | None = None
    early_down_pass_n: int | None = None
    early_down_pass_success_rate: float | None = None
    early_down_rush_epa_per_play: float | None = None
    early_down_rush_n: int | None = None
    early_down_rush_success_rate: float | None = None
    # Optional. None leaves the sim on the columns above.
    # Seconds per play are play_seconds / timed_plays. Neutral is the
    # no-script pair. Both move team play volume when present.
    plays_per_game: float | None = None
    seconds_per_play: float | None = None
    neutral_plays_per_game: float | None = None
    neutral_seconds_per_play: float | None = None
    yards_per_carry_allowed: float | None = None
    yards_per_dropback_allowed: float | None = None
    yards_per_attempt_allowed: float | None = None
    sack_rate: float | None = None
    air_yards_per_attempt_allowed: float | None = None
    pace_games: float | None = None
    pace_plays: float | None = None
    play_seconds: float | None = None
    pace_neutral_plays: float | None = None
    neutral_play_seconds: float | None = None
    timed_plays: float | None = None
    neutral_timed_plays: float | None = None
    rush_yards: float | None = None
    net_pass_yards: float | None = None
    air_yards: float | None = None
    yards_per_carry: float | None = None
    yards_per_dropback: float | None = None
    yards_per_pass_attempt: float | None = None
    air_yards_per_attempt: float | None = None

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
    carries: float | None = None
    team_carries: float | None = None
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
class CarryWeek:
    """One player-week of rush attempts. Used only to split the team rush pie."""

    season: int
    week: int
    player_name: str
    team_fd: str
    carries: float
    position: str = "RB"
    gsis_id: str | None = None
    player_id: str | None = None


@dataclass(frozen=True)
class PlayerWeek:
    """One player-week of counting stats for the efficiency layer.

    Optional columns stay ``None`` when the row omits them. A missing
    column is not a zero. ``rz_receiving_tds`` and ``rz_rushing_tds`` are
    stored and do not set the TD rate.
    """

    season: int
    week: int
    position: str
    player_name: str
    team_fd: str
    opponent: str | None = None
    gsis_id: str | None = None
    player_id: str | None = None
    targets: float = 0.0
    receptions: float = 0.0
    receiving_yards: float = 0.0
    receiving_tds: float = 0.0
    carries: float = 0.0
    rushing_yards: float = 0.0
    rushing_tds: float = 0.0
    pass_attempts: float = 0.0
    completions: float = 0.0
    passing_yards: float = 0.0
    passing_tds: float = 0.0
    interceptions: float = 0.0
    receiving_air_yards: float | None = None
    target_share: float | None = None
    air_yards_share: float | None = None
    wopr: float | None = None
    red_zone_targets: float | None = None
    red_zone_carries: float | None = None
    goal_line_carries: float | None = None
    rz_receiving_tds: float | None = None
    rz_rushing_tds: float | None = None


@dataclass(frozen=True)
class SimInputs:
    """Bundle passed into ``simulate_games``. Empty means today's fallback."""

    team_stats: tuple[TeamStat, ...] = ()
    targets: tuple[TargetWeek, ...] = ()
    snaps: tuple[SnapWeek, ...] = ()
    carries: tuple[CarryWeek, ...] = ()
    player_weeks: tuple[PlayerWeek, ...] = ()
    team_weeks: tuple[TeamStat, ...] = ()

    @property
    def empty(self) -> bool:
        return (
            not self.team_stats
            and not self.targets
            and not self.snaps
            and not self.player_weeks
            and not self.team_weeks
        )


def _same_rate(row: dict, *keys: str) -> float | None:
    """First present rate. Defense rows are what was allowed."""
    return _float(_pick(row, *keys))


def _seconds_rate(row: dict, rate_key: str, num_key: str, den_key: str) -> float | None:
    """Named rate, or numerator / denominator when the rate column is absent."""
    direct = _float(_pick(row, rate_key))
    if direct is not None:
        return direct
    num = _float(_pick(row, num_key))
    den = _float(_pick(row, den_key))
    if num is None or den is None or den <= 0:
        return None
    return num / den


def _plays_per_game(row: dict) -> float | None:
    direct = _float(_pick(row, "plays_per_game"))
    if direct is not None:
        return direct
    plays = _float(_pick(row, "pace_plays"))
    games = _float(_pick(row, "pace_games"))
    if plays is None or games is None or games <= 0:
        return None
    return plays / games


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
        week=_int(_pick(row, "week")),
        pass_epa_per_play=_float(_pick(row, "pass_epa_per_play")),
        rush_epa_per_play=_float(_pick(row, "rush_epa_per_play")),
        pass_success_rate=_unit_rate(_float(_pick(row, "pass_success_rate"))),
        rush_success_rate=_unit_rate(_float(_pick(row, "rush_success_rate"))),
        success_rate=_unit_rate(_float(_pick(row, "success_rate"))),
        explosive_rate=_unit_rate(_float(_pick(row, "explosive_rate"))),
        red_zone_td_rate=_unit_rate(_float(_pick(row, "red_zone_td_rate"))),
        third_down_rate=_unit_rate(_float(_pick(row, "third_down_rate"))),
        early_down_pass_epa_per_play=_float(
            _pick(row, "early_down_pass_epa_per_play")
        ),
        early_down_pass_n=_int(_pick(row, "early_down_pass_n")),
        early_down_pass_success_rate=_unit_rate(
            _float(_pick(row, "early_down_pass_success_rate"))
        ),
        early_down_rush_epa_per_play=_float(
            _pick(row, "early_down_rush_epa_per_play")
        ),
        early_down_rush_n=_int(_pick(row, "early_down_rush_n")),
        early_down_rush_success_rate=_unit_rate(
            _float(_pick(row, "early_down_rush_success_rate"))
        ),
        plays_per_game=_plays_per_game(row),
        seconds_per_play=_seconds_rate(
            row, "seconds_per_play", "play_seconds", "timed_plays"
        ),
        neutral_plays_per_game=_float(_pick(row, "neutral_plays_per_game")),
        neutral_seconds_per_play=_seconds_rate(
            row, "neutral_seconds_per_play", "neutral_play_seconds", "neutral_timed_plays"
        ),
        yards_per_carry_allowed=_same_rate(row, "yards_per_carry", "yards_per_carry_allowed"),
        yards_per_dropback_allowed=_same_rate(
            row, "yards_per_dropback", "yards_per_dropback_allowed"
        ),
        yards_per_attempt_allowed=_same_rate(
            row,
            "yards_per_pass_attempt",
            "yards_per_attempt_allowed",
            "yards_per_attempt",
        ),
        sack_rate=_unit_rate(_float(_pick(row, "sack_rate"))),
        air_yards_per_attempt_allowed=_same_rate(
            row, "air_yards_per_attempt", "air_yards_per_attempt_allowed"
        ),
        pace_games=_float(_pick(row, "pace_games")),
        pace_plays=_float(_pick(row, "pace_plays")),
        play_seconds=_float(_pick(row, "play_seconds")),
        pace_neutral_plays=_float(_pick(row, "pace_neutral_plays")),
        neutral_play_seconds=_float(_pick(row, "neutral_play_seconds")),
        timed_plays=_float(_pick(row, "timed_plays")),
        neutral_timed_plays=_float(_pick(row, "neutral_timed_plays")),
        rush_yards=_float(_pick(row, "rush_yards")),
        net_pass_yards=_float(_pick(row, "net_pass_yards")),
        air_yards=_float(_pick(row, "air_yards")),
        yards_per_carry=_same_rate(row, "yards_per_carry", "yards_per_carry_allowed"),
        yards_per_dropback=_same_rate(
            row, "yards_per_dropback", "yards_per_dropback_allowed"
        ),
        yards_per_pass_attempt=_same_rate(
            row,
            "yards_per_pass_attempt",
            "yards_per_attempt_allowed",
            "yards_per_attempt",
        ),
        air_yards_per_attempt=_same_rate(
            row, "air_yards_per_attempt", "air_yards_per_attempt_allowed"
        ),
    )


def target_week_from_row(row: dict) -> TargetWeek | None:
    if not isinstance(row, dict):
        return None
    name = _str(_pick(row, "player_name", "name", "player"))
    team = _str(_pick(row, "team_fd", "team")).upper()
    gsis_id = _str(_pick(row, "gsis_id")) or None
    player_id = _str(_pick(row, "player_id")) or None
    if (not name and not gsis_id and not player_id) or not team:
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
        carries=_float(_pick(row, "carries", "rushing_attempts", "rush_attempts", "rush_att")),
        team_carries=_float(_pick(row, "team_carries", "team_rush_attempts")),
        gsis_id=gsis_id,
        player_id=player_id,
    )


def carry_week_from_row(row: dict) -> CarryWeek | None:
    """Map a player-week that has a carry count. None when carries are absent."""
    if not isinstance(row, dict):
        return None
    name = _str(_pick(row, "player_name", "name", "player"))
    team = _str(_pick(row, "team_fd", "team", "recent_team")).upper()
    if not name or not team:
        return None
    week = _int(_pick(row, "week"))
    carries = _float(_pick(row, "carries", "rushing_attempts", "rush_attempts", "rush_att"))
    if week is None or carries is None:
        return None
    pos = _str(_pick(row, "position", "pos")).upper() or "RB"
    return CarryWeek(
        season=_int(_pick(row, "season")) or 0,
        week=week,
        player_name=name,
        team_fd=team,
        carries=carries,
        position=pos,
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


def player_week_from_row(row: dict) -> PlayerWeek | None:
    """Map a player-week. None when team or week is blank, or name and ids are."""
    if not isinstance(row, dict):
        return None
    name = _str(_pick(row, "player_name", "name", "player"))
    team = _str(_pick(row, "team_fd", "team", "recent_team")).upper()
    week = _int(_pick(row, "week"))
    gsis_id = _str(_pick(row, "gsis_id")) or None
    player_id = _str(_pick(row, "player_id")) or None
    if (not name and not gsis_id and not player_id) or not team or week is None:
        return None
    pos = _str(_pick(row, "position", "pos")).upper() or "WR"
    opponent = _str(_pick(row, "opponent", "opponent_fd", "opp")).upper() or None

    def _count(*keys: str) -> float:
        val = _float(_pick(row, *keys))
        return 0.0 if val is None else val

    return PlayerWeek(
        season=_int(_pick(row, "season")) or 0,
        week=week,
        position=pos,
        player_name=name,
        team_fd=team,
        opponent=opponent,
        gsis_id=gsis_id,
        player_id=player_id,
        targets=_count("targets"),
        receptions=_count("receptions"),
        receiving_yards=_count("receiving_yards", "rec_yards"),
        receiving_tds=_count("receiving_tds", "rec_tds"),
        carries=_count("carries", "rushing_attempts", "rush_attempts"),
        rushing_yards=_count("rushing_yards", "rush_yards"),
        rushing_tds=_count("rushing_tds", "rush_tds"),
        pass_attempts=_count("pass_attempts", "attempts"),
        completions=_count("completions"),
        passing_yards=_count("passing_yards", "pass_yards"),
        passing_tds=_count("passing_tds", "pass_tds"),
        interceptions=_count("interceptions", "ints"),
        receiving_air_yards=_float(_pick(row, "receiving_air_yards")),
        target_share=_unit_rate(_float(_pick(row, "target_share"))),
        air_yards_share=_unit_rate(_float(_pick(row, "air_yards_share"))),
        wopr=_float(_pick(row, "wopr")),
        red_zone_targets=_float(_pick(row, "red_zone_targets", "rz_targets")),
        red_zone_carries=_float(_pick(row, "red_zone_carries", "rz_carries")),
        goal_line_carries=_float(_pick(row, "goal_line_carries", "gl_carries")),
        rz_receiving_tds=_float(_pick(row, "rz_receiving_tds")),
        rz_rushing_tds=_float(_pick(row, "rz_rushing_tds")),
    )


def sim_inputs_from_records(
    team_stats: list[dict] | None = None,
    targets: list[dict] | None = None,
    snaps: list[dict] | None = None,
    carries: list[dict] | None = None,
    player_weeks: list[dict] | None = None,
    team_weeks: list[dict] | None = None,
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
    carry_rows = tuple(
        row
        for row in (carry_week_from_row(r) for r in (carries or []))
        if row is not None
    )
    # Target rows may also carry a rush count. Keep those when the
    # dedicated list did not already name that player-week.
    seen = {(row.team_fd, match_key(row.player_name), row.week) for row in carry_rows}
    extra: list[CarryWeek] = []
    for week in weeks:
        if week.carries is None:
            continue
        key = (week.team_fd, match_key(week.player_name), week.week)
        if key in seen:
            continue
        seen.add(key)
        extra.append(
            CarryWeek(
                season=week.season,
                week=week.week,
                player_name=week.player_name,
                team_fd=week.team_fd,
                carries=float(week.carries),
                position=week.position,
                gsis_id=week.gsis_id,
                player_id=week.player_id,
            )
        )
    players = tuple(
        row
        for row in (player_week_from_row(r) for r in (player_weeks or []))
        if row is not None
    )
    weekly = tuple(
        row
        for row in (team_stat_from_row(r) for r in (team_weeks or []))
        if row is not None
    )
    return SimInputs(
        team_stats=stats,
        targets=weeks,
        snaps=snap_rows,
        carries=carry_rows + tuple(extra),
        player_weeks=players,
        team_weeks=weekly,
    )


def load_sim_inputs(path: str | Path) -> SimInputs:
    """Read a fixture or cache dump. ``{team_stats, targets, snaps}`` lists."""
    file = Path(path)
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SimInputError(f"sim inputs {file}: {e}") from e
    if not isinstance(payload, dict):
        raise SimInputError(f"sim inputs {file} is not an object")
    for key in ("team_stats", "targets", "snaps", "carries", "player_weeks", "team_weeks"):
        if key in payload and not isinstance(payload[key], list):
            raise SimInputError(f"sim inputs {file} field {key} is not a list")
    return sim_inputs_from_records(
        payload.get("team_stats") or [],
        payload.get("targets") or [],
        payload.get("snaps") or [],
        payload.get("carries") or [],
        payload.get("player_weeks") or [],
        payload.get("team_weeks") or [],
    )
