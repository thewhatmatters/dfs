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
from nfl.teams import require_fd

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


def parse_target_row(row: dict) -> dict | None:
    """One player-week. None when position is not RB/WR/TE.

    Live fields: season, week, position, player_name, team_fd, targets,
    target_share, air_yards_share, wopr, receptions, rec_yards, team_targets,
    team_pass_attempts, gsis_id, player_id. No targets_total or targets_avg.
    """
    if not isinstance(row, dict):
        return None
    name = _str(row.get("player_name"))
    if not name:
        raise GangstashDataError(
            "gangstash targets row missing player_name "
            f"(keys {sorted(row)})"
        )
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
    want = set(weeks) if weeks else None
    by_week: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in rows:
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
    """Live fields: game_id, season, week, commence_time, home_team_fd,
    away_team_fd, spread (home line, negative = home favored), total,
    home_moneyline, away_moneyline, updated_at.
    """
    if not isinstance(row, dict):
        return None
    home_raw = _str(row.get("home_team_fd"))
    away_raw = _str(row.get("away_team_fd"))
    spread = _float(row.get("spread"))
    total = _float(row.get("total"))
    if not home_raw and not away_raw and spread is None and total is None:
        return None
    if not home_raw or not away_raw:
        raise GangstashDataError(
            "gangstash game_lines row missing home_team_fd/away_team_fd "
            f"(keys {sorted(row)})"
        )
    if spread is None or total is None:
        raise GangstashDataError(
            f"gangstash game_lines row {away_raw}@{home_raw} missing spread/total "
            f"(keys {sorted(row)})"
        )
    home = require_fd(home_raw)
    away = require_fd(away_raw)
    return GangstashGameLine(
        home_fd=home.fd,
        away_fd=away.fd,
        spread=spread,
        total=total,
        home_moneyline=_float(row.get("home_moneyline")),
        away_moneyline=_float(row.get("away_moneyline")),
        commence_time=_str(row.get("commence_time")) or None,
    )


def map_game_lines(rows: list[dict]) -> list[GangstashGameLine]:
    out: list[GangstashGameLine] = []
    for row in rows:
        parsed = parse_game_line(row)
        if parsed is not None:
            out.append(parsed)
    return out


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
    not reset inside an alignment.
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
    if not name and not team_raw and not pos and rank_raw is None:
        return None
    if not name or not team_raw or not pos or rank_raw is None:
        raise GangstashDataError(
            "gangstash depth_charts row missing player_name, team_fd, pos_abb, "
            f"or pos_rank (keys {sorted(row)})"
        )
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


def fetch_depth_charts(
    *,
    team: str | None = None,
    position: str | None = None,
    pos_grp: str | None = BASE_OFFENSE_POS_GRP,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """`dataset=depth_charts`. `position` is pos_abb. Default pos_grp is 3WR 1TE."""
    params: dict[str, str] = {}
    if team:
        params["team"] = team.strip().upper()
    if position:
        params["position"] = position.strip().upper()
    if pos_grp:
        params["pos_grp"] = pos_grp.strip()
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
    """Season team stats. Cached only — not an optimizer input.

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
