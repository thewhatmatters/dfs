"""Gangstash `/data` datasets: targets, game lines, depth, team stats.

Field names match the live Edge Function. Fetch/cache is
`nfl.gangstash.fetch_dataset` (pages of 1,000, cap 20,000). This module
does not score.

RB snap share is not on these datasets. The RB usage blend still reads
Lineups snap share.

Do not send a Supabase service-role key. Auth is `GANGSTASH_API_KEY`
(`x-api-key`). The server also accepts Bearer; this client sends `x-api-key`.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date

from nfl.gangstash import (
    GangstashDataError,
    GangstashDataKeyMissing,
    GangstashTruncated,
    dataset_id,
    fetch_dataset,
)
from nfl.names import match_key
from nfl.teams import UnmappedTeam, require_fd

TARGET_POSITIONS = frozenset({"RB", "WR", "TE"})
BASE_OFFENSE_POS_GRP = "3WR 1TE"
TEAM_STATS_SIDES = frozenset({"offense", "defense"})
DEFAULT_SEASON_TYPE = "REG"


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


def _first_float(row: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in row:
            continue
        val = _float(row.get(key))
        if val is not None:
            return val
    return None


def fetch_targets(
    *,
    season: int,
    weeks: list[int] | None = None,
    position: str | None = None,
    team: str | None = None,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """`dataset=targets`. `season` is required. `week` is a comma list when set."""
    if int(season) < 1:
        raise GangstashDataError("gangstash targets requires season")
    params: dict[str, str] = {"season": str(int(season))}
    if weeks:
        params["week"] = ",".join(str(int(w)) for w in weeks)
    if position:
        params["position"] = position.strip().upper()
    if team:
        params["team"] = team.strip().upper()
    return fetch_dataset(
        dataset_id("targets"),
        params,
        refresh=refresh,
        cache_day=cache_day,
    )


def _columns_present(rows: list, keys: tuple[str, ...]) -> bool:
    """True when at least one row carries every named column (schema present)."""
    for row in rows:
        if isinstance(row, dict) and all(key in row for key in keys):
            return True
    return False


def _warn_skipped(dataset: str, skipped: int, reason: str) -> None:
    if skipped < 1:
        return
    noun = "row" if skipped == 1 else "rows"
    print(
        f"gangstash {dataset} skipped {skipped} {noun} with {reason}",
        file=sys.stderr,
    )


def parse_target_row(row: dict) -> dict | None:
    """One player-week. None when position is not RB/WR/TE or the name is blank.

    Live fields: season, week, position, player_name, team_fd, targets,
    target_share, air_yards_share, wopr, receptions, rec_yards, team_targets,
    team_pass_attempts, gsis_id, player_id. No targets_total or targets_avg.
    A null `player_name` is an unmatched id, not a schema break. Callers count
    those rows. An empty payload or a missing `player_name` column is fatal
    in `aggregate_target_window`.
    """
    if not isinstance(row, dict):
        return None
    name = _str(row.get("player_name"))
    if not name:
        return None
    team_raw = _str(row.get("team_fd"))
    if not team_raw:
        raise GangstashDataError(f"gangstash targets row missing team_fd for {name}")
    pos = _str(row.get("position")).upper()
    if not pos:
        raise GangstashDataError(f"gangstash targets row missing position for {name}")
    if pos not in TARGET_POSITIONS:
        return None
    week = _int(row.get("week"))
    if week is None or week < 1:
        raise GangstashDataError(f"gangstash targets row missing week for {name}")
    targets = _int(row.get("targets"))
    if targets is None:
        raise GangstashDataError(f"gangstash targets row missing targets for {name}")
    team = require_fd(team_raw)
    return {
        "player_name": name,
        "team_fd": team.fd,
        "position": pos,
        "week": week,
        "season": _int(row.get("season")),
        "targets": targets,
        "target_share": _float(row.get("target_share")),
        "team_targets": _int(row.get("team_targets")),
        "team_pass_attempts": _int(row.get("team_pass_attempts")),
        "air_yards_share": _float(row.get("air_yards_share")),
        "wopr": _float(row.get("wopr")),
        "receptions": _int(row.get("receptions")),
        "rec_yards": _int(row.get("rec_yards")),
        "gsis_id": _str(row.get("gsis_id")) or None,
        "player_id": _str(row.get("player_id")) or None,
        "carries": _float(
            row.get("carries")
            if row.get("carries") is not None
            else row.get("rushing_attempts")
            if row.get("rushing_attempts") is not None
            else row.get("rush_attempts")
        ),
        "team_carries": _float(
            row.get("team_carries")
            if row.get("team_carries") is not None
            else row.get("team_rush_attempts")
        ),
    }


def aggregate_target_window(
    rows: list[dict],
    *,
    weeks: list[int] | None = None,
) -> list[dict]:
    """One row per player. `target_share` = sum(targets) / sum(team_targets).

    Every output row uses the same `week` label (max week in the window) so
    the existing single-week join keeps the whole window. A single week with
    no `team_targets` falls back to that row's `target_share`.
    """
    if not rows:
        raise GangstashDataError("gangstash targets response is empty")
    if not _columns_present(rows, ("player_name",)):
        raise GangstashDataError("gangstash targets columns absent: player_name")
    want = set(weeks) if weeks else None
    by_week: dict[tuple, dict] = {}
    order: list[tuple] = []
    skipped = 0
    for row in rows:
        if isinstance(row, dict) and not _str(row.get("player_name")):
            skipped += 1
            continue
        item = parse_target_row(row)
        if item is None:
            continue
        if want is not None and item["week"] not in want:
            continue
        key = (
            item["team_fd"],
            match_key(item["player_name"]),
            item["position"],
            item["week"],
        )
        if key in by_week:
            continue
        by_week[key] = item
        order.append(key)

    groups: dict[tuple, list[dict]] = {}
    group_order: list[tuple] = []
    for key in order:
        item = by_week[key]
        gkey = key[:3]
        if gkey not in groups:
            groups[gkey] = []
            group_order.append(gkey)
        groups[gkey].append(item)
    _warn_skipped("targets", skipped, "null player_name")
    if not groups:
        return []

    label = max(item["week"] for item in by_week.values())
    out: list[dict] = []
    for gkey in group_order:
        items = groups[gkey]
        tgt_sum = sum(int(i["targets"]) for i in items)
        known_tt = [i["team_targets"] for i in items if i["team_targets"] is not None]
        missing_tt = len(known_tt) != len(items)
        team_sum = sum(int(v) for v in known_tt)
        name = items[0]["player_name"]
        if not missing_tt and team_sum > 0:
            share = tgt_sum / team_sum
        elif len(items) == 1 and items[0]["target_share"] is not None:
            share = float(items[0]["target_share"])
        elif missing_tt:
            raise GangstashDataError(
                "gangstash targets window needs team_targets on each row to compute "
                "sum(targets)/sum(team_targets); field missing for "
                f"{name}"
            )
        else:
            raise GangstashDataError(
                f"gangstash targets team_targets sum is 0 for {name}"
            )
        n_weeks = len(items)

        def _keep(key: str):
            found = None
            for item in items:
                if item.get(key):
                    found = item[key]
            return found

        out.append(
            {
                "player_name": name,
                "team_fd": items[0]["team_fd"],
                "position": items[0]["position"],
                "week": label,
                "weeks": [int(i["week"]) for i in items],
                "targets": tgt_sum,
                "target_share": float(share),
                "team_targets": team_sum,
                "targets_total": tgt_sum,
                "targets_avg": tgt_sum / n_weeks,
                "gsis_id": _keep("gsis_id"),
                "player_id": _keep("player_id"),
            }
        )
    return out


def fetch_snaps(
    *,
    season: int,
    weeks: list[int] | None = None,
    position: str | None = None,
    team: str | None = None,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """`dataset=snaps`. `season` is required. `week` is a comma list when set."""
    if int(season) < 1:
        raise GangstashDataError("gangstash snaps requires season")
    params: dict[str, str] = {"season": str(int(season))}
    if weeks:
        params["week"] = ",".join(str(int(w)) for w in weeks)
    if position:
        params["position"] = position.strip().upper()
    if team:
        params["team"] = team.strip().upper()
    return fetch_dataset(
        dataset_id("snaps"),
        params,
        refresh=refresh,
        cache_day=cache_day,
    )


def parse_snap_row(row: dict) -> dict | None:
    """One player-week. None when position is not RB/WR/TE or identity is blank.

    Live fields: season, week, position, player_name, team_fd, offense_snaps,
    offense_pct (0–1 fraction, same scale as Lineups snap_share), gsis_id,
    player_id, opponent, game_id, defense_snaps, st_snaps.
    A null player_name, team_fd, or offense_pct is skipped by
    `aggregate_snap_window`. Join stays name + team; ids are kept, not matched.
    """
    if not isinstance(row, dict):
        return None
    name = _str(row.get("player_name"))
    if not name:
        return None
    team_raw = _str(row.get("team_fd"))
    if not team_raw:
        return None
    pct = _float(row.get("offense_pct"))
    if pct is None:
        return None
    pos = _str(row.get("position")).upper()
    if not pos:
        raise GangstashDataError(f"gangstash snaps row missing position for {name}")
    if pos not in TARGET_POSITIONS:
        return None
    if pct < 0 or pct > 1:
        raise GangstashDataError(
            f"gangstash snaps offense_pct {pct} for {name} is outside 0-1 "
            "(expected a fraction, same scale as Lineups snap_share)"
        )
    week = _int(row.get("week"))
    if week is None or week < 1:
        raise GangstashDataError(f"gangstash snaps row missing week for {name}")
    snaps = _int(row.get("offense_snaps"))
    if snaps is None:
        raise GangstashDataError(f"gangstash snaps row missing offense_snaps for {name}")
    team = require_fd(team_raw)
    return {
        "player_name": name,
        "team_fd": team.fd,
        "position": pos,
        "week": week,
        "season": _int(row.get("season")),
        "offense_snaps": snaps,
        "offense_pct": pct,
        "gsis_id": _str(row.get("gsis_id")) or None,
        "player_id": _str(row.get("player_id")) or None,
    }


def _snap_row_gap(row: dict) -> bool:
    if not _str(row.get("player_name")):
        return True
    if not _str(row.get("team_fd")):
        return True
    pct = row.get("offense_pct")
    return pct is None or pct == ""


def _window_snap_share(items: list[dict]) -> float:
    """sum(offense_snaps) / sum(team offense snaps).

    Team snaps for a week are offense_snaps / offense_pct. One week therefore
    returns offense_pct unchanged (already a 0–1 fraction).
    """
    snaps = sum(int(i["offense_snaps"]) for i in items)
    denom = 0.0
    for item in items:
        pct = float(item["offense_pct"])
        played = int(item["offense_snaps"])
        if pct > 0:
            denom += played / pct
        elif played == 0:
            continue
        else:
            raise GangstashDataError(
                "gangstash snaps offense_pct is 0 with offense_snaps "
                f"{played} for {item['player_name']}"
            )
    if denom > 0:
        return snaps / denom
    return float(items[-1]["offense_pct"])


def aggregate_snap_window(
    rows: list[dict],
    *,
    weeks: list[int] | None = None,
) -> list[dict]:
    """One row per player. `snap_share` is the window offense-snap fraction.

    Every output row uses the same `week` label (max week in the window) so
    the existing single-week join keeps the whole window.
    """
    if not rows:
        raise GangstashDataError("gangstash snaps response is empty")
    if not _columns_present(rows, ("player_name", "team_fd", "offense_pct")):
        raise GangstashDataError(
            "gangstash snaps columns absent: player_name, team_fd, offense_pct"
        )
    want = set(weeks) if weeks else None
    by_week: dict[tuple, dict] = {}
    order: list[tuple] = []
    skipped = 0
    for row in rows:
        if isinstance(row, dict) and _snap_row_gap(row):
            skipped += 1
            continue
        item = parse_snap_row(row)
        if item is None:
            continue
        if want is not None and item["week"] not in want:
            continue
        key = (
            item["team_fd"],
            match_key(item["player_name"]),
            item["position"],
            item["week"],
        )
        if key in by_week:
            continue
        by_week[key] = item
        order.append(key)

    groups: dict[tuple, list[dict]] = {}
    group_order: list[tuple] = []
    for key in order:
        item = by_week[key]
        gkey = key[:3]
        if gkey not in groups:
            groups[gkey] = []
            group_order.append(gkey)
        groups[gkey].append(item)
    _warn_skipped(
        "snaps",
        skipped,
        "null player_name, team_fd, or offense_pct",
    )
    if not groups:
        return []

    label = max(item["week"] for item in by_week.values())
    out: list[dict] = []
    for gkey in group_order:
        items = groups[gkey]

        def _keep(key: str) -> str | None:
            found = None
            for item in items:
                if item.get(key):
                    found = item[key]
            return found

        snap_sum = sum(int(i["offense_snaps"]) for i in items)
        n_weeks = len(items)
        out.append(
            {
                "player_name": items[0]["player_name"],
                "team_fd": items[0]["team_fd"],
                "position": items[0]["position"],
                "week": label,
                "weeks": [int(i["week"]) for i in items],
                "snaps": snap_sum,
                "snap_share": _window_snap_share(items),
                "snaps_avg": snap_sum / n_weeks,
                "snaps_total": snap_sum,
                "gsis_id": _keep("gsis_id"),
                "player_id": _keep("player_id"),
            }
        )
    return out


@dataclass(frozen=True)
class GangstashGameLine:
    """Home spread is negative when home is favored (same sign as Odds API)."""

    home_fd: str
    away_fd: str
    spread: float
    total: float
    home_moneyline: float | None
    away_moneyline: float | None
    commence_time: str | None


def parse_game_line(row: dict) -> GangstashGameLine | None:
    """Live ``game_lines`` or ``closing_lines``.

    ``spread`` and live ``home_line`` are the home line (negative = home
    favored), the same sign as the Odds API. Neither is flipped.
    ``closing_lines`` may also be nflverse-shaped: ``spread_line`` is
    positive when the home team is favored, so the stored home spread is
    ``-spread_line``. Home and away implied totals win when both are
    present, including ``implied_home_total`` / ``implied_away_total`` and
    ``home_implied_tt`` / ``away_implied_tt``. Teams may be ``home_team_fd``
    or ``home_team``. A row that still has no spread and total returns
    None so one bad close does not abort the week. ``kickoff`` is accepted
    as ``commence_time`` when it is a real timestamp; null stays empty.
    """
    if not isinstance(row, dict):
        return None
    home_raw = _str(row.get("home_team_fd") or row.get("home_team") or row.get("home"))
    away_raw = _str(row.get("away_team_fd") or row.get("away_team") or row.get("away"))
    spread = _float(row.get("spread"))
    total = _float(row.get("total"))
    spread_line = _float(row.get("spread_line"))
    total_line = _float(row.get("total_line"))
    home_line = _float(row.get("home_line"))
    home_impl = _first_float(
        row,
        (
            "implied_home_total",
            "home_implied_total",
            "home_implied",
            "home_implied_tt",
            "implied_home",
            "home_team_total",
        ),
    )
    away_impl = _first_float(
        row,
        (
            "implied_away_total",
            "away_implied_total",
            "away_implied",
            "away_implied_tt",
            "implied_away",
            "away_team_total",
        ),
    )
    if (
        not home_raw
        and not away_raw
        and spread is None
        and total is None
        and spread_line is None
        and total_line is None
        and home_impl is None
        and away_impl is None
        and home_line is None
    ):
        return None
    if not home_raw or not away_raw:
        return None
    if home_impl is not None and away_impl is not None:
        total = home_impl + away_impl
        # Home spread is negative when home is favored.
        spread = away_impl - home_impl
    elif spread_line is not None and (total_line is not None or total is not None):
        # nflverse spread_line is positive when the home team is favored.
        spread = -spread_line
        if total_line is not None:
            total = total_line
    elif spread is None and home_line is not None and total is not None:
        # Live closing_lines home_line already uses the Odds sign.
        spread = home_line
    if spread is None or total is None:
        return None
    try:
        home = require_fd(home_raw)
        away = require_fd(away_raw)
    except UnmappedTeam:
        return None
    kickoff = _str(row.get("kickoff")) or None
    commence = _str(row.get("commence_time")) or kickoff
    return GangstashGameLine(
        home_fd=home.fd,
        away_fd=away.fd,
        spread=spread,
        total=total,
        home_moneyline=_float(row.get("home_moneyline")),
        away_moneyline=_float(row.get("away_moneyline")),
        commence_time=commence,
    )


def map_game_lines(rows: list[dict]) -> list[GangstashGameLine]:
    out: list[GangstashGameLine] = []
    for row in rows:
        parsed = parse_game_line(row)
        if parsed is not None:
            out.append(parsed)
    return out


def fetch_closing_lines(
    *,
    season: int,
    week: int,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """`dataset=closing_lines`. Past weeks prefer this over ``game_lines``.

    Rows may carry ``spread`` and ``total``, live ``home_line`` (negative
    when home is favored) plus ``total``, nflverse ``spread_line`` (positive
    when home is favored) and ``total_line``, or home and away implied team
    totals (``implied_home_total`` / ``implied_away_total``). Implied totals
    win. ``parse_game_line`` stores the home spread with the Odds sign
    (negative when home is favored). ``kickoff`` may be null. A row that
    still cannot form a spread and total is skipped.
    """
    if int(season) < 1 or int(week) < 1:
        raise GangstashDataError("gangstash closing_lines needs season and week")
    return fetch_dataset(
        dataset_id("closing_lines"),
        {"season": str(int(season)), "week": str(int(week))},
        refresh=refresh,
        cache_day=cache_day,
    )


def fetch_game_lines(
    *,
    on_date: date | None = None,
    season: int | None = None,
    week: int | None = None,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """`dataset=game_lines`. Send `date`, or `season` + `week`, not both."""
    if on_date is not None and week is None:
        params = {"date": on_date.isoformat()}
    elif season is not None and week is not None:
        params = {"season": str(int(season)), "week": str(int(week))}
    else:
        raise GangstashDataError("gangstash game_lines needs date or season+week")
    return fetch_dataset(
        dataset_id("game_lines"),
        params,
        refresh=refresh,
        cache_day=cache_day,
    )


@dataclass(frozen=True)
class GangstashDepthSlot:
    team_fd: str
    position: str
    rank: int
    player_name: str


def parse_depth_slot(row: dict) -> GangstashDepthSlot | None:
    """One base-offense skill row. Other `pos_grp` values are skipped.

    Live fields: team, team_fd, pos_grp, pos_abb, pos_name, pos_slot, pos_rank,
    player_name, gsis_id, espn_id, player_id, snapshot_at.
    `pos_rank` is the depth rank (WR2 = pos_abb WR, pos_rank 2). Rank does
    not reset inside an alignment. A null `player_name`, `team_fd`,
    `pos_abb`, or `pos_rank` returns None. `map_depth_slots` counts those
    rows and chokes only on an empty payload or missing columns.
    """
    if not isinstance(row, dict):
        return None
    grp = _str(row.get("pos_grp"))
    if grp != BASE_OFFENSE_POS_GRP:
        return None
    name = _str(row.get("player_name"))
    team_raw = _str(row.get("team_fd"))
    pos = _str(row.get("pos_abb")).upper()
    rank_raw = row.get("pos_rank")
    if not name or not team_raw or not pos or rank_raw is None or rank_raw == "":
        return None
    rank = _int(rank_raw)
    if rank is None or rank < 1:
        raise GangstashDataError(f"gangstash pos_rank {rank_raw!r} for {name}")
    team = require_fd(team_raw)
    return GangstashDepthSlot(
        team_fd=team.fd,
        position=pos,
        rank=rank,
        player_name=name,
    )


_DEPTH_COLUMNS = ("player_name", "team_fd", "pos_abb", "pos_rank")


def _depth_row_gap(row: dict) -> bool:
    """Null identity on one chart row (unmatched ESPN id), not a schema break."""
    if not _str(row.get("player_name")):
        return True
    if not _str(row.get("team_fd")):
        return True
    if not _str(row.get("pos_abb")):
        return True
    rank = row.get("pos_rank")
    return rank is None or rank == ""


def map_depth_slots(rows: list) -> list[GangstashDepthSlot]:
    """Base-offense slots. Null identity rows are skipped and counted once.

    Chokes when `rows` is empty or no row carries `player_name`, `team_fd`,
    `pos_abb`, and `pos_rank` (the columns are absent). A null value on an
    otherwise present column is a skipped row.
    """
    if not rows:
        raise GangstashDataError("gangstash depth_charts response is empty")
    if not _columns_present(rows, _DEPTH_COLUMNS):
        raise GangstashDataError(
            "gangstash depth_charts columns absent: "
            "player_name, team_fd, pos_abb, pos_rank"
        )
    skipped = 0
    out: list[GangstashDepthSlot] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _depth_row_gap(row):
            skipped += 1
            continue
        slot = parse_depth_slot(row)
        if slot is not None:
            out.append(slot)
    _warn_skipped(
        "depth_charts",
        skipped,
        "null player_name, team_fd, pos_abb, or pos_rank",
    )
    return out


def fetch_depth_charts(
    *,
    team: str | None = None,
    position: str | None = None,
    pos_grp: str | None = BASE_OFFENSE_POS_GRP,
    season: int | None = None,
    week: int | None = None,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """`dataset=depth_charts`. `position` is pos_abb. Default pos_grp is 3WR 1TE.

    ``season`` and ``week`` are sent when the caller has them. A chart with
    no ``week`` column is the current chart; the caller decides whether to
    use it.
    """
    params: dict[str, str] = {}
    if team:
        params["team"] = team.strip().upper()
    if position:
        params["position"] = position.strip().upper()
    if pos_grp:
        params["pos_grp"] = pos_grp.strip()
    if season is not None:
        params["season"] = str(int(season))
    if week is not None:
        params["week"] = str(int(week))
    return fetch_dataset(
        dataset_id("depth_charts"),
        params,
        refresh=refresh,
        cache_day=cache_day,
    )


def _side_param(side: str | None) -> str | None:
    if side is None or not str(side).strip():
        return None
    value = str(side).strip().lower()
    if value not in TEAM_STATS_SIDES:
        raise GangstashDataError(
            f"gangstash team_stats side must be offense or defense, got {side!r}"
        )
    return value


def fetch_team_stats(
    *,
    season: int,
    side: str | None = None,
    team: str | None = None,
    season_type: str = DEFAULT_SEASON_TYPE,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """Season team stats. Not an ILP input.

    `--sim` reads these rows for EPA variance and pass rate. `week1_score` does not.

    Sends `season_type=REG` unless overridden. Rows stay raw.
    """
    if int(season) < 1:
        raise GangstashDataError("gangstash team_stats requires season")
    params: dict[str, str] = {
        "season": str(int(season)),
        "season_type": (season_type or DEFAULT_SEASON_TYPE).strip() or DEFAULT_SEASON_TYPE,
    }
    side_value = _side_param(side)
    if side_value:
        params["side"] = side_value
    if team:
        params["team"] = team.strip().upper()
    return fetch_dataset(
        dataset_id("team_stats"),
        params,
        refresh=refresh,
        cache_day=cache_day,
    )


def fetch_week_injuries(
    *,
    season: int,
    week: int,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """`dataset=injuries` for one season and week. Rows stay raw."""
    if int(season) < 1 or int(week) < 1:
        raise GangstashDataError("gangstash injuries needs season and week")
    return fetch_dataset(
        dataset_id("injuries"),
        {"season": str(int(season)), "week": str(int(week))},
        refresh=refresh,
        cache_day=cache_day,
    )


def fetch_player_stats_weekly(
    *,
    season: int,
    week: int | None = None,
    weeks: list[int] | None = None,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """`dataset=player_stats_weekly`. `season` is required. `week` is optional.

    Same cache as the other `/data` readers (`fetch_dataset`, header
    `x-api-key` from `GANGSTASH_API_KEY`). Rows stay raw. `fd_points` is
    the backtest actual. `carries` / `rushing_attempts` feed rush share.
    """
    if int(season) < 1:
        raise GangstashDataError("gangstash player_stats_weekly requires season")
    week_list = list(weeks) if weeks else ([int(week)] if week is not None else [])
    params: dict[str, str] = {"season": str(int(season))}
    if week_list:
        params["week"] = ",".join(str(int(w)) for w in week_list)
    return fetch_dataset(
        dataset_id("player_stats_weekly"),
        params,
        refresh=refresh,
        cache_day=cache_day,
    )


def parse_player_stat_row(row: dict) -> dict | None:
    """One weekly box score. None when the name or team is blank.

    Keeps `fd_points` and a carry count when those columns exist. Does not
    raise on a missing score — the backtest skips unscored rows.
    """
    if not isinstance(row, dict):
        return None
    name = _str(row.get("player_name") or row.get("name") or row.get("player"))
    team_raw = _str(row.get("team_fd") or row.get("team") or row.get("recent_team"))
    if not name or not team_raw:
        return None
    try:
        team = require_fd(team_raw).fd
    except UnmappedTeam:
        return None
    carries = row.get("carries")
    if carries is None:
        carries = row.get("rushing_attempts")
    if carries is None:
        carries = row.get("rush_attempts")
    return {
        "player_name": name,
        "team_fd": team,
        "week": _int(row.get("week")),
        "season": _int(row.get("season")) or 0,
        "position": _str(row.get("position") or row.get("pos")).upper(),
        "fd_points": _float(row.get("fd_points")),
        "carries": _float(carries),
        "gsis_id": _str(row.get("gsis_id")) or None,
        "player_id": _str(row.get("player_id")) or None,
    }


def fetch_dst_weekly(
    *,
    season: int,
    week: int | None = None,
    weeks: list[int] | None = None,
    team: str | None = None,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """`dataset=dst_weekly`. `season` is required. `week` and `team` are optional.

    Team defense actuals. `fd_points` is the FanDuel DEF score. Rows stay raw.
    """
    if int(season) < 1:
        raise GangstashDataError("gangstash dst_weekly requires season")
    week_list = list(weeks) if weeks else ([int(week)] if week is not None else [])
    params: dict[str, str] = {"season": str(int(season))}
    if week_list:
        params["week"] = ",".join(str(int(w)) for w in week_list)
    if team:
        params["team"] = team.strip().upper()
    return fetch_dataset(
        dataset_id("dst_weekly"),
        params,
        refresh=refresh,
        cache_day=cache_day,
    )


def parse_dst_row(row: dict) -> dict | None:
    """One team-week of DEF actuals. None when it looks like a player row.

    Join key is ``(team_fd)`` after the season/week query. ``fd_points`` is
    required. A skill row with ``player_name`` is left to the player index.
    """
    if not isinstance(row, dict):
        return None
    if _str(row.get("player_name") or row.get("name") or row.get("player")):
        return None
    team_raw = _str(row.get("team_fd") or row.get("team"))
    fd_points = _float(row.get("fd_points"))
    if not team_raw or fd_points is None:
        return None
    try:
        team = require_fd(team_raw).fd
    except UnmappedTeam:
        return None
    return {
        "team_fd": team,
        "week": _int(row.get("week")),
        "season": _int(row.get("season")) or 0,
        "fd_points": fd_points,
    }


def fetch_team_stats_weekly(
    *,
    season: int,
    week: int | None = None,
    weeks: list[int] | None = None,
    team: str | None = None,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """Weekly team stats. Cached only — not an optimizer input.

    `week` is one week or a comma list (`weeks=[1, 2]`). Rows stay raw.
    """
    if int(season) < 1:
        raise GangstashDataError("gangstash team_stats_weekly requires season")
    week_list = list(weeks) if weeks else ([int(week)] if week is not None else [])
    if not week_list:
        raise GangstashDataError("gangstash team_stats_weekly requires week")
    params: dict[str, str] = {
        "season": str(int(season)),
        "week": ",".join(str(int(w)) for w in week_list),
    }
    if team:
        params["team"] = team.strip().upper()
    return fetch_dataset(
        dataset_id("team_stats_weekly"),
        params,
        refresh=refresh,
        cache_day=cache_day,
    )


def _parse_week_arg(raw: str | None) -> list[int] | None:
    if raw is None or not str(raw).strip():
        return None
    out: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out or None


def _print_fetch(kind: str, rows: list[dict], meta: dict) -> None:
    print(
        f"{kind} {len(rows)} rows  cache {meta.get('cache')}  "
        f"live {meta.get('live')}  stale {meta.get('cache_stale')}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """Fetch and cache team stats. Does not score a lineup."""
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ts = sub.add_parser("team-stats", help="cache dataset=team_stats")
    ts.add_argument("--season", type=int, required=True)
    ts.add_argument("--side", default=None, help="offense or defense")
    ts.add_argument("--team", default=None)
    ts.add_argument("--season-type", default=DEFAULT_SEASON_TYPE)
    ts.add_argument("--refresh", action="store_true")
    tw = sub.add_parser("team-stats-weekly", help="cache dataset=team_stats_weekly")
    tw.add_argument("--season", type=int, required=True)
    tw.add_argument("--week", default=None, help="one week or a comma list, e.g. 1,2")
    tw.add_argument("--team", default=None)
    tw.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "team-stats":
            rows, meta = fetch_team_stats(
                season=args.season,
                side=args.side,
                team=args.team,
                season_type=args.season_type,
                refresh=args.refresh,
            )
            _print_fetch("team_stats", rows, meta)
        else:
            week_list = _parse_week_arg(args.week)
            rows, meta = fetch_team_stats_weekly(
                season=args.season,
                weeks=week_list,
                team=args.team,
                refresh=args.refresh,
            )
            _print_fetch("team_stats_weekly", rows, meta)
    except GangstashDataKeyMissing as e:
        print(f"choke TEAM_STATS_GANGSTASH_KEY: {e}", file=sys.stderr)
        return 1
    except GangstashTruncated as e:
        print(f"choke TEAM_STATS_GANGSTASH: {e}", file=sys.stderr)
        return 1
    except GangstashDataError as e:
        print(f"choke TEAM_STATS_GANGSTASH: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
