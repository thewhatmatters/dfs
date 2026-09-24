"""Gangstash `/data` datasets: targets, game lines, depth, team stats.

Field names live here so a live payload can be confirmed in one place.
Fetch/cache is `nfl.gangstash.fetch_dataset`. This module does not score.

RB snap share is not on these datasets. The RB usage blend still reads
Lineups snap share.

Do not send a Supabase service-role key. Auth is `GANGSTASH_API_KEY`
(`x-api-key`) inside `nfl.gangstash`.
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

# Canonical keys are first. Later names are aliases if the live payload differs.
# Confirm the canonical set once the endpoint is live — see nfl/docs/data/gangstash.md.


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
    """One player-week. None when the position is not RB/WR/TE.

    Assumed fields: player_name, team_fd, position, week, targets,
    target_share (0–1), team_targets, targets_total, targets_avg,
    gsis_id, player_id.
    """
    if not isinstance(row, dict):
        return None
    name = _str(_pick(row, "player_name", "name", "player"))
    if not name:
        raise GangstashDataError(
            "gangstash targets row missing player_name "
            f"(keys {sorted(row)}; confirm field names)"
        )
    team_raw = _str(_pick(row, "team_fd", "team"))
    if not team_raw:
        raise GangstashDataError(f"gangstash targets row missing team_fd for {name}")
    pos = _str(_pick(row, "position", "pos")).upper()
    if not pos:
        raise GangstashDataError(f"gangstash targets row missing position for {name}")
    if pos not in TARGET_POSITIONS:
        return None
    week = _int(_pick(row, "week"))
    if week is None or week < 1:
        raise GangstashDataError(f"gangstash targets row missing week for {name}")
    targets = _int(_pick(row, "targets"))
    if targets is None:
        raise GangstashDataError(f"gangstash targets row missing targets for {name}")
    team = require_fd(team_raw)
    return {
        "player_name": name,
        "team_fd": team.fd,
        "position": pos,
        "week": week,
        "targets": targets,
        "target_share": _float(_pick(row, "target_share", "targetShare")),
        "team_targets": _int(_pick(row, "team_targets", "teamTargets")),
        "targets_total": _int(_pick(row, "targets_total", "targetsTotal")),
        "targets_avg": _float(_pick(row, "targets_avg", "targetsAvg")),
        "gsis_id": _str(_pick(row, "gsis_id", "gsisId")) or None,
        "player_id": _str(_pick(row, "player_id", "playerId")) or None,
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
                f"{name} (confirm team_targets)"
            )
        else:
            raise GangstashDataError(
                f"gangstash targets team_targets sum is 0 for {name}"
            )
        latest = max(items, key=lambda i: int(i["week"]))
        n_weeks = len(items)
        totals = [i["targets_total"] for i in items if i["targets_total"] is not None]
        avg = latest["targets_avg"]
        if avg is None:
            avg = tgt_sum / n_weeks

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
                "targets_total": max(int(v) for v in totals) if totals else tgt_sum,
                "targets_avg": float(avg),
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


def _moneylines(row: dict) -> tuple[float | None, float | None]:
    block = row.get("moneylines")
    if block is None:
        block = row.get("moneyline")
    if isinstance(block, dict):
        return (
            _float(_pick(block, "home", "home_moneyline", "home_ml")),
            _float(_pick(block, "away", "away_moneyline", "away_ml")),
        )
    return (
        _float(_pick(row, "home_moneyline", "moneyline_home")),
        _float(_pick(row, "away_moneyline", "moneyline_away")),
    )


def parse_game_line(row: dict) -> GangstashGameLine | None:
    """Assumed fields: home_team_fd, away_team_fd, spread, total, moneylines, commence_time.

    `spread` is the home spread. `moneylines` is assumed `{home, away}`.
    Flat home_moneyline / away_moneyline are accepted when that object is absent.
    """
    if not isinstance(row, dict):
        return None
    home_raw = _str(_pick(row, "home_team_fd", "home_fd", "home"))
    away_raw = _str(_pick(row, "away_team_fd", "away_fd", "away"))
    spread = _float(_pick(row, "spread", "home_spread", "spread_home"))
    total = _float(_pick(row, "total", "game_total"))
    if not home_raw and not away_raw and spread is None and total is None:
        return None
    if not home_raw or not away_raw:
        raise GangstashDataError(
            "gangstash game_lines row missing home_team_fd/away_team_fd "
            f"(keys {sorted(row)}; confirm field names)"
        )
    if spread is None or total is None:
        raise GangstashDataError(
            f"gangstash game_lines row {away_raw}@{home_raw} missing spread/total "
            f"(keys {sorted(row)}; confirm field names)"
        )
    home = require_fd(home_raw)
    away = require_fd(away_raw)
    home_ml, away_ml = _moneylines(row)
    commence = _str(_pick(row, "commence_time", "commence", "kickoff")) or None
    return GangstashGameLine(
        home_fd=home.fd,
        away_fd=away.fd,
        spread=spread,
        total=total,
        home_moneyline=home_ml,
        away_moneyline=away_ml,
        commence_time=commence,
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
    """Assumed fields: player_name, team_fd, position, depth_rank (1 = starter)."""
    if not isinstance(row, dict):
        return None
    name = _str(_pick(row, "player_name", "name", "player"))
    team_raw = _str(_pick(row, "team_fd", "team"))
    pos = _str(_pick(row, "position", "pos"))
    rank_raw = _pick(row, "depth_rank", "rank", "depth_chart_rank")
    if not name and not team_raw and not pos and rank_raw is None:
        return None
    if not name or not team_raw or not pos or rank_raw is None:
        raise GangstashDataError(
            "gangstash depth_charts row missing player_name, team_fd, position, "
            f"or depth_rank (keys {sorted(row)}; confirm field names)"
        )
    rank = _int(rank_raw)
    if rank is None or rank < 1:
        raise GangstashDataError(f"gangstash depth_rank {rank_raw!r} for {name}")
    team = require_fd(team_raw)
    return GangstashDepthSlot(
        team_fd=team.fd,
        position=pos.upper(),
        rank=rank,
        player_name=name,
    )


def fetch_depth_charts(
    *,
    team: str | None = None,
    position: str | None = None,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """`dataset=depth_charts`. `team` and `position` are optional filters."""
    params: dict[str, str] = {}
    if team:
        params["team"] = team.strip().upper()
    if position:
        params["position"] = position.strip().upper()
    return fetch_dataset(
        dataset_id("depth_charts"),
        params,
        refresh=refresh,
        cache_day=cache_day,
    )


def fetch_team_stats(
    *,
    season: int,
    side: str | None = None,
    team: str | None = None,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """Season team stats. Cached only — not an optimizer input."""
    if int(season) < 1:
        raise GangstashDataError("gangstash team_stats requires season")
    params: dict[str, str] = {"season": str(int(season))}
    if side:
        params["side"] = side.strip()
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
    team: str | None = None,
    refresh: bool = False,
    cache_day: date | None = None,
) -> tuple[list[dict], dict]:
    """Weekly team stats. Cached only — not an optimizer input.

    This client always sends `season`. Confirm whether the live weekly
    dataset requires it.
    """
    if int(season) < 1:
        raise GangstashDataError("gangstash team_stats_weekly requires season")
    params: dict[str, str] = {"season": str(int(season))}
    if week is not None:
        params["week"] = str(int(week))
    if team:
        params["team"] = team.strip().upper()
    return fetch_dataset(
        dataset_id("team_stats_weekly"),
        params,
        refresh=refresh,
        cache_day=cache_day,
    )


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
    ts.add_argument("--side", default=None)
    ts.add_argument("--team", default=None)
    ts.add_argument("--refresh", action="store_true")
    tw = sub.add_parser("team-stats-weekly", help="cache dataset=team_stats_weekly")
    tw.add_argument("--season", type=int, required=True)
    tw.add_argument("--week", type=int, default=None)
    tw.add_argument("--team", default=None)
    tw.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "team-stats":
            rows, meta = fetch_team_stats(
                season=args.season,
                side=args.side,
                team=args.team,
                refresh=args.refresh,
            )
            _print_fetch("team_stats", rows, meta)
        else:
            rows, meta = fetch_team_stats_weekly(
                season=args.season,
                week=args.week,
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
