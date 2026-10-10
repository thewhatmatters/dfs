"""Publish gate for nightly NFL projections.

Before anything is posted, refuse when a collector run is stale or the
latest load is partial, failed, or a dead ``running`` process, or when
coverage of the target week is incomplete.

Freshness and load status come from ``public.collector_runs``
(``dataset=collector_runs``). A dataset's age is the latest
``finished_at`` among rows with ``status='succeeded'`` for its collector.
The latest load is the newest row for that collector by ``started_at``.
Row ``updated_at`` / ``as_of`` columns and the local cache file's mtime
are not freshness.

Coverage (who must appear) comes from the data rows. Expected teams are
the clubs on that week's games, so a bye is not required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from nfl.teams import canon_team

# Data Aggregator collector names. Edit this map if a collector is renamed.
COLLECTORS: dict[str, tuple[str, ...]] = {
    "lines": ("bettingpros-odds",),
    "props": ("bettingpros-pbcs",),
    "injuries": ("nflverse-injuries",),
    "depth": ("nflverse-depth-charts", "nflverse-depth-charts-weekly"),
}

# Week presence only. These feeds have no collector in COLLECTORS.
WEEK_DATASETS: tuple[str, ...] = (
    "player_stats_weekly",
    "player_usage",
    "snaps",
    "targets",
)

_GAME_TEAM_KEYS = ("home_team_fd", "away_team_fd", "home_team", "away_team")
_ROW_TEAM_KEYS = ("team_fd", "team", "recent_team")


@dataclass(frozen=True)
class GateLimits:
    """Age limits at publish time. Hours are a maximum age; running is minutes."""

    lines_hours: float = 26.0
    props_hours: float = 26.0
    injuries_hours: float = 26.0
    depth_hours: float = 72.0
    running_minutes: float = 30.0

    def hours_for(self, dataset: str) -> float:
        return {
            "lines": self.lines_hours,
            "props": self.props_hours,
            "injuries": self.injuries_hours,
            "depth": self.depth_hours,
        }[dataset]


@dataclass
class GateSnapshot:
    """Rows the gate judges. ``None`` means that load failed.

    ``week_rows`` is ``None`` on week 1 (no previous completed week).
    A value of ``None`` inside the dict means that dataset failed to load.
    """

    runs: list | None
    games: list | None
    injury_rows: list | None
    depth_rows: list | None
    week_rows: dict | None
    load_errors: list = field(default_factory=list)


@dataclass(frozen=True)
class GateResult:
    ok: bool
    failures: tuple[str, ...]
    header: tuple[str, ...]


def limits_from_args(args: object) -> GateLimits:
    defaults = GateLimits()
    return GateLimits(
        lines_hours=float(getattr(args, "lines_max_age_hours", defaults.lines_hours)),
        props_hours=float(getattr(args, "props_max_age_hours", defaults.props_hours)),
        injuries_hours=float(getattr(args, "injuries_max_age_hours", defaults.injuries_hours)),
        depth_hours=float(getattr(args, "depth_max_age_hours", defaults.depth_hours)),
        running_minutes=float(getattr(args, "running_max_minutes", defaults.running_minutes)),
    )


def gate_allows_stale(args: object) -> bool:
    """``--allow-stale`` and ``--skip-gate`` both publish after a failed check."""
    return bool(getattr(args, "allow_stale", False) or getattr(args, "skip_gate", False))


def gate_header_lines(result: GateResult, *, allow: bool) -> list[str]:
    """Report header: loud warnings when publishing past a failed check, then ages."""
    lines: list[str] = []
    if allow and result.failures:
        lines.extend(f"GATE WARNING: {line}" for line in result.failures)
    lines.extend(result.header)
    return lines


def parse_stamp(value: object) -> datetime | None:
    """UTC timestamp. Naive values are UTC. ``Z`` is accepted on Python 3.9."""
    if isinstance(value, datetime):
        dt = value
    else:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_limit(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:g}"


def _fmt_minutes(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return str(int(round(value)))
    return f"{value:.1f}"


def _canon(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return canon_team(text)


def _as_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_week(row: dict) -> int | None:
    if "week" not in row:
        return None
    return _as_int(row.get("week"))


def _in_week(row: dict, week: int) -> bool:
    """Keep rows stamped for ``week`` and rows the query already scoped."""
    stamped = _row_week(row)
    if stamped is None:
        return True
    return stamped == int(week)


def _team_from(row: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        team = _canon(row.get(key))
        if team:
            return team
    return ""


def scheduled_teams(games: list, week: int) -> tuple[str, ...]:
    """Clubs on this week's games. Bye clubs are absent, so they are not required."""
    found: set[str] = set()
    for row in games:
        if not isinstance(row, dict) or not _in_week(row, week):
            continue
        for key in _GAME_TEAM_KEYS:
            team = _canon(row.get(key))
            if team:
                found.add(team)
    return tuple(sorted(found))


def _lined_teams(games: list, week: int) -> set[str]:
    found: set[str] = set()
    for row in games:
        if not isinstance(row, dict) or not _in_week(row, week):
            continue
        spread = row.get("spread")
        total = row.get("total")
        if spread is None or spread == "" or total is None or total == "":
            continue
        for key in _GAME_TEAM_KEYS:
            team = _canon(row.get(key))
            if team:
                found.add(team)
    return found


def _listed_teams(rows: list, week: int) -> set[str]:
    found: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not _in_week(row, week):
            continue
        team = _team_from(row, _ROW_TEAM_KEYS)
        if team:
            found.add(team)
    return found


def _has_week(rows: list, week: int) -> bool:
    for row in rows:
        if isinstance(row, dict) and _row_week(row) == int(week):
            return True
    return False


def _group_runs(runs: list) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in runs:
        if not isinstance(row, dict):
            continue
        name = str(row.get("collector") or "").strip()
        if not name:
            continue
        grouped.setdefault(name, []).append(row)
    return grouped


def _latest_run(rows: list[dict]) -> dict | None:
    best: dict | None = None
    best_ts: datetime | None = None
    for row in rows:
        ts = parse_stamp(row.get("started_at"))
        if ts is None:
            continue
        if best_ts is None or ts > best_ts:
            best = row
            best_ts = ts
    return best


def _latest_succeeded_finished(rows: list[dict]) -> datetime | None:
    best: datetime | None = None
    for row in rows:
        if str(row.get("status") or "").strip() != "succeeded":
            continue
        ts = parse_stamp(row.get("finished_at"))
        if ts is None:
            continue
        if best is None or ts > best:
            best = ts
    return best


def _run_id(row: dict) -> str:
    raw = row.get("run_id")
    text = str(raw).strip() if raw is not None else ""
    return text or "?"


def _started_text(row: dict) -> str:
    raw = row.get("started_at")
    text = str(raw).strip() if raw is not None else ""
    return text or "unknown"


def evaluate_gate(
    snapshot: GateSnapshot,
    *,
    now: datetime,
    week: int,
    limits: GateLimits | None = None,
) -> GateResult:
    """One failure line per broken check. Header lists age and coverage either way.

    ``updated_at`` and ``as_of`` on a run row are ignored. Age is only
    ``finished_at`` of ``status='succeeded'``.
    """
    spec = limits or GateLimits()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    failures: list[str] = []
    header: list[str] = []
    failures.extend(str(item) for item in (snapshot.load_errors or []) if str(item).strip())

    if snapshot.runs is None:
        pass
    else:
        grouped = _group_runs(snapshot.runs)
        for dataset, collectors in COLLECTORS.items():
            limit_h = spec.hours_for(dataset)
            for collector in collectors:
                rows = grouped.get(collector, [])
                if not rows:
                    failures.append(f"{dataset} stale: no collector_runs for {collector}")
                    header.append(
                        f"{dataset} {collector} age unknown "
                        f"(limit {_fmt_limit(limit_h)}h) latest missing"
                    )
                    continue
                latest = _latest_run(rows)
                if latest is None:
                    failures.append(
                        f"{dataset} latest load missing started_at: {collector}"
                    )
                    status = "missing"
                else:
                    raw_status = latest.get("status") if "status" in latest else None
                    status = "" if raw_status is None else str(raw_status).strip()
                    rid = _run_id(latest)
                    started = _started_text(latest)
                    if not status:
                        failures.append(
                            f"{dataset} latest load status missing: "
                            f"{collector} run_id={rid} started_at={started}"
                        )
                        status = "missing"
                    elif status == "partial":
                        failures.append(
                            f"{dataset} latest load partial: "
                            f"{collector} run_id={rid} started_at={started}"
                        )
                    elif status == "failed":
                        failures.append(
                            f"{dataset} latest load failed: "
                            f"{collector} run_id={rid} started_at={started}"
                        )
                    elif status == "running":
                        started_at = parse_stamp(latest.get("started_at"))
                        if started_at is None:
                            failures.append(
                                f"{dataset} latest load running with no started_at: "
                                f"{collector} run_id={rid}"
                            )
                        else:
                            minutes = (now - started_at).total_seconds() / 60.0
                            if minutes > spec.running_minutes:
                                failures.append(
                                    f"{dataset} latest load running "
                                    f"{_fmt_minutes(minutes)}m "
                                    f"(> {_fmt_limit(spec.running_minutes)}m): "
                                    f"{collector} run_id={rid} started_at={started}"
                                )
                    elif status != "succeeded":
                        failures.append(
                            f"{dataset} latest load status {status!r} is not succeeded: "
                            f"{collector} run_id={rid} started_at={started}"
                        )
                finished = _latest_succeeded_finished(rows)
                if finished is None:
                    failures.append(
                        f"{dataset} stale: {collector} has no succeeded finished_at"
                    )
                    header.append(
                        f"{dataset} {collector} age unknown "
                        f"(limit {_fmt_limit(limit_h)}h) latest {status}"
                    )
                else:
                    age_h = (now - finished).total_seconds() / 3600.0
                    if age_h > limit_h:
                        failures.append(
                            f"{dataset} stale: {collector} last succeeded "
                            f"{finished.isoformat()} ({age_h:.1f}h > {_fmt_limit(limit_h)}h)"
                        )
                        header.append(
                            f"{dataset} {collector} age {age_h:.1f}h "
                            f"(limit {_fmt_limit(limit_h)}h) latest {status} stale"
                        )
                    else:
                        header.append(
                            f"{dataset} {collector} age {age_h:.1f}h "
                            f"(limit {_fmt_limit(limit_h)}h) latest {status}"
                        )

    _coverage(snapshot, week=int(week), failures=failures, header=header)
    _previous_week(snapshot, week=int(week), failures=failures, header=header)
    return GateResult(ok=not failures, failures=tuple(failures), header=tuple(header))


def _coverage(
    snapshot: GateSnapshot,
    *,
    week: int,
    failures: list[str],
    header: list[str],
) -> None:
    if snapshot.games is None:
        header.append("coverage lines unavailable")
        header.append("coverage injuries unavailable")
        header.append("coverage depth unavailable")
        return
    expected = scheduled_teams(snapshot.games, week)
    if not expected:
        failures.append(f"lines: no games for week {week}; cannot derive expected teams")
        header.append("coverage lines no schedule")
        header.append("coverage injuries no schedule")
        header.append("coverage depth no schedule")
        return
    _team_gap(
        "lines",
        expected,
        _lined_teams(snapshot.games, week),
        failures=failures,
        header=header,
    )
    if snapshot.injury_rows is None:
        header.append("coverage injuries unavailable")
    else:
        _team_gap(
            "injuries",
            expected,
            _listed_teams(snapshot.injury_rows, week),
            failures=failures,
            header=header,
        )
    if snapshot.depth_rows is None:
        header.append("coverage depth unavailable")
    else:
        _team_gap(
            "depth",
            expected,
            _listed_teams(snapshot.depth_rows, week),
            failures=failures,
            header=header,
        )


def _team_gap(
    label: str,
    expected: tuple[str, ...],
    present: set[str],
    *,
    failures: list[str],
    header: list[str],
) -> None:
    missing = [team for team in expected if team not in present]
    hit = len(expected) - len(missing)
    if missing:
        names = ", ".join(missing)
        failures.append(f"{label} missing {len(missing)} teams: {names}")
        header.append(f"coverage {label} {hit}/{len(expected)} teams missing {names}")
    else:
        header.append(f"coverage {label} {hit}/{len(expected)} teams")


def _previous_week(
    snapshot: GateSnapshot,
    *,
    week: int,
    failures: list[str],
    header: list[str],
) -> None:
    if int(week) <= 1:
        header.append("previous week n/a (week 1)")
        return
    prev = int(week) - 1
    rows_map = snapshot.week_rows or {}
    for name in WEEK_DATASETS:
        rows = rows_map.get(name) if snapshot.week_rows is not None else None
        if rows is None:
            header.append(f"previous week {prev} {name} missing")
            if snapshot.week_rows is not None and name in rows_map:
                continue
            failures.append(f"{name} missing previous completed week {prev}")
            continue
        if _has_week(rows, prev):
            header.append(f"previous week {prev} {name} present")
        else:
            failures.append(f"{name} missing previous completed week {prev}")
            header.append(f"previous week {prev} {name} missing")


def _call(label: str, fn):
    try:
        rows, _meta = fn()
    except Exception as exc:
        return None, f"{label} unavailable: {exc}"
    if rows is None:
        return [], None
    return list(rows), None


def _load_depth_rows(refresh: bool):
    """Current depth chart as ``(rows, meta)``, matching the other fetches."""
    from nfl.gangstash import GangstashDataError
    from nfl.gangstash_data import fetch_depth_charts

    try:
        rows, meta = fetch_depth_charts(pos_grp=None, refresh=refresh)
        return list(rows or []), meta
    except GangstashDataError:
        rows: list = []
        meta: dict = {"cache_stale": False}
        for pos in ("QB", "RB", "WR", "TE"):
            try:
                chunk, meta = fetch_depth_charts(
                    position=pos, pos_grp=None, refresh=refresh
                )
            except GangstashDataError:
                continue
            rows.extend(chunk or [])
        if not rows:
            raise
        return rows, meta


def load_gate_snapshot(season: int, week: int, *, refresh: bool) -> GateSnapshot:
    """Read collector runs and the coverage rows. Does not score or post.

    Cache file mtime is not consulted. ``fetch_*`` may return a same-day
    file; the gate still judges ``collector_runs.finished_at``.
    """
    from nfl.gangstash_data import (
        fetch_collector_runs,
        fetch_game_lines,
        fetch_player_stats_weekly,
        fetch_player_usage,
        fetch_snaps,
        fetch_targets,
        fetch_week_injuries,
    )

    errors: list[str] = []
    runs, err = _call("collector_runs", lambda: fetch_collector_runs(refresh=refresh))
    if err:
        errors.append(err)
    games, err = _call(
        "game_lines",
        lambda: fetch_game_lines(season=int(season), week=int(week), refresh=refresh),
    )
    if err:
        errors.append(err)
    injuries, err = _call(
        "injuries",
        lambda: fetch_week_injuries(season=int(season), week=int(week), refresh=refresh),
    )
    if err:
        errors.append(err)
    depth, err = _call("depth_charts", lambda: _load_depth_rows(refresh))
    if err:
        errors.append(err)

    week_rows: dict | None
    if int(week) <= 1:
        week_rows = None
    else:
        prev = int(week) - 1
        week_rows = {}
        fetchers = (
            (
                "player_stats_weekly",
                lambda: fetch_player_stats_weekly(
                    season=int(season), weeks=[prev], refresh=refresh
                ),
            ),
            (
                "player_usage",
                lambda: fetch_player_usage(season=int(season), weeks=[prev], refresh=refresh),
            ),
            (
                "snaps",
                lambda: fetch_snaps(season=int(season), weeks=[prev], refresh=refresh),
            ),
            (
                "targets",
                lambda: fetch_targets(season=int(season), weeks=[prev], refresh=refresh),
            ),
        )
        for name, fn in fetchers:
            rows, ferr = _call(name, fn)
            if ferr:
                errors.append(ferr)
                week_rows[name] = None
            else:
                week_rows[name] = rows
    return GateSnapshot(
        runs=runs,
        games=games,
        injury_rows=injuries,
        depth_rows=depth,
        week_rows=week_rows,
        load_errors=errors,
    )


def enforce_publish_gate(args: object, season: int, week: int, *, now: datetime | None = None) -> GateResult:
    """Load the snapshot and judge it. Call this before posting."""
    snapshot = load_gate_snapshot(int(season), int(week), refresh=bool(getattr(args, "refresh", True)))
    return evaluate_gate(
        snapshot,
        now=now or datetime.now(timezone.utc),
        week=int(week),
        limits=limits_from_args(args),
    )
