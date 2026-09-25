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
    fetch_snaps,
    fetch_targets,
    fetch_team_stats,
    fetch_team_stats_weekly,
    parse_player_stat_row,
    parse_snap_row,
    parse_target_row,
)
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


def resolve_sim_inputs(
    *,
    path: str | None,
    season: int,
    weeks: list[int] | None,
    refresh_targets: bool = False,
    refresh_snaps: bool = False,
) -> tuple[SimInputs | None, str]:
    """File override, else the gangstash feed.

    A bad ``path`` raises ``SimInputError``. An unavailable feed returns
    ``(None, note)`` and does not raise.
    """
    if path:
        return load_sim_inputs(path), f"sim inputs: file {path}"
    return load_gangstash_sim_inputs(
        season=season,
        weeks=weeks,
        refresh_targets=refresh_targets,
        refresh_snaps=refresh_snaps,
    )


def load_gangstash_sim_inputs(
    *,
    season: int,
    weeks: list[int] | None,
    refresh_targets: bool = False,
    refresh_snaps: bool = False,
) -> tuple[SimInputs | None, str]:
    """Season team stats, optional weekly rates, target weeks, RB snaps.

    Each dataset is independent. A missing key with no cache skips that
    dataset. If nothing usable comes back, the note is ``UNAVAILABLE_NOTE``.
    """
    stats_rows, stats_meta, stats_err = _pull(
        lambda: fetch_team_stats(season=season, refresh=False)
    )
    weekly_rows: list[dict] = []
    weekly_meta: dict = {}
    weekly_err: str | None = None
    if weeks:
        weekly_rows, weekly_meta, weekly_err = _pull(
            lambda: fetch_team_stats_weekly(season=season, weeks=weeks, refresh=False)
        )
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
    team_stats = _team_stats(stats_rows, weekly_rows)
    targets = _targets(target_rows)
    snaps = _snaps(snap_rows)
    carries = _carries(stat_rows)
    inputs = sim_inputs_from_records(
        team_stats=team_stats,
        targets=targets,
        snaps=snaps,
        carries=carries,
    )
    if inputs.empty:
        return None, UNAVAILABLE_NOTE
    stale = any(
        bool(meta.get("cache_stale"))
        for meta in (stats_meta, weekly_meta, target_meta, snap_meta, stat_meta)
    )
    skipped = [
        name
        for name, err in (
            ("team_stats", stats_err),
            ("team_stats_weekly", weekly_err),
            ("targets", target_err),
            ("snaps", snap_err),
            ("player_stats_weekly", stat_err),
        )
        if err
    ]
    note = (
        f"sim inputs: gangstash team_stats {len(inputs.team_stats)}  "
        f"targets {len(inputs.targets)}  snaps {len(inputs.snaps)}  "
        f"carries {len(inputs.carries)}"
    )
    if stale:
        note += "  stale cache"
    if skipped:
        note += "  skipped " + ",".join(skipped)
    return inputs, note


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
    }


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
