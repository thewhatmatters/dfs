"""Build ``SimInputs`` from the gangstash dataset readers.

``--sim-inputs PATH`` skips this module. With no key and no cache the sim
keeps deterministic role shares and prints one stderr line.

Week-1 ``week1_score`` does not read these rows. Fetches go through
``nfl.gangstash_data`` (same-day cache, stale fallback). This module does
not scrape.
"""

from __future__ import annotations

from dataclasses import replace

from nfl.gangstash import GangstashDataError, GangstashDataKeyMissing, GangstashTruncated
from nfl.gangstash_data import (
    fetch_player_stats_weekly,
    fetch_player_usage,
    fetch_snaps,
    fetch_targets,
    fetch_team_stats,
    fetch_team_stats_weekly,
    parse_player_stat_row,
    parse_snap_row,
    parse_target_row,
)
from nfl.injuries import POOL_OUT_CODES, injury_code
from nfl.players import Player
from nfl.sim_inputs import (
    SimInputs,
    TeamStat,
    load_sim_inputs,
    sim_inputs_from_records,
    team_stat_from_row,
)
from nfl.teams import UnmappedTeam, require_fd

UNAVAILABLE_NOTE = (
    "sim inputs: gangstash unavailable — role shares deterministic"
)

_RATE_FIELDS = ("pass_rate", "neutral_pass_rate", "proe")
# WOPR = 1.5 * target_share + 0.7 * air_yards_share. Divide to recover a share.
WOPR_SHARE_DIVISOR = 2.2


def load_week_injuries(
    *,
    season: int,
    week: int,
    refresh: bool = False,
):
    """Current-week ``dataset=injuries`` rows and cache meta.

    Point-in-time ``injury_snapshots`` (an ``as_of`` read that skips
    ``is_baseline``) is not on this revision. The nightly path uses the
    week feed. Rows stay raw.
    """
    from nfl.gangstash_data import fetch_week_injuries

    return fetch_week_injuries(season=season, week=week, refresh=refresh)


def sim_pool(players: list[Player]) -> list[Player]:
    """Players the nightly sim scores.

    Out, IR, NA, and suspended players are omitted. Doubtful stays at
    full value: the sim treats ``D`` as inactive, so the copy passed to
    ``simulate_games`` has a blank injury. The published row still
    carries ``D``. Questionable is unchanged.
    """
    kept: list[Player] = []
    for pl in players:
        code = injury_code(pl.injury)
        if code in POOL_OUT_CODES:
            continue
        if code == "D":
            kept.append(replace(pl, injury=""))
            continue
        kept.append(pl)
    return kept


def resolve_sim_inputs(
    *,
    path: str | None,
    season: int,
    weeks: list[int] | None,
    refresh_targets: bool = False,
    refresh_snaps: bool = False,
    team_stats_scope: str = "season",
) -> tuple[SimInputs | None, str]:
    """File override, else the gangstash feed.

    A bad ``path`` raises ``SimInputError``. An unavailable feed returns
    ``(None, note)`` and does not raise. ``team_stats_scope="weekly"`` is
    the backtest path: team inputs are pooled ``team_stats_weekly`` rows
    for ``weeks`` only. The season board is not fetched.
    """
    if path:
        return load_sim_inputs(path), f"sim inputs: file {path}"
    return load_gangstash_sim_inputs(
        season=season,
        weeks=weeks,
        refresh_targets=refresh_targets,
        refresh_snaps=refresh_snaps,
        team_stats_scope=team_stats_scope,
    )


def load_gangstash_sim_inputs(
    *,
    season: int,
    weeks: list[int] | None,
    refresh_targets: bool = False,
    refresh_snaps: bool = False,
    team_stats_scope: str = "season",
) -> tuple[SimInputs | None, str]:
    """Season team stats, optional weekly rates, target weeks, RB snaps.

    Each dataset is independent. A missing key with no cache skips that
    dataset. If nothing usable comes back, the note is ``UNAVAILABLE_NOTE``.
    ``team_stats_scope="weekly"`` skips the season board and pools weekly
    sums for ``weeks`` (weeks before the backtest target). An empty week
    list leaves team inputs empty so dispersion stays at the league default.
    """
    weekly_only = (team_stats_scope or "season").strip().lower() == "weekly"
    if weekly_only:
        stats_rows, stats_meta, stats_err = [], {}, None
    else:
        stats_rows, stats_meta, stats_err = _pull(
            lambda: fetch_team_stats(season=season, refresh=False)
        )
    # ``weeks=[]`` is a backtest of week 1: no prior week exists. Do not
    # pull the season-to-date board (that includes the week being scored).
    # ``weeks is None`` keeps the optimizer's "every completed week" pull.
    prior = weeks is None or bool(weeks)
    weekly_rows: list[dict] = []
    weekly_meta: dict = {}
    weekly_err: str | None = None
    if weeks:
        weekly_rows, weekly_meta, weekly_err = _pull(
            lambda: fetch_team_stats_weekly(season=season, weeks=weeks, refresh=False)
        )
        weekly_rows = _rows_in_weeks(weekly_rows, weeks)
    if prior:
        target_rows, target_meta, target_err = _pull(
            lambda: fetch_targets(
                season=season,
                weeks=weeks,
                refresh=refresh_targets,
            )
        )
        snap_rows, snap_meta, snap_err = _pull(
            lambda: fetch_snaps(
                season=season,
                weeks=weeks,
                position="RB",
                refresh=refresh_snaps,
            )
        )
        stat_rows, stat_meta, stat_err = _pull(
            lambda: fetch_player_stats_weekly(
                season=season,
                weeks=weeks,
                refresh=False,
            )
        )
        usage_rows, usage_meta, usage_err = _pull(
            lambda: fetch_player_usage(
                season=season,
                weeks=weeks,
                refresh=False,
            )
        )
        if weeks:
            target_rows = _rows_in_weeks(target_rows, weeks)
            snap_rows = _rows_in_weeks(snap_rows, weeks)
            stat_rows = _rows_in_weeks(stat_rows, weeks)
            usage_rows = _rows_in_weeks(usage_rows, weeks)
    else:
        target_rows, target_meta, target_err = [], {}, None
        snap_rows, snap_meta, snap_err = [], {}, None
        stat_rows, stat_meta, stat_err = [], {}, None
        usage_rows, usage_meta, usage_err = [], {}, None
    if weekly_only:
        team_stats = _pooled_weekly_rows(weekly_rows)
    else:
        team_stats = _team_stats(stats_rows, weekly_rows)
    targets = _targets(target_rows)
    snaps = _snaps(snap_rows)
    carries = _carries(stat_rows)
    player_rows = _player_week_rows(stat_rows)
    targets, player_rows = _apply_player_usage(targets, player_rows, usage_rows)
    inputs = sim_inputs_from_records(
        team_stats=team_stats,
        targets=targets,
        snaps=snaps,
        carries=carries,
        player_weeks=player_rows,
        team_weeks=_weekly_team_rows(weekly_rows),
    )
    if inputs.empty:
        return None, UNAVAILABLE_NOTE
    stale = any(
        bool(meta.get("cache_stale"))
        for meta in (stats_meta, weekly_meta, target_meta, snap_meta, stat_meta, usage_meta)
    )
    skipped = [
        name
        for name, err in (
            ("team_stats", stats_err),
            ("team_stats_weekly", weekly_err),
            ("targets", target_err),
            ("snaps", snap_err),
            ("player_stats_weekly", stat_err),
            ("player_usage", usage_err),
        )
        if err
    ]
    note = (
        f"sim inputs: gangstash team_stats {len(inputs.team_stats)}  "
        f"targets {len(inputs.targets)}  snaps {len(inputs.snaps)}  "
        f"carries {len(inputs.carries)}  "
        f"player_weeks {len(inputs.player_weeks)}  "
        f"team_weeks {len(inputs.team_weeks)}  "
        f"usage {len(usage_rows)}"
    )
    if weekly_only:
        note += "  team_stats_scope weekly"
    if stale:
        note += "  stale cache"
    if skipped:
        note += "  skipped " + ",".join(skipped)
    return inputs, note


def _rows_in_weeks(rows: list[dict], weeks: list[int] | None) -> list[dict]:
    """Drop rows whose ``week`` is outside the requested prior window."""
    if not weeks:
        return rows
    want = {int(week) for week in weeks}
    kept: list[dict] = []
    for row in rows:
        raw = row.get("week")
        if raw is None or raw == "":
            continue
        try:
            week = int(raw)
        except (TypeError, ValueError):
            continue
        if week in want:
            kept.append(row)
    return kept


def _pull(fn) -> tuple[list[dict], dict, str | None]:
    try:
        rows, meta = fn()
    except GangstashDataKeyMissing:
        return [], {}, "key"
    except (GangstashDataError, GangstashTruncated):
        return [], {}, "error"
    if not isinstance(rows, list):
        return [], {}, "error"
    return [row for row in rows if isinstance(row, dict)], dict(meta or {}), None


def _fd(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return require_fd(text).fd
    except UnmappedTeam:
        return None


def _team_stats(season_rows: list[dict], weekly_rows: list[dict]) -> list[dict]:
    """Season rows keep EPA sums. Weekly rows overwrite script rates only."""
    stats: list[TeamStat] = []
    for row in season_rows:
        team = _fd(str(row.get("team_fd") or row.get("team") or ""))
        if team is None:
            continue
        mapped = dict(row)
        mapped["team_fd"] = team
        stat = team_stat_from_row(mapped)
        if stat is not None:
            stats.append(stat)
    if not weekly_rows:
        return [_as_row(stat) for stat in stats]
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in weekly_rows:
        team = _fd(str(row.get("team_fd") or row.get("team") or ""))
        if team is None:
            continue
        side = str(row.get("side") or "offense").strip().lower() or "offense"
        grouped.setdefault((team, side), []).append(row)
    seen: set[tuple[str, str]] = set()
    blended: list[TeamStat] = []
    for stat in stats:
        key = (stat.team_fd, (stat.side or "offense").strip().lower() or "offense")
        seen.add(key)
        blended.append(_overlay_rates(stat, grouped.get(key)))
    for key, rows in grouped.items():
        if key in seen:
            continue
        team, side = key
        blended.append(_overlay_rates(TeamStat(team_fd=team, side=side), rows))
    return [_as_row(stat) for stat in blended]


def _overlay_rates(stat: TeamStat, rows: list[dict] | None) -> TeamStat:
    if not rows:
        return stat
    updates: dict[str, float] = {}
    for field in _RATE_FIELDS:
        val = _mean_rate(rows, field)
        if val is not None:
            updates[field] = val
    if not updates:
        return stat
    return replace(stat, **updates)


def _mean_rate(rows: list[dict], field: str) -> float | None:
    vals: list[float] = []
    for row in rows:
        raw = row.get(field)
        if raw is None or raw == "":
            continue
        try:
            num = float(raw)
        except (TypeError, ValueError):
            continue
        if num > 1.5:
            num = num / 100.0
        vals.append(num)
    if not vals:
        return None
    return sum(vals) / len(vals)


def _as_row(stat: TeamStat) -> dict:
    return {
        "team_fd": stat.team_fd,
        "side": stat.side,
        "pass_rate": stat.pass_rate,
        "neutral_pass_rate": stat.neutral_pass_rate,
        "proe": stat.proe,
        "epa_per_play": stat.epa_per_play,
        "epa_var": stat.epa_var,
        "n": stat.n,
        "epa_sum": stat.epa_sum,
        "epa_sq_sum": stat.epa_sq_sum,
        "pass_n": stat.pass_n,
        "rush_n": stat.rush_n,
        "pass_epa_sum": stat.pass_epa_sum,
        "pass_epa_sq_sum": stat.pass_epa_sq_sum,
        "rush_epa_sum": stat.rush_epa_sum,
        "rush_epa_sq_sum": stat.rush_epa_sq_sum,
        "pass_epa_var": stat.pass_epa_var,
        "rush_epa_var": stat.rush_epa_var,
        "week": stat.week,
        "pass_epa_per_play": stat.pass_epa_per_play,
        "rush_epa_per_play": stat.rush_epa_per_play,
        "pass_success_rate": stat.pass_success_rate,
        "rush_success_rate": stat.rush_success_rate,
        "success_rate": stat.success_rate,
        "explosive_rate": stat.explosive_rate,
        "red_zone_td_rate": stat.red_zone_td_rate,
        "third_down_rate": stat.third_down_rate,
        "early_down_pass_epa_per_play": stat.early_down_pass_epa_per_play,
        "early_down_pass_n": stat.early_down_pass_n,
        "early_down_pass_success_rate": stat.early_down_pass_success_rate,
        "early_down_rush_epa_per_play": stat.early_down_rush_epa_per_play,
        "early_down_rush_n": stat.early_down_rush_n,
        "early_down_rush_success_rate": stat.early_down_rush_success_rate,
        "plays_per_game": stat.plays_per_game,
        "seconds_per_play": stat.seconds_per_play,
        "neutral_plays_per_game": stat.neutral_plays_per_game,
        "neutral_seconds_per_play": stat.neutral_seconds_per_play,
        "yards_per_carry_allowed": stat.yards_per_carry_allowed,
        "yards_per_dropback_allowed": stat.yards_per_dropback_allowed,
        "yards_per_attempt_allowed": stat.yards_per_attempt_allowed,
        "sack_rate": stat.sack_rate,
        "air_yards_per_attempt_allowed": stat.air_yards_per_attempt_allowed,
        "pace_games": stat.pace_games,
        "pace_plays": stat.pace_plays,
        "play_seconds": stat.play_seconds,
        "pace_neutral_plays": stat.pace_neutral_plays,
        "neutral_play_seconds": stat.neutral_play_seconds,
        "timed_plays": stat.timed_plays,
        "neutral_timed_plays": stat.neutral_timed_plays,
        "rush_yards": stat.rush_yards,
        "net_pass_yards": stat.net_pass_yards,
        "air_yards": stat.air_yards,
        "yards_per_carry": stat.yards_per_carry,
        "yards_per_dropback": stat.yards_per_dropback,
        "yards_per_pass_attempt": stat.yards_per_pass_attempt,
        "air_yards_per_attempt": stat.air_yards_per_attempt,
    }


def _pooled_weekly_rows(rows: list[dict]) -> list[dict]:
    """Sum weekly team rows into one offense and one defense board per team.

    ``epa_var`` stays empty so ``epa_variance`` uses the pooled sums. A
    missing square-sum drops that variance (league default) instead of
    mixing a partial second moment. Rates are play-weighted.
    """
    parsed: list[TeamStat] = []
    for row in rows:
        mapped = _mapped_team_row(row)
        if mapped is None:
            continue
        stat = team_stat_from_row(mapped)
        if stat is not None:
            parsed.append(stat)
    grouped: dict[tuple[str, str], list[TeamStat]] = {}
    for stat in parsed:
        side = (stat.side or "offense").strip().lower() or "offense"
        grouped.setdefault((stat.team_fd, side), []).append(stat)
    return [_as_row(_pool_team_side(team, side, items)) for (team, side), items in grouped.items()]


def _pool_team_side(team: str, side: str, rows: list[TeamStat]) -> TeamStat:
    n = _sum_counts(rows, "n")
    pass_n = _sum_counts(rows, "pass_n")
    rush_n = _sum_counts(rows, "rush_n")
    early_pass_n = _sum_counts(rows, "early_down_pass_n")
    early_rush_n = _sum_counts(rows, "early_down_rush_n")
    epa_sum = _sum_epa(rows, "epa_sum", "epa_per_play", "n")
    pass_epa_sum = _sum_epa(rows, "pass_epa_sum", "pass_epa_per_play", "pass_n")
    rush_epa_sum = _sum_epa(rows, "rush_epa_sum", "rush_epa_per_play", "rush_n")
    return TeamStat(
        team_fd=team,
        side=side,
        pass_rate=_weighted_mean(rows, "pass_rate", "n"),
        neutral_pass_rate=_weighted_mean(rows, "neutral_pass_rate", "n"),
        proe=_weighted_mean(rows, "proe", "n"),
        epa_per_play=(epa_sum / n) if epa_sum is not None and n else None,
        epa_var=None,
        n=n,
        epa_sum=epa_sum,
        epa_sq_sum=_sum_squares(rows, "epa_sq_sum", "n"),
        pass_n=pass_n,
        rush_n=rush_n,
        pass_epa_sum=pass_epa_sum,
        pass_epa_sq_sum=_sum_squares(rows, "pass_epa_sq_sum", "pass_n"),
        rush_epa_sum=rush_epa_sum,
        rush_epa_sq_sum=_sum_squares(rows, "rush_epa_sq_sum", "rush_n"),
        pass_epa_var=None,
        rush_epa_var=None,
        week=None,
        pass_epa_per_play=(
            (pass_epa_sum / pass_n) if pass_epa_sum is not None and pass_n else None
        ),
        rush_epa_per_play=(
            (rush_epa_sum / rush_n) if rush_epa_sum is not None and rush_n else None
        ),
        pass_success_rate=_weighted_mean(rows, "pass_success_rate", "pass_n"),
        rush_success_rate=_weighted_mean(rows, "rush_success_rate", "rush_n"),
        success_rate=_weighted_mean(rows, "success_rate", "n"),
        explosive_rate=_weighted_mean(rows, "explosive_rate", "n"),
        red_zone_td_rate=_weighted_mean(rows, "red_zone_td_rate", "n"),
        third_down_rate=_weighted_mean(rows, "third_down_rate", "n"),
        early_down_pass_epa_per_play=_weighted_mean(
            rows, "early_down_pass_epa_per_play", "early_down_pass_n"
        ),
        early_down_pass_n=early_pass_n,
        early_down_pass_success_rate=_weighted_mean(
            rows, "early_down_pass_success_rate", "early_down_pass_n"
        ),
        early_down_rush_epa_per_play=_weighted_mean(
            rows, "early_down_rush_epa_per_play", "early_down_rush_n"
        ),
        early_down_rush_n=early_rush_n,
        early_down_rush_success_rate=_weighted_mean(
            rows, "early_down_rush_success_rate", "early_down_rush_n"
        ),
        plays_per_game=_plays_per_game_pool(rows),
        seconds_per_play=_ratio_pool(
            rows, "seconds_per_play", "play_seconds", "timed_plays", "n"
        ),
        neutral_plays_per_game=_weighted_mean(rows, "neutral_plays_per_game", "n"),
        neutral_seconds_per_play=_ratio_pool(
            rows,
            "neutral_seconds_per_play",
            "neutral_play_seconds",
            "neutral_timed_plays",
            "n",
        ),
        yards_per_carry_allowed=_paired_rate(
            rows, "yards_per_carry_allowed", "yards_per_carry", "rush_n"
        ),
        yards_per_dropback_allowed=_paired_rate(
            rows, "yards_per_dropback_allowed", "yards_per_dropback", "pass_n"
        ),
        yards_per_attempt_allowed=_paired_rate(
            rows, "yards_per_attempt_allowed", "yards_per_pass_attempt", "pass_n"
        ),
        sack_rate=_weighted_mean(rows, "sack_rate", "pass_n"),
        air_yards_per_attempt_allowed=_paired_rate(
            rows, "air_yards_per_attempt_allowed", "air_yards_per_attempt", "pass_n"
        ),
        pace_games=_sum_pace_games(rows),
        pace_plays=_sum_optional(rows, "pace_plays"),
        play_seconds=_sum_optional(rows, "play_seconds"),
        pace_neutral_plays=_sum_optional(rows, "pace_neutral_plays"),
        neutral_play_seconds=_sum_optional(rows, "neutral_play_seconds"),
        timed_plays=_sum_optional(rows, "timed_plays"),
        neutral_timed_plays=_sum_optional(rows, "neutral_timed_plays"),
        rush_yards=_sum_optional(rows, "rush_yards"),
        net_pass_yards=_sum_optional(rows, "net_pass_yards"),
        air_yards=_sum_optional(rows, "air_yards"),
        yards_per_carry=_paired_rate(rows, "yards_per_carry", "yards_per_carry_allowed", "rush_n"),
        yards_per_dropback=_paired_rate(
            rows, "yards_per_dropback", "yards_per_dropback_allowed", "pass_n"
        ),
        yards_per_pass_attempt=_paired_rate(
            rows, "yards_per_pass_attempt", "yards_per_attempt_allowed", "pass_n"
        ),
        air_yards_per_attempt=_paired_rate(
            rows, "air_yards_per_attempt", "air_yards_per_attempt_allowed", "pass_n"
        ),
    )


def _sum_counts(rows: list[TeamStat], attr: str) -> int | None:
    total = 0
    any_count = False
    for row in rows:
        raw = getattr(row, attr)
        if raw is None:
            continue
        total += int(raw)
        any_count = True
    return total if any_count else None


def _sum_epa(
    rows: list[TeamStat], sum_attr: str, per_attr: str, n_attr: str
) -> float | None:
    """Sum EPA. ``epa_per_play * n`` fills a missing sum. A play count with neither is a gap."""
    total = 0.0
    any_part = False
    for row in rows:
        count = getattr(row, n_attr) or 0
        direct = getattr(row, sum_attr)
        per = getattr(row, per_attr)
        if direct is not None:
            total += float(direct)
            any_part = True
        elif per is not None and count:
            total += float(per) * int(count)
            any_part = True
        elif count:
            return None
    return total if any_part else None


def _sum_squares(rows: list[TeamStat], sq_attr: str, n_attr: str) -> float | None:
    """Square-sum only when every row that has plays also has the square sum."""
    total = 0.0
    any_part = False
    for row in rows:
        count = getattr(row, n_attr) or 0
        sq = getattr(row, sq_attr)
        if sq is None:
            if count:
                return None
            continue
        total += float(sq)
        any_part = True
    return total if any_part else None


def _weighted_mean(rows: list[TeamStat], val_attr: str, n_attr: str) -> float | None:
    num = 0.0
    den = 0.0
    for row in rows:
        val = getattr(row, val_attr)
        count = getattr(row, n_attr)
        if val is None or not count:
            continue
        num += float(val) * int(count)
        den += int(count)
    if den <= 0:
        return None
    return num / den


def _mapped_team_row(row: dict) -> dict | None:
    team = _fd(str(row.get("team_fd") or row.get("team") or ""))
    if team is None:
        return None
    mapped = dict(row)
    mapped["team_fd"] = team
    return mapped


def _weekly_team_rows(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        mapped = _mapped_team_row(row)
        if mapped is not None:
            out.append(mapped)
    return out


def _player_week_rows(rows: list[dict]) -> list[dict]:
    """Every skill row, with FanDuel abbrevs. Optional columns pass through."""
    out: list[dict] = []
    for row in rows:
        mapped = _mapped_team_row(row)
        if mapped is None:
            continue
        opp = row.get("opponent") or row.get("opponent_fd") or row.get("opp")
        if opp:
            opp_fd = _fd(str(opp))
            if opp_fd:
                mapped["opponent"] = opp_fd
        out.append(mapped)
    return out


def _targets(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        try:
            item = parse_target_row(row)
        except (GangstashDataError, UnmappedTeam):
            continue
        if item is not None:
            out.append(item)
    return out


def _carries(rows: list[dict]) -> list[dict]:
    """RB carry counts from player_stats_weekly. Other positions are dropped."""
    out: list[dict] = []
    for row in rows:
        item = parse_player_stat_row(row)
        if item is None or item.get("carries") is None:
            continue
        if item.get("position") not in {"", "RB"}:
            continue
        out.append(item)
    return out


def _snaps(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        try:
            item = parse_snap_row(row)
        except (GangstashDataError, UnmappedTeam):
            continue
        if item is None or item.get("position") != "RB":
            continue
        out.append(item)
    return out


def _positive_share(val) -> float | None:
    if val is None or val == "":
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if num > 1.5:
        num = num / 100.0
    if num > 0:
        return num
    return None


def _usage_share(row: dict) -> float | None:
    """Target share from usage. A zero target share does not win by itself."""
    tgt = _positive_share(row.get("target_share"))
    air = _positive_share(row.get("air_yards_share"))
    if tgt is not None and air is not None:
        return 0.75 * tgt + 0.25 * air
    if tgt is not None:
        return tgt
    if air is not None:
        return air
    raw = row.get("wopr")
    if raw is None or raw == "":
        return None
    try:
        wopr = float(raw)
    except (TypeError, ValueError):
        return None
    if wopr <= 0 or WOPR_SHARE_DIVISOR <= 0:
        return None
    return min(1.0, max(0.0, wopr / WOPR_SHARE_DIVISOR))


def _week_of(row: dict) -> int | None:
    raw = row.get("week")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _index_by_id(rows: list[dict]) -> tuple[dict, dict]:
    by_gsis: dict[tuple[str, int], dict] = {}
    by_pid: dict[tuple[str, int], dict] = {}
    for row in rows:
        week = _week_of(row)
        if week is None:
            continue
        gsis = str(row.get("gsis_id") or "").strip()
        pid = str(row.get("player_id") or "").strip()
        if gsis:
            by_gsis.setdefault((gsis, week), row)
        if pid:
            by_pid.setdefault((pid, week), row)
    return by_gsis, by_pid


def _find_row(by_gsis: dict, by_pid: dict, row: dict) -> dict | None:
    week = _week_of(row)
    if week is None:
        return None
    gsis = str(row.get("gsis_id") or "").strip()
    if gsis and (gsis, week) in by_gsis:
        return by_gsis[(gsis, week)]
    pid = str(row.get("player_id") or "").strip()
    if pid and (pid, week) in by_pid:
        return by_pid[(pid, week)]
    return None


def _blank_field(row: dict, key: str) -> bool:
    val = row.get(key)
    if val is None:
        return True
    if isinstance(val, str) and not str(val).strip():
        return True
    return False


def _first_present(row: dict, keys: tuple[str, ...]):
    for key in keys:
        val = row.get(key)
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        return val
    return None


_USAGE_COPIES = (
    ("receiving_air_yards", ("receiving_air_yards",)),
    ("air_yards_share", ("air_yards_share",)),
    ("wopr", ("wopr",)),
    ("target_share", ("target_share",)),
    ("red_zone_targets", ("red_zone_targets", "rz_targets")),
    ("red_zone_carries", ("red_zone_carries", "rz_carries")),
    ("goal_line_carries", ("goal_line_carries", "gl_carries")),
    ("rz_receiving_tds", ("rz_receiving_tds",)),
    ("rz_rushing_tds", ("rz_rushing_tds",)),
)


def _copy_usage(dest: dict, source: dict) -> None:
    for dest_key, source_keys in _USAGE_COPIES:
        if not _blank_field(dest, dest_key):
            continue
        val = _first_present(source, source_keys)
        if val is not None:
            dest[dest_key] = val


def _apply_player_usage(
    targets: list[dict],
    player_rows: list[dict],
    usage_rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Blend usage shares onto targets and fill blank efficiency columns.

    Join is ``(gsis_id, week)`` then ``(player_id, week)``. A usage row that
    matches neither list is kept on its own when it has an id or a name.
    """
    if not usage_rows:
        return targets, player_rows
    tgt_gsis, tgt_pid = _index_by_id(targets)
    st_gsis, st_pid = _index_by_id(player_rows)
    extra_targets: list[dict] = []
    extra_players: list[dict] = []
    for raw in usage_rows:
        if not isinstance(raw, dict):
            continue
        team = _fd(str(raw.get("team_fd") or raw.get("team") or ""))
        if team is None:
            continue
        row = dict(raw)
        row["team_fd"] = team
        week = _week_of(row)
        if week is None:
            continue
        gsis = str(row.get("gsis_id") or "").strip()
        pid = str(row.get("player_id") or "").strip()
        name = str(row.get("player_name") or row.get("name") or "").strip()
        if not gsis and not pid and not name:
            continue
        share = _usage_share(row)
        hit = _find_row(tgt_gsis, tgt_pid, row)
        if hit is not None and share is not None:
            existing = _positive_share(hit.get("target_share"))
            hit["target_share"] = (
                share if existing is None else 0.5 * existing + 0.5 * share
            )
        elif hit is None and share is not None:
            raw_targets = row.get("targets")
            try:
                targets_val = (
                    0.0 if raw_targets is None or raw_targets == "" else float(raw_targets)
                )
            except (TypeError, ValueError):
                targets_val = 0.0
            extra_targets.append(
                {
                    "player_name": name,
                    "team_fd": team,
                    "position": str(row.get("position") or "WR").strip().upper() or "WR",
                    "week": week,
                    "season": row.get("season") or 0,
                    "targets": targets_val,
                    "target_share": share,
                    "gsis_id": gsis or None,
                    "player_id": pid or None,
                }
            )
        stat = _find_row(st_gsis, st_pid, row)
        if stat is not None:
            _copy_usage(stat, row)
        else:
            extra_players.append(row)
    return targets + extra_targets, player_rows + extra_players


def _sum_optional(rows: list[TeamStat], attr: str) -> float | None:
    total = 0.0
    seen = False
    for row in rows:
        raw = getattr(row, attr)
        if raw is None:
            continue
        total += float(raw)
        seen = True
    return total if seen else None


def _sum_pace_games(rows: list[TeamStat]) -> float | None:
    """Sum games. A plays-per-game row with no pace_games counts as one."""
    total = 0.0
    seen = False
    for row in rows:
        if row.pace_games is not None and float(row.pace_games) > 0:
            total += float(row.pace_games)
            seen = True
        elif row.plays_per_game is not None:
            total += 1.0
            seen = True
    return total if seen else None


def _plays_per_game_pool(rows: list[TeamStat]) -> float | None:
    weighted = _weighted_mean(rows, "plays_per_game", "n")
    if weighted is not None:
        return weighted
    num = 0.0
    den = 0.0
    for row in rows:
        if row.plays_per_game is None:
            continue
        weight = float(row.pace_games) if row.pace_games else 1.0
        num += float(row.plays_per_game) * weight
        den += weight
    if den <= 0:
        return None
    return num / den


def _ratio_pool(
    rows: list[TeamStat],
    rate_attr: str,
    num_attr: str,
    den_attr: str,
    fallback_n: str,
) -> float | None:
    """Sum(numerator) / sum(denominator), else a weighted mean of the rate."""
    num = _sum_optional(rows, num_attr)
    den = _sum_optional(rows, den_attr)
    if num is not None and den is not None and den > 0:
        return num / den
    weighted = _weighted_mean(rows, rate_attr, den_attr)
    if weighted is not None:
        return weighted
    return _weighted_mean(rows, rate_attr, fallback_n)


def _paired_rate(
    rows: list[TeamStat], primary: str, fallback: str, n_attr: str
) -> float | None:
    got = _weighted_mean(rows, primary, n_attr)
    if got is not None:
        return got
    got = _weighted_mean(rows, fallback, n_attr)
    if got is not None:
        return got
    vals: list[float] = []
    for row in rows:
        val = getattr(row, primary)
        if val is None:
            val = getattr(row, fallback)
        if val is not None:
            vals.append(float(val))
    if not vals:
        return None
    return sum(vals) / len(vals)
