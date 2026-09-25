"""Publish nightly NFL projections to gangstash.

Board rows are `week1_score` for every skill player and DEF on the current
week's slate. Inputs are gangstash game lines, depth, targets, snaps, props,
and the week's injuries. A FanDuel players CSV is optional (salary,
FanDuel id, and an O/IR injury indicator).

    python3 -m nfl.publish_projections --refresh
    python3 -m nfl.publish_projections --dry-run

Write key: GANGSTASH_PROJECTIONS_WRITER_KEY, header `x-api-key` only.
Read key: GANGSTASH_API_KEY (existing /data and /props clients).

`--sim N` also posts model=sim. Draws and percentiles come from
`simulate_games` with the same gangstash `SimInputs` the optimizer
builds for `--projection-source sim`. `mean` is that simulated mean.
`--sim-efficiency` matches the optimizer (default `data`).
The run log prints the effective mode after any fallback, and each sim
row stores that mode on `inputs.sim_efficiency`. Missing sim inputs
fall back to placeholder and the board, and the log says so. The
nightly publish still posts the board.

`GANGSTASH_API_KEY` is required to read game lines, depth, targets,
snaps, props, and injuries. If it is unset the command exits 1 before posting.
There is no other read source. `--refresh` (the default) does not
fall back to a cache when the key is missing.
`--report` writes `nfl/reports/<season>-w<week>-<date>.md` after a
successful POST or `--dry-run` and prints that path.
Every successful run also writes
`nfl/reports/<season>-w<week>-<date>.manifest.json` (git sha, dirty
flag, seed, draws, as_of, CLI args, per-dataset freshness, input run
ids, Python version). `--as-of` defaults to the run start and keeps
the current reads. An explicit `--as-of` reads snapshot datasets and
writes the manifest, report, and games sidecar under an `asof-` name
so it does not replace the nightly files. A failed `collector_runs`
read warns and continues with empty `input_run_ids`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nfl import env as envmod
from nfl.gangstash import (
    FUNCTIONS_BASE,
    GangstashDataError,
    GangstashDataKeyMissing,
    GangstashError,
    GangstashKeyMissing,
    normalize_as_of,
)
from nfl.gangstash_data import (
    aggregate_snap_window,
    aggregate_target_window,
    consensus_game_lines,
    fetch_collector_runs,
    fetch_depth_charts,
    fetch_depth_charts_weekly,
    fetch_game_line_snapshots,
    fetch_game_lines,
    fetch_injury_snapshots,
    fetch_player_stats_weekly,
    fetch_player_usage,
    fetch_props_snapshots,
    fetch_snaps,
    fetch_targets,
    fetch_team_stats,
)
from nfl.http import HttpError, http_json_post
from nfl.injuries import apply_projection_injuries, is_pool_out
from nfl.lines import TeamLine, implied_totals
from nfl.names import match_key
from nfl.ourlads import skill_pos
from nfl.players import Player, load_fanduel_csv
from nfl.projections import (
    implied_core,
    prop_factor,
    usage_factor,
    week1_score,
)
from nfl.props import PropsError, PropsKeyMissing, attach_props, ingest_slate_props
from nfl.snaps import SnapWeekRow, attach_snaps
from nfl.targets import TargetWeekRow, attach_targets
from nfl.teams import UnmappedTeam, require_fd

PROJECTIONS_URL = FUNCTIONS_BASE + "/projections"
CHUNK_SIZE = 5000
OUT_DIR = Path(__file__).resolve().parent / "data" / "projections"
ET = ZoneInfo("America/New_York")
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")
# Same-rank tie for a player listed at two positions. Lower sorts first.
# A QB1 row is chosen before this order and is never replaced.
_DEPTH_POS_TIE = {"TE": 0, "RB": 1, "WR": 2, "QB": 3}
ROW_FIELDS = (
    "season",
    "week",
    "season_type",
    "run_at",
    "model",
    "model_version",
    "gsis_id",
    "player_id",
    "player_name",
    "team",
    "opponent",
    "position",
    "game_id",
    "salary",
    "mean",
    "p10",
    "p50",
    "p90",
    "inputs",
    "as_of",
    "input_run_ids",
)

# Latest succeeded collector_runs row for each command that feeds a publish.
# Names match dagg `runlog.Config.Collector` (the cmd directory).
FEED_COLLECTORS = (
    "bettingpros-odds",
    "bettingpros-pbcs",
    "nflverse-depth-charts",
    "nflverse-depth-charts-weekly",
    "nflverse-injuries",
    "nflverse-player-stats",
    "nflverse-player-usage",
    "nflverse-snaps",
    "nflverse-targets",
    "nflverse-team-stats",
)
MAX_INPUT_RUN_IDS = 100
STALE_HOURS = 36
# collector_runs is a log, not a scored board. A bounded window keeps the
# nightly read off the 20,000-row cap. Provenance must not fail the post.
COLLECTOR_LOOKBACK_HOURS = 48
# These datasets reject as_of. An explicit --as-of run still reads them live.
CURRENT_READS = (
    "targets",
    "snaps",
    "team_stats",
    "player_usage",
    "player_stats_weekly",
)
NO_PIT_INJURIES = "no point-in-time injuries at as_of; injuries NOT applied"


class PublishError(Exception):
    """Projections publish failed."""


class ProjectionsKeyMissing(PublishError):
    """No GANGSTASH_PROJECTIONS_WRITER_KEY."""


class StaleInputs(PublishError):
    """Game lines or depth are missing or from a stale cache."""


@dataclass
class DatasetRecord:
    """Rows that were scored, plus the server freshness flags."""

    rows: list
    observed_at: str | None = None
    untimestamped: bool = False


class RunCapture:
    """Provenance for one publish. ``datasets`` is keyed by dataset name."""

    def __init__(self) -> None:
        self.datasets = {}
        self.line_rows = []
        self.input_run_ids = []
        self.point_in_time = False
        self.notes = []


@dataclass(frozen=True)
class SimResult:
    """Player draws, the efficiency mode that ran, and sim team scores.

    ``by_pid, mode = maybe_sim(...)`` still unpacks. ``game_draws`` is
    ``(game, away, home, away_pts, home_pts)`` per game per draw.
    """

    by_pid: dict | None
    efficiency: str
    game_draws: tuple = ()

    def __iter__(self):
        yield self.by_pid
        yield self.efficiency


@dataclass(frozen=True)
class PublishEntry:
    player: Player
    gsis_id: str | None
    player_id: str | None
    game_id: str | None
    fanduel_id: str | None
    salary: int | None


def projections_key() -> str:
    k = envmod.get("GANGSTASH_PROJECTIONS_WRITER_KEY")
    if not k:
        raise ProjectionsKeyMissing("GANGSTASH_PROJECTIONS_WRITER_KEY is not set")
    return k


def model_version() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out or "unknown"


def chunk_rows(rows: list[dict], size: int = CHUNK_SIZE) -> list[list[dict]]:
    n = max(1, int(size))
    return [rows[i : i + n] for i in range(0, len(rows), n)]


def _redact(text: str, key: str) -> str:
    if key and key in text:
        return text.replace(key, "***")
    return text


def _counts(payload: object) -> tuple[int, int]:
    if not isinstance(payload, dict):
        raise PublishError("projections response is not an object")
    if "inserted" not in payload or "updated" not in payload:
        raise PublishError(
            "projections response missing inserted/updated "
            f"(keys {sorted(payload)})"
        )
    return int(payload["inserted"]), int(payload["updated"])


def post_projection_rows(
    rows: list[dict],
    *,
    key: str,
    poster=None,
) -> tuple[int, int]:
    """POST `{"rows": ...}` in chunks of 5000. Key stays in the header."""
    if not key:
        raise ProjectionsKeyMissing("GANGSTASH_PROJECTIONS_WRITER_KEY is not set")
    send = poster or _post_chunk
    inserted = 0
    updated = 0
    for chunk in chunk_rows(rows):
        try:
            payload = send(PROJECTIONS_URL, chunk, key)
        except PublishError:
            raise
        except (HttpError, GangstashError) as e:
            raise PublishError(_redact(str(e), key)) from e
        except Exception as e:
            raise PublishError(_redact(str(e), key)) from e
        ins, upd = _counts(payload)
        inserted += ins
        updated += upd
    return inserted, updated


def _post_chunk(url: str, chunk: list[dict], key: str) -> dict:
    if "?" in url or key in url or "api_key=" in url or "apikey=" in url.lower():
        raise PublishError("refusing to put the projections key in the URL")
    payload, _hdrs = http_json_post(url, {"rows": chunk}, headers={"x-api-key": key})
    if not isinstance(payload, dict):
        raise PublishError("projections response is not an object")
    return payload


def load_simulate_games():
    """Return simulate_games, or None when the sim module is unavailable."""
    try:
        import nfl.sim as sim
    except ImportError:
        return None
    fn = getattr(sim, "simulate_games", None)
    return fn if callable(fn) else None


def missing_read_key_message(detail: str) -> str:
    """Why publish stopped when the gangstash read key is missing.

    The nightly job has no second data source. ``--refresh`` (the default)
    does not read a cache when the key is unset.
    """
    text = (detail or "GANGSTASH_API_KEY is not set").strip()
    if "GANGSTASH_API_KEY" not in text:
        text = f"GANGSTASH_API_KEY is not set ({text})"
    return (
        f"{text}. publish_projections reads game lines, depth, targets, "
        "snaps, props, and injuries from gangstash and exits without posting. "
        "Set GANGSTASH_API_KEY. The default --refresh does not switch to "
        "another data source or a cache."
    )


def maybe_sim(
    entries: list[PublishEntry],
    n: int,
    seed: int,
    *,
    season: int,
    refresh: bool,
    week: int | None = None,
    sim_efficiency: str = "data",
) -> SimResult:
    """``(pid → SimStats, effective efficiency mode)``, plus game draws.

    Same call as the optimizer's `--projection-source sim` path:
    `resolve_sim_inputs` then `simulate_games(..., inputs=, efficiency=)`.
    `before_week` drops the slate week and later, so a nightly publish
    uses completed weeks only. `mean` on the published row is that draw's
    mean (the ILP mean when projection source is sim). p10/p50/p90 are
    the same draws. The mode is the one actually used, including a
    placeholder fallback when sim inputs are missing.
    """
    requested = (sim_efficiency or "data").strip().lower()
    if n <= 0:
        return SimResult(None, requested)
    fn = load_simulate_games()
    if fn is None:
        print(
            "sim unavailable (nfl.sim.simulate_games); publishing board only",
            file=sys.stderr,
        )
        return SimResult(None, requested)
    try:
        from nfl.sim_feed import resolve_sim_inputs, sim_pool
        from nfl.sim_inputs import SimInputError
    except ImportError:
        print(
            "sim inputs unavailable; publishing board only",
            file=sys.stderr,
        )
        return SimResult(None, requested)
    try:
        sim_inputs, note = resolve_sim_inputs(
            path=None,
            season=int(season),
            weeks=None,
            refresh_targets=bool(refresh),
            refresh_snaps=bool(refresh),
        )
    except SimInputError as e:
        raise PublishError(str(e)) from e
    print(note, file=sys.stderr)
    if "stale cache" in note:
        raise StaleInputs("sim inputs cache is stale")
    from nfl.sim_efficiency import resolve_run_efficiency

    efficiency, used, efficiency_note = resolve_run_efficiency(
        sim_efficiency,
        sim_inputs,
        before_week=week,
    )
    if efficiency_note:
        print(efficiency_note, file=sys.stderr)
    if sim_inputs is None:
        print(
            "sim inputs missing; sim fell back to board; publishing board only",
            file=sys.stderr,
        )
        return SimResult(None, used)
    try:
        result = fn(
            sim_pool([e.player for e in entries]),
            n=int(n),
            seed=int(seed),
            inputs=sim_inputs,
            efficiency=efficiency,
        )
    except Exception as e:
        print(
            f"sim failed ({e}); sim fell back to board; publishing board only",
            file=sys.stderr,
        )
        return SimResult(None, used)
    by_pid = getattr(result, "by_pid", None)
    if not isinstance(by_pid, dict):
        print(
            "sim fell back to board; publishing board only",
            file=sys.stderr,
        )
        return SimResult(None, used)
    raw_draws = getattr(result, "game_draws", ()) or ()
    return SimResult(by_pid, used, tuple(raw_draws))


def sim_parts(sim: SimResult | tuple | dict | None) -> tuple[dict | None, str, tuple]:
    """``(by_pid, efficiency, game_draws)`` from a ``SimResult`` or a 2-tuple."""
    if isinstance(sim, SimResult):
        return sim.by_pid, sim.efficiency, tuple(sim.game_draws or ())
    if isinstance(sim, tuple) and len(sim) >= 2:
        draws = sim[2] if len(sim) > 2 else ()
        return sim[0], str(sim[1]), tuple(draws or ())
    if isinstance(sim, dict):
        return sim, "data", ()
    if sim is None:
        return None, "data", ()
    by_pid = getattr(sim, "by_pid", None)
    mode = getattr(sim, "efficiency", None) or "data"
    draws = getattr(sim, "game_draws", ()) or ()
    if not isinstance(by_pid, dict) and by_pid is not None:
        return None, str(mode), ()
    return by_pid, str(mode), tuple(draws)


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _remember(
    capture: RunCapture,
    name: str,
    rows: list,
    meta: dict | None,
    *,
    observed_at: str | None = None,
    normalized: list | None = None,
    observed_set: bool = False,
) -> None:
    """Keep the rows that were scored and the server freshness flags.

    ``observed_set`` uses ``observed_at`` even when it is None, so a
    baseline-only injury snapshot does not claim the seed time.
    """
    server = {}
    if isinstance(meta, dict):
        raw = meta.get("response_meta")
        if isinstance(raw, dict):
            server = raw
    used = list(rows if normalized is None else normalized)
    if observed_set:
        obs = observed_at
    else:
        obs = observed_at if observed_at is not None else server.get("observed_at")
    if obs is not None:
        obs = str(obs)
    flag = bool(server.get("untimestamped"))
    prev = capture.datasets.get(name)
    if prev is None:
        capture.datasets[name] = DatasetRecord(
            rows=used,
            observed_at=obs,
            untimestamped=flag,
        )
        return
    prev.rows.extend(used)
    prev.untimestamped = bool(prev.untimestamped or flag)
    if obs:
        previous = _parse_utc(prev.observed_at)
        current = _parse_utc(obs)
        if previous is None or (current is not None and current > previous):
            prev.observed_at = obs


def canonical_sha256(rows: list) -> str:
    """sha256 of rows as canonical JSON (sorted keys, sorted rows)."""
    encoded = [
        json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
        for row in rows
        if isinstance(row, dict)
    ]
    encoded.sort()
    payload = "[" + ",".join(encoded) + "]"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def dataset_manifest(capture: RunCapture, *, as_of: str | None = None) -> dict:
    cutoff = _parse_utc(as_of) if capture.point_in_time else None
    out = {}
    for name in sorted(capture.datasets):
        record = capture.datasets[name]
        item = {
            "row_count": len(record.rows),
            "observed_at": record.observed_at,
            "sha256": canonical_sha256(record.rows),
            "untimestamped": bool(record.untimestamped),
        }
        if capture.point_in_time:
            item["point_in_time"] = name not in CURRENT_READS
            observed = _parse_utc(record.observed_at)
            item["after_as_of"] = bool(cutoff is not None and observed is not None and observed > cutoff)
        out[name] = item
    return out


def freshness_warnings(
    capture: RunCapture,
    *,
    now: datetime,
    as_of: datetime | None = None,
) -> list[str]:
    """Datasets older than 36h, legacy untimestamped rows, or newer than ``as_of``.

    When ``as_of`` is set, 36h is measured from that time. Otherwise it is
    measured from ``now`` (the run start).
    """
    moment = as_of if as_of is not None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    lines: list[str] = []
    for name in sorted(capture.datasets):
        record = capture.datasets[name]
        if record.untimestamped:
            lines.append(f"{name} untimestamped")
        observed = _parse_utc(record.observed_at)
        if observed is None:
            continue
        if as_of is not None and observed > moment:
            lines.append(f"{name} observed_at {record.observed_at} is after as_of")
        age = moment - observed
        if age > timedelta(hours=STALE_HOURS):
            lines.append(f"{name} observed_at {record.observed_at} is older than 36h")
    return lines


def provenance_warnings(
    capture: RunCapture,
    *,
    now: datetime,
    as_of: datetime | None = None,
) -> list[str]:
    """Stderr, report footer, and manifest warnings for one publish."""
    lines = [str(note) for note in capture.notes]
    if capture.point_in_time:
        injury = capture.datasets.get("injury_snapshots")
        if injury is not None and len(injury.rows) == 0 and NO_PIT_INJURIES not in lines:
            lines.append(NO_PIT_INJURIES)
        current = [name for name in CURRENT_READS if name in capture.datasets]
        if current:
            lines.append("current reads (not point-in-time): " + ", ".join(current))
    lines.extend(
        freshness_warnings(
            capture,
            now=now,
            as_of=as_of if capture.point_in_time else None,
        )
    )
    return lines


def select_input_run_ids(rows: list, *, as_of: datetime) -> list[str]:
    """Latest succeeded run at or before ``as_of`` for each feed collector.

    ``finished_at`` is the cutoff: a run that had not finished was not
    knowable. At most ``MAX_INPUT_RUN_IDS`` ids, in collector-name order.
    """
    cutoff = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    cutoff = cutoff.astimezone(timezone.utc)
    wanted = set(FEED_COLLECTORS)
    best: dict[str, tuple] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") != "succeeded":
            continue
        collector = str(row.get("collector") or "")
        if collector not in wanted:
            continue
        finished = _parse_utc(row.get("finished_at"))
        if finished is None or finished > cutoff:
            continue
        started = _parse_utc(row.get("started_at")) or finished
        run_id = str(row.get("run_id") or "").strip().lower()
        if not run_id:
            continue
        rank = (finished, started, run_id)
        prev = best.get(collector)
        if prev is None or rank > prev[0]:
            best[collector] = (rank, run_id)
    ids = [best[name][1] for name in FEED_COLLECTORS if name in best]
    return ids[:MAX_INPUT_RUN_IDS]


def git_state() -> tuple[str, bool]:
    """Full HEAD sha and whether the worktree is dirty."""
    root = Path(__file__).resolve().parent.parent
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        sha = "unknown"
    try:
        dirty_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        dirty = bool(dirty_out.strip())
    except (OSError, subprocess.CalledProcessError):
        dirty = False
    return sha or "unknown", dirty


def build_manifest(
    capture: RunCapture,
    *,
    seed: int,
    draws: int,
    as_of: str,
    cli_args: list,
    warnings: list | None = None,
) -> dict:
    sha, dirty = git_state()
    current = [name for name in CURRENT_READS if name in capture.datasets] if capture.point_in_time else []
    return {
        "git_sha": sha,
        "dirty": dirty,
        "seed": int(seed),
        "draws": int(draws),
        "as_of": as_of,
        "cli_args": list(cli_args),
        "datasets": dataset_manifest(capture, as_of=as_of),
        "input_run_ids": list(capture.input_run_ids),
        "python": platform.python_version(),
        "point_in_time": bool(capture.point_in_time),
        "current_reads": current,
        "warnings": list(capture.notes if warnings is None else warnings),
    }


def asof_file_stamp(as_of: str) -> str:
    """Compact UTC stamp for an explicit ``--as-of`` artifact name."""
    stamp = _parse_utc(as_of)
    if stamp is None:
        return "unknown"
    return stamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")


def publish_artifact_names(
    season: int,
    week: int,
    run_at: str,
    as_of: str | None = None,
) -> dict:
    """Report filenames. An explicit ``as_of`` does not reuse the nightly names."""
    if as_of:
        stem = f"{int(season)}-w{int(week)}-asof-{asof_file_stamp(as_of)}"
        return {
            "manifest": f"{stem}.manifest.json",
            "report": f"{stem}.md",
            "sidecar": f"{stem}-games.json",
        }
    from nfl.report import report_day

    day = report_day(run_at)
    return {
        "manifest": f"{int(season)}-w{int(week)}-{day}.manifest.json",
        "report": f"{int(season)}-w{int(week)}-{day}.md",
        "sidecar": f"{int(season)}-w{int(week)}-games.json",
    }


def manifest_path(
    season: int,
    week: int,
    run_at: str,
    root: Path | None = None,
    filename: str | None = None,
) -> Path:
    from nfl.report import REPORTS_DIR

    dest = root or REPORTS_DIR
    name = filename or publish_artifact_names(season, week, run_at)["manifest"]
    return dest / name


def write_manifest(
    payload: dict,
    *,
    season: int,
    week: int,
    run_at: str,
    dest: Path | None = None,
    filename: str | None = None,
) -> Path:
    path = manifest_path(season, week, run_at, dest, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _stale(meta: dict, label: str) -> None:
    if meta.get("cache_stale"):
        raise StaleInputs(f"{label} cache is stale")


def _week_from_rows(rows: list[dict]) -> tuple[int, int]:
    seasons = {int(r["season"]) for r in rows if r.get("season") is not None}
    weeks = {int(r["week"]) for r in rows if r.get("week") is not None}
    if len(seasons) != 1 or len(weeks) != 1:
        raise StaleInputs(
            f"game_lines season/week not unique (season={sorted(seasons)} week={sorted(weeks)})"
        )
    return next(iter(seasons)), next(iter(weeks))


def resolve_nfl_week(
    today: date,
    *,
    refresh: bool,
    season: int | None = None,
    week: int | None = None,
) -> tuple[int, int, list[dict]]:
    """Current NFL week from gangstash game_lines, then the full week."""
    if (season is None) ^ (week is None):
        raise PublishError("pass both --season and --week")
    if season is None:
        anchor: list[dict] | None = None
        deltas = list(range(0, 8)) + list(range(-1, -7, -1))
        for delta in deltas:
            day = today + timedelta(days=delta)
            rows, meta = fetch_game_lines(on_date=day, refresh=refresh)
            if meta.get("cache_stale") or not rows:
                continue
            anchor = rows
            break
        if not anchor:
            raise StaleInputs(f"no game_lines near {today.isoformat()}")
        season, week = _week_from_rows(anchor)
    assert season is not None and week is not None
    rows, meta = fetch_game_lines(season=int(season), week=int(week), refresh=refresh)
    _stale(meta, f"game_lines {season} week {week}")
    if not rows:
        raise StaleInputs(f"no game_lines for {season} week {week}")
    got_season, got_week = _week_from_rows(rows)
    if got_season != int(season) or got_week != int(week):
        raise StaleInputs(
            f"game_lines returned {got_season} week {got_week}, expected {season} week {week}"
        )
    return int(season), int(week), rows


def _kickoff_et_day(row: dict) -> date | None:
    stamp = _parse_utc(row.get("commence_time") or row.get("kickoff_at"))
    if stamp is None:
        return None
    return stamp.astimezone(ET).date()


def _season_guess(day: date) -> int:
    """NFL season year. January and February belong to the previous season."""
    return day.year if day.month >= 3 else day.year - 1


def resolve_nfl_week_as_of(
    today: date,
    *,
    refresh: bool,
    as_of: str,
    season: int | None = None,
    week: int | None = None,
) -> tuple[int, int, list[dict], dict]:
    """Week from ``game_line_snapshots`` at ``as_of``, collapsed to consensus lines."""
    if (season is None) ^ (week is None):
        raise PublishError("pass both --season and --week")
    if season is not None and week is not None:
        rows, meta = fetch_game_line_snapshots(
            season=int(season),
            week=int(week),
            refresh=refresh,
            as_of=as_of,
        )
        _stale(meta, f"game_line_snapshots {season} week {week}")
        lines = consensus_game_lines(rows)
        if not lines:
            raise StaleInputs(f"no game_line_snapshots for {season} week {week} at {as_of}")
        got_season, got_week = _week_from_rows(lines)
        if got_season != int(season) or got_week != int(week):
            raise StaleInputs(
                f"game_line_snapshots returned {got_season} week {got_week}, "
                f"expected {season} week {week}"
            )
        return int(season), int(week), lines, meta

    guesses = [_season_guess(today)]
    other = today.year if guesses[0] != today.year else today.year - 1
    if other not in guesses:
        guesses.append(other)
    last_meta: dict = {}
    for guess in guesses:
        rows, meta = fetch_game_line_snapshots(season=int(guess), refresh=refresh, as_of=as_of)
        last_meta = meta
        if meta.get("cache_stale"):
            continue
        lines = consensus_game_lines(rows)
        deltas = list(range(0, 8)) + list(range(-1, -7, -1))
        for delta in deltas:
            day = today + timedelta(days=delta)
            anchor = [row for row in lines if _kickoff_et_day(row) == day]
            if not anchor:
                continue
            found_season, found_week = _week_from_rows(anchor)
            week_lines = [
                row
                for row in lines
                if int(row.get("season") or 0) == found_season
                and int(row.get("week") or 0) == found_week
            ]
            if not week_lines:
                continue
            return found_season, found_week, week_lines, meta
    if last_meta.get("cache_stale"):
        raise StaleInputs("game_line_snapshots cache is stale")
    raise StaleInputs(f"no game_line_snapshots near {today.isoformat()} at {as_of}")


def _team_lines(rows: list[dict]) -> dict[str, tuple[TeamLine, str | None]]:
    out: dict[str, tuple[TeamLine, str | None]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        home = require_fd(str(row.get("home_team_fd") or "")).fd
        away = require_fd(str(row.get("away_team_fd") or "")).fd
        spread = row.get("spread")
        total = row.get("total")
        if spread is None or total is None:
            raise StaleInputs(f"game_lines {away}@{home} missing spread or total")
        implied_home, implied_away = implied_totals(float(total), float(spread))
        game = f"{away}@{home}"
        game_id = row.get("game_id")
        gid = None if game_id is None or game_id == "" else str(game_id)
        line = TeamLine(
            game=game,
            home_fd=home,
            away_fd=away,
            home_spread=float(spread),
            total=float(total),
            implied_home=implied_home,
            implied_away=implied_away,
            home_moneyline=_opt_float(row.get("home_moneyline")),
            away_moneyline=_opt_float(row.get("away_moneyline")),
            provider="bettingpros",
            source="gangstash",
            commence_time=(str(row.get("commence_time")) if row.get("commence_time") else None),
        )
        out[home] = (line, gid)
        out[away] = (line, gid)
    return out


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _id_index(rows: list[dict]) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    out: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _opt_str(row.get("player_name"))
        team_raw = _opt_str(row.get("team_fd"))
        if not name or not team_raw:
            continue
        try:
            team = require_fd(team_raw).fd
        except UnmappedTeam:
            continue
        gsis = _opt_str(row.get("gsis_id"))
        pid = _opt_str(row.get("player_id"))
        if not gsis and not pid:
            continue
        key = (team, match_key(name))
        prev = out.get(key)
        if prev is None or (not prev[0] and gsis):
            out[key] = (gsis or (prev[0] if prev else None), pid or (prev[1] if prev else None))
    return out


def _merge_ids(*indexes: dict[tuple[str, str], tuple[str | None, str | None]]):
    merged: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for idx in indexes:
        for key, (gsis, pid) in idx.items():
            prev = merged.get(key)
            if prev is None:
                merged[key] = (gsis, pid)
                continue
            merged[key] = (prev[0] or gsis, prev[1] or pid)
    return merged


def _depth_row_identity(
    item: dict,
    ids: dict[tuple[str, str], tuple[str | None, str | None]],
) -> tuple[str | None, str | None, str | None]:
    """``(gsis, player_id, shared)``. ``shared`` is the id that would collide."""
    team = item["team"]
    gsis, pid = ids.get((team, match_key(item["name"])), (None, None))
    row = item["row"]
    gsis = gsis or _opt_str(row.get("gsis_id"))
    pid = pid or _opt_str(row.get("player_id"))
    return gsis, pid, gsis or pid


def _fanduel_skill_positions(
    players: list[Player] | None,
) -> dict[tuple[str, str], str]:
    """``(team, match_key) → FanDuel skill position``. The first skill row wins."""
    out: dict[tuple[str, str], str] = {}
    for pl in players or []:
        try:
            team = require_fd(pl.team).fd
        except UnmappedTeam:
            continue
        pos = (pl.position or "").upper()
        if pos not in SKILL_POSITIONS:
            continue
        key = (team, match_key(pl.name))
        if key not in out:
            out[key] = pos
    return out


def _choose_depth_item(group: list[dict], fd_pos: str | None) -> dict:
    """One chart row for a player listed at more than one position.

    A QB1 row is kept even when the FanDuel CSV or a same-rank tie would
    pick something else. Otherwise the CSV position wins when that
    position is on the chart. With no CSV hit, the lower depth rank wins,
    and equal ranks break TE > RB > WR > QB.
    """
    qb1 = [item for item in group if item["pos"] == "QB" and item["rank"] == 1]
    if qb1:
        return qb1[0]
    if fd_pos:
        matched = [item for item in group if item["pos"] == fd_pos]
        if matched:
            return matched[0]
    return min(
        group,
        key=lambda item: (
            item["rank"],
            _DEPTH_POS_TIE.get(item["pos"], 9),
            item["name"],
        ),
    )


def _collapse_depth_best(
    items: list[dict],
    slate: dict[str, tuple[TeamLine, str | None]],
    ids: dict[tuple[str, str], tuple[str | None, str | None]],
    csv_players: list[Player] | None,
) -> list[dict]:
    """Drop extra positions for one player id in one game.

    The kept row is the only usage profile. Target and snap shares are
    joined later, once, onto that player. They are not summed across the
    dropped position.
    """
    fd = _fanduel_skill_positions(csv_players)
    groups: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for item in items:
        _gsis, _pid, shared = _depth_row_identity(item, ids)
        if not shared:
            continue
        game = slate[item["team"]][0].game
        key = (game, shared)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    drop: set[int] = set()
    for key in order:
        group = groups[key]
        if len(group) < 2:
            continue
        fd_pos = None
        for item in group:
            fd_pos = fd.get((item["team"], match_key(item["name"])))
            if fd_pos:
                break
        picked = _choose_depth_item(group, fd_pos)
        positions = ",".join(item["pos"] for item in group)
        print(
            f"depth collapse {key[1]} positions={positions} chose={picked['pos']}",
            file=sys.stderr,
        )
        for item in group:
            if item is not picked:
                drop.add(id(item))
    return [item for item in items if id(item) not in drop]


def _depth_players(
    rows: list[dict],
    slate: dict[str, tuple[TeamLine, str | None]],
    ids: dict[tuple[str, str], tuple[str | None, str | None]],
    csv_players: list[Player] | None = None,
) -> list[tuple[Player, str | None, str | None, str | None]]:
    best: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _opt_str(row.get("player_name"))
        team_raw = _opt_str(row.get("team_fd"))
        pos = skill_pos(str(row.get("pos_abb") or ""))
        rank_raw = row.get("pos_rank")
        if not name or not team_raw or pos not in SKILL_POSITIONS:
            continue
        if rank_raw is None or rank_raw == "":
            continue
        try:
            team = require_fd(team_raw).fd
            rank = int(rank_raw)
        except (UnmappedTeam, TypeError, ValueError):
            continue
        if team not in slate or rank < 1:
            continue
        key = (team, match_key(name), pos)
        prev = best.get(key)
        if prev is None or rank < prev["rank"]:
            best[key] = {"name": name, "team": team, "pos": pos, "rank": rank, "row": row}
    chosen = _collapse_depth_best(list(best.values()), slate, ids, csv_players)
    built: list[tuple[Player, str | None, str | None, str | None]] = []
    for item in chosen:
        team = item["team"]
        line, game_id = slate[team]
        opponent = line.away_fd if team == line.home_fd else line.home_fd
        gsis, pid = ids.get((team, match_key(item["name"])), (None, None))
        row = item["row"]
        gsis = gsis or _opt_str(row.get("gsis_id"))
        pid = pid or _opt_str(row.get("player_id"))
        ident = gsis or f"pub:{team}:{item['pos']}:{match_key(item['name'])}"
        implied = line.implied_for(team)
        opp_implied = line.implied_for(opponent)
        pl = Player(
            pid=ident,
            name=item["name"],
            position=item["pos"],
            salary=0,
            team=team,
            opponent=opponent,
            game=line.game,
            fppg=None,
            injury="",
            roster_position=item["pos"],
            spread=line.spread_for(team),
            total=line.total,
            implied_total=implied,
            implied_opp=opp_implied,
            moneyline=line.moneyline_for(team),
            lines_provider=line.provider,
            lines_source=line.source,
            depth_rank=item["rank"],
            depth_source="gangstash",
            objective=week1_score(
                implied,
                depth_rank=item["rank"],
                position=item["pos"],
                implied_opp=opp_implied,
            ),
        )
        built.append((pl, gsis, pid, game_id))
    missing = sorted(set(slate) - {p.team for p, *_rest in built})
    if missing:
        raise StaleInputs("no skill depth for " + ", ".join(missing))
    if not built:
        raise StaleInputs("no skill players on the slate")
    return built


def _defense_players(
    slate: dict[str, tuple[TeamLine, str | None]],
) -> list[tuple[Player, str | None, str | None, str | None]]:
    out = []
    for team, (line, game_id) in sorted(slate.items()):
        opponent = line.away_fd if team == line.home_fd else line.home_fd
        ref = require_fd(team)
        name = ref.odds[0]
        implied = line.implied_for(team)
        opp_implied = line.implied_for(opponent)
        pl = Player(
            pid=f"dst:{team}",
            name=name,
            position="D",
            salary=0,
            team=team,
            opponent=opponent,
            game=line.game,
            fppg=None,
            injury="",
            roster_position="D",
            spread=line.spread_for(team),
            total=line.total,
            implied_total=implied,
            implied_opp=opp_implied,
            moneyline=line.moneyline_for(team),
            lines_provider=line.provider,
            lines_source=line.source,
            objective=week1_score(implied, position="D", implied_opp=opp_implied),
        )
        out.append((pl, None, None, game_id))
    return out


def _csv_index(players: list[Player]) -> dict[tuple[str, str, str], Player]:
    out: dict[tuple[str, str, str], Player] = {}
    for pl in players:
        try:
            team = require_fd(pl.team).fd
        except UnmappedTeam:
            continue
        pos = "D" if (pl.position or "").upper() in {"D", "DEF"} else (pl.position or "").upper()
        out[(team, match_key(pl.name), pos)] = pl
    return out


def _apply_csv(
    triples: list[tuple[Player, str | None, str | None, str | None]],
    csv_players: list[Player] | None,
) -> list[PublishEntry]:
    idx = _csv_index(csv_players or [])
    entries: list[PublishEntry] = []
    for pl, gsis, pid, game_id in triples:
        hit = idx.get((pl.team, match_key(pl.name), pl.position))
        salary = None if hit is None else int(hit.salary)
        fanduel_id = None if hit is None else hit.pid
        entries.append(
            PublishEntry(
                player=pl,
                gsis_id=gsis,
                player_id=pid,
                game_id=game_id,
                fanduel_id=fanduel_id,
                salary=salary,
            )
        )
    return entries


def _injury_season_week(
    line_rows: list[dict],
    season: int | None,
    week: int | None,
) -> tuple[int, int]:
    if season is not None and week is not None:
        return int(season), int(week)
    for row in line_rows:
        if not isinstance(row, dict):
            continue
        raw_season = row.get("season")
        raw_week = row.get("week")
        if raw_season in (None, "") or raw_week in (None, ""):
            continue
        try:
            return int(raw_season), int(raw_week)
        except (TypeError, ValueError):
            continue
    raise PublishError("injuries need a season and week")


def _csv_marks_injury(csv_players: list[Player] | None) -> bool:
    for pl in csv_players or []:
        if (pl.injury or "").strip():
            return True
    return False


def _apply_entry_injuries(
    entries: list[PublishEntry],
    injury_rows: list[dict] | None,
    *,
    season: int,
    week: int,
    csv_players: list[Player] | None,
) -> list[PublishEntry]:
    players = apply_projection_injuries(
        [entry.player for entry in entries],
        injury_rows,
        season=season,
        week=week,
        csv_players=csv_players,
    )
    by_pid = {pl.pid: pl for pl in players}
    return [
        PublishEntry(
            player=by_pid[entry.player.pid],
            gsis_id=entry.gsis_id,
            player_id=entry.player_id,
            game_id=entry.game_id,
            fanduel_id=entry.fanduel_id,
            salary=entry.salary,
        )
        for entry in entries
    ]


def build_entries(
    line_rows: list[dict],
    depth_rows: list[dict],
    *,
    target_rows: list[TargetWeekRow] | None = None,
    snap_rows: list[SnapWeekRow] | None = None,
    prop_by_pid: dict | None = None,
    csv_players: list[Player] | None = None,
    extra_id_rows: list[dict] | None = None,
    injury_rows: list[dict] | None = None,
    injury_season: int | None = None,
    injury_week: int | None = None,
) -> list[PublishEntry]:
    """Score the slate with the existing week1_score path. No ILP."""
    slate = _team_lines(line_rows)
    if not slate:
        raise StaleInputs("no game_lines for the target week")
    ids = _merge_ids(_id_index(depth_rows), _id_index(extra_id_rows or []))
    skill = _depth_players(depth_rows, slate, ids, csv_players=csv_players)
    players = [p for p, *_r in skill]
    side = {(p.pid): (gsis, pid, gid) for p, gsis, pid, gid in skill}
    if target_rows:
        players, _stats = attach_targets(players, target_rows)
    if snap_rows:
        players, _stats = attach_snaps(players, snap_rows)
    if prop_by_pid is not None:
        players = attach_props(players, prop_by_pid)
    triples = []
    for pl in players:
        gsis, pid, gid = side[pl.pid]
        triples.append((pl, gsis, pid, gid))
    triples.extend(_defense_players(slate))
    entries = _apply_csv(triples, csv_players)
    if injury_rows is not None or _csv_marks_injury(csv_players):
        season, week = _injury_season_week(line_rows, injury_season, injury_week)
        entries = _apply_entry_injuries(
            entries,
            injury_rows,
            season=season,
            week=week,
            csv_players=csv_players,
        )
    return entries


def _inputs(
    player: Player,
    entry: PublishEntry,
    *,
    sim_efficiency: str | None = None,
) -> dict:
    pos = (player.position or "").upper()
    dst = pos in {"D", "DEF"}
    base = None
    if not dst:
        base = implied_core(
            player.implied_total or 0.0,
            depth_rank=player.depth_rank,
            position=player.position,
            target_share=player.target_share,
            snap_share=player.snap_share,
        )
    tags: list[str] = []
    if player.depth_rank is not None and (player.depth_source or "") == "gangstash":
        tags.append("gs-depth")
    elif player.depth_rank is not None:
        tags.append("ourlads")
    inherited = False
    if player.targets_source == "inherited":
        inherited = True
    elif player.target_share is not None:
        tags.append("gs-tgt" if player.targets_source == "gangstash" else "lineups-tgt")
    if player.snaps_source == "inherited":
        inherited = True
    elif player.snap_share is not None:
        tags.append("gs-snap" if player.snaps_source == "gangstash" else "lineups-snap")
    if inherited:
        tags.append("inherited")
    if player.prop_fd is not None:
        tags.append("gs-props")
    if dst and player.implied_opp is not None:
        tags.append("vegas-dst")
    out = {
        "sources": "/".join(tags) if tags else None,
        "lines_source": player.lines_source,
        "depth_source": player.depth_source,
        "depth_rank": player.depth_rank,
        "targets_source": player.targets_source,
        "target_share": player.target_share,
        "snaps_source": player.snaps_source,
        "snap_share": player.snap_share,
        "prop_book": player.prop_book,
        "prop_fd": player.prop_fd,
        "implied_total": None if player.implied_total is None else round(float(player.implied_total), 4),
        "implied_opp": None if player.implied_opp is None else round(float(player.implied_opp), 4),
        "usage_factor": None
        if dst
        else round(
            usage_factor(
                player.target_share,
                player.position,
                player.depth_rank,
                snap_share=player.snap_share,
            ),
            4,
        ),
        "prop_factor": None if base is None else round(prop_factor(base, player.prop_fd), 4),
        "fanduel_id": entry.fanduel_id,
        "injury": player.injury or None,
    }
    if sim_efficiency is not None:
        out["sim_efficiency"] = sim_efficiency
    return out


def _row(
    entry: PublishEntry,
    *,
    season: int,
    week: int,
    season_type: str,
    run_at: str,
    model: str,
    model_version: str,
    mean: float,
    p10: float | None,
    p50: float | None,
    p90: float | None,
    sim_efficiency: str | None = None,
    as_of: str | None = None,
    input_run_ids: list | None = None,
) -> dict:
    pl = entry.player
    return {
        "season": int(season),
        "week": int(week),
        "season_type": season_type or "REG",
        "run_at": run_at,
        "model": model,
        "model_version": model_version,
        "gsis_id": entry.gsis_id,
        "player_id": entry.player_id,
        "player_name": pl.name,
        "team": pl.team,
        "opponent": pl.opponent,
        "position": pl.position,
        "game_id": entry.game_id,
        "salary": entry.salary,
        "mean": round(float(mean), 4),
        "p10": None if p10 is None else round(float(p10), 4),
        "p50": None if p50 is None else round(float(p50), 4),
        "p90": None if p90 is None else round(float(p90), 4),
        "inputs": _inputs(pl, entry, sim_efficiency=sim_efficiency),
        "as_of": as_of,
        "input_run_ids": list(input_run_ids or []),
    }


def projection_rows(
    entries: list[PublishEntry],
    *,
    season: int,
    week: int,
    season_type: str,
    run_at: str,
    model_version: str,
    sim_by_pid: dict | None = None,
    sim_efficiency: str = "data",
    as_of: str | None = None,
    input_run_ids: list | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for entry in entries:
        pl = entry.player
        rows.append(
            _row(
                entry,
                season=season,
                week=week,
                season_type=season_type,
                run_at=run_at,
                model="board",
                model_version=model_version,
                mean=float(pl.objective if pl.objective is not None else pl.projection),
                p10=None,
                p50=None,
                p90=None,
                as_of=as_of,
                input_run_ids=input_run_ids,
            )
        )
    if not sim_by_pid:
        return rows
    missing = 0
    for entry in entries:
        stats = sim_by_pid.get(entry.player.pid)
        if stats is None:
            if is_pool_out(entry.player):
                continue
            missing += 1
            continue
        rows.append(
            _row(
                entry,
                season=season,
                week=week,
                season_type=season_type,
                run_at=run_at,
                model="sim",
                model_version=model_version,
                mean=float(stats.mean),
                p10=float(stats.p10),
                p50=float(stats.p50),
                p90=float(stats.p90),
                sim_efficiency=sim_efficiency,
                as_of=as_of,
                input_run_ids=input_run_ids,
            )
        )
    if missing:
        print(f"sim missing {missing} players", file=sys.stderr)
    return rows


def summarize(rows: list[dict]) -> str:
    lines: list[str] = []
    by_model: dict[str, list[dict]] = {}
    for row in rows:
        by_model.setdefault(str(row.get("model")), []).append(row)
    for model in ("board", "sim"):
        group = by_model.get(model) or []
        if model == "sim" and not group:
            continue
        counts = Counter(str(r.get("position") or "") for r in group)
        order = ("QB", "RB", "WR", "TE", "D")
        bits = ", ".join(f"{pos} {counts[pos]}" for pos in order if counts[pos])
        missing = sum(1 for r in group if not r.get("gsis_id"))
        lines.append(f"{model}: {len(group)} rows ({bits}); missing gsis_id {missing}")
    return "\n".join(lines)


def write_local(rows: list[dict], dest_dir: Path, stem: str) -> tuple[Path, Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    json_path = dest_dir / f"{stem}.json"
    csv_path = dest_dir / f"{stem}.csv"
    json_path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ROW_FIELDS))
        writer.writeheader()
        for row in rows:
            flat = dict(row)
            flat["inputs"] = json.dumps(row.get("inputs") or {}, separators=(",", ":"))
            flat["input_run_ids"] = json.dumps(row.get("input_run_ids") or [], separators=(",", ":"))
            writer.writerow(flat)
    return json_path, csv_path


def _has_qb(rows: list[dict]) -> bool:
    return any(
        skill_pos(str(r.get("pos_abb") or "")) == "QB"
        for r in rows
        if isinstance(r, dict)
    )


def _load_depth(refresh: bool) -> tuple[list[dict], dict]:
    """Skill chart. Omit pos_grp so QBs are included; fall back per position."""
    try:
        rows, meta = fetch_depth_charts(pos_grp=None, refresh=refresh)
    except GangstashDataError as e:
        rows = []
        meta = {"cache_stale": False}
        for pos in SKILL_POSITIONS:
            chunk, meta = fetch_depth_charts(position=pos, pos_grp=None, refresh=refresh)
            _stale(meta, f"depth_charts {pos}")
            rows.extend(chunk)
        if not rows:
            raise PublishError(str(e)) from e
    else:
        _stale(meta, "depth_charts")
        if not _has_qb(rows):
            extra, meta_q = fetch_depth_charts(position="QB", pos_grp=None, refresh=refresh)
            _stale(meta_q, "depth_charts QB")
            rows = list(rows) + list(extra)
            meta = _merge_fetch_meta(meta, meta_q)
    if not rows:
        raise StaleInputs("no depth_charts rows")
    return rows, meta


def _call_depth_weekly(season: int, week: int, refresh: bool, as_of: str, **kwargs):
    return fetch_depth_charts_weekly(
        season=int(season),
        week=int(week),
        position=kwargs.get("position"),
        pos_grp=kwargs.get("pos_grp"),
        refresh=refresh,
        as_of=as_of,
    )


def _load_depth_weekly(
    season: int,
    week: int,
    refresh: bool,
    as_of: str,
) -> tuple[list[dict], dict]:
    """Chart in force at ``as_of``, including games that have not kicked off."""
    try:
        rows, meta = _call_depth_weekly(season, week, refresh, as_of, pos_grp=None)
    except GangstashDataError as e:
        rows = []
        meta = {"cache_stale": False, "response_meta": {}}
        for pos in SKILL_POSITIONS:
            chunk, meta = _call_depth_weekly(
                season, week, refresh, as_of, position=pos, pos_grp=None
            )
            _stale(meta, f"depth_charts_weekly {pos}")
            rows.extend(chunk)
        if not rows:
            raise PublishError(str(e)) from e
    else:
        _stale(meta, "depth_charts_weekly")
        if not _has_qb(rows):
            extra, meta_q = _call_depth_weekly(
                season, week, refresh, as_of, position="QB", pos_grp=None
            )
            _stale(meta_q, "depth_charts_weekly QB")
            rows = list(rows) + list(extra)
            meta = _merge_fetch_meta(meta, meta_q)
    if not rows:
        raise StaleInputs("no depth_charts_weekly rows")
    return _uniquify_legacy_ranks(rows), meta


def _merge_fetch_meta(left: dict, right: dict) -> dict:
    """Keep both server metas' freshness when a QB chart is appended."""
    merged = dict(left)
    left_server = dict(left.get("response_meta") or {})
    right_server = dict(right.get("response_meta") or {})
    if not left_server and not right_server:
        return merged
    observed = [
        stamp
        for stamp in (
            _parse_utc(left_server.get("observed_at")),
            _parse_utc(right_server.get("observed_at")),
        )
        if stamp is not None
    ]
    server = dict(left_server or right_server)
    if observed:
        server["observed_at"] = max(observed).replace(microsecond=0).isoformat()
    server["untimestamped"] = bool(
        left_server.get("untimestamped") or right_server.get("untimestamped")
    )
    if "includes_baseline" in left_server or "includes_baseline" in right_server:
        server["includes_baseline"] = bool(
            left_server.get("includes_baseline") or right_server.get("includes_baseline")
        )
    merged["response_meta"] = server
    merged["cache_stale"] = bool(left.get("cache_stale") or right.get("cache_stale"))
    return merged


def _uniquify_legacy_ranks(rows: list[dict]) -> list[dict]:
    """Rewrite tied 2024 weekly ranks to 1..n. ESPN charts stay as published."""
    if not any(isinstance(row, dict) and row.get("chart_format") == "nflverse_weekly" for row in rows):
        return rows
    groups: dict[tuple[str, str], list[int]] = {}
    order: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("chart_format") != "nflverse_weekly":
            continue
        team = str(row.get("team_fd") or "")
        pos = str(row.get("pos_abb") or "").upper()
        key = (team, pos)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(index)
    rewritten = [dict(row) if isinstance(row, dict) else row for row in rows]
    for key in order:
        indexes = groups[key]
        ranks = []
        for index in indexes:
            try:
                ranks.append(int(rewritten[index].get("pos_rank")))
            except (TypeError, ValueError):
                ranks.append(None)
        present = [rank for rank in ranks if rank is not None]
        if len(present) != len(indexes) or len(present) == len(set(present)):
            continue
        ordered = sorted(
            indexes,
            key=lambda index: (
                int(rewritten[index].get("pos_rank") or 0),
                str(rewritten[index].get("pos_slot") or ""),
                str(rewritten[index].get("player_name") or ""),
            ),
        )
        for rank, index in enumerate(ordered, start=1):
            rewritten[index]["pos_rank"] = rank
    return rewritten


def _injury_records(rows: list[dict]) -> tuple[list[dict], str | None]:
    """Drop seeded baseline rows. ``observed_at`` is the latest real capture."""
    kept: list[dict] = []
    stamps: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("is_baseline") is True:
            continue
        stamp = _parse_utc(row.get("captured_at"))
        if stamp is not None:
            stamps.append(stamp)
        kept.append(
            {
                "season": row.get("season"),
                "week": row.get("week"),
                "player_name": row.get("full_name") or row.get("player_name") or row.get("name"),
                "status": row.get("report_status") or row.get("status") or "",
                "team_fd": row.get("team_fd") or row.get("team"),
                "gsis_id": row.get("gsis_id"),
                "player_id": row.get("player_id"),
            }
        )
    observed = max(stamps).replace(microsecond=0).isoformat() if stamps else None
    return kept, observed


def _with_prop_stamp(row: dict) -> dict:
    """``rows_to_props`` orders on ``scraped_at``. Snapshots carry ``captured_at``."""
    if row.get("scraped_at"):
        return row
    copied = dict(row)
    if copied.get("captured_at"):
        copied["scraped_at"] = copied["captured_at"]
    return copied


def _optional_window(
    label: str,
    raw: list[dict],
    aggregate,
) -> list[dict]:
    """Empty usage boards are not stale. A missing column still stops."""
    if not raw:
        print(f"{label} empty; usage factor 1.0", file=sys.stderr)
        return []
    try:
        return aggregate(raw)
    except GangstashDataError as e:
        if "empty" in str(e).lower():
            print(f"{label} empty; usage factor 1.0", file=sys.stderr)
            return []
        raise PublishError(str(e)) from e


def _load_targets(season: int, refresh: bool) -> tuple[list[TargetWeekRow], list[dict], dict]:
    raw, meta = fetch_targets(season=season, weeks=None, refresh=refresh)
    _stale(meta, "targets")
    normalized = _optional_window("targets", raw, aggregate_target_window)
    rows = [
        TargetWeekRow(
            player=item["player_name"],
            team=item["team_fd"],
            position=item["position"],
            week=int(item["week"]),
            targets=int(item["targets"]),
            target_share=float(item["target_share"]),
            targets_avg=float(item["targets_avg"]),
            targets_total=int(item["targets_total"]),
            source="gangstash",
            asof=date.today().isoformat(),
        )
        for item in normalized
    ]
    return rows, raw, meta


def _load_snaps(season: int, refresh: bool) -> tuple[list[SnapWeekRow], list[dict], dict]:
    raw, meta = fetch_snaps(season=season, weeks=None, refresh=refresh)
    _stale(meta, "snaps")
    normalized = _optional_window("snaps", raw, aggregate_snap_window)
    rows = [
        SnapWeekRow(
            player=item["player_name"],
            team=item["team_fd"],
            position=item["position"],
            week=int(item["week"]),
            snaps=int(item["snaps"]),
            snap_share=float(item["snap_share"]),
            snaps_avg=float(item["snaps_avg"]),
            snaps_total=int(item["snaps_total"]),
            team_snap_pct=None,
            source="gangstash",
            asof=date.today().isoformat(),
        )
        for item in normalized
    ]
    return rows, raw, meta


def collector_run_window(as_of: datetime) -> tuple[str, str]:
    """Inclusive UTC days covering the last ``COLLECTOR_LOOKBACK_HOURS`` before ``as_of``."""
    moment = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    start = moment - timedelta(hours=COLLECTOR_LOOKBACK_HOURS)
    return start.date().isoformat(), moment.date().isoformat()


def _load_input_run_ids(as_of: datetime, *, refresh: bool) -> tuple[list[str], dict, str | None]:
    """Bounded ``collector_runs`` read. A failure is a warning, not a failed publish.

    Returns ``(ids, meta, warning)``. ``warning`` is set when the read
    fails or times out; ``ids`` is then empty.
    """
    date_from, date_to = collector_run_window(as_of)
    try:
        rows, meta = fetch_collector_runs(
            date_from=date_from,
            date_to=date_to,
            refresh=refresh,
        )
    except Exception as e:
        return [], {}, f"collector_runs unavailable ({e}); input_run_ids empty"
    return select_input_run_ids(rows, as_of=as_of), meta, None


def _max_row_stamp(rows: list, keys: tuple) -> str | None:
    """Latest parseable timestamp among ``keys`` on each row."""
    stamps = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in keys:
            stamp = _parse_utc(row.get(key))
            if stamp is not None:
                stamps.append(stamp)
                break
    if not stamps:
        return None
    return max(stamps).replace(microsecond=0).isoformat()


def _capture_sim_feeds(capture: RunCapture, season: int) -> None:
    """Re-read the sim feeds from the same-day cache. Does not change draws."""
    pulls = (
        ("team_stats", lambda: fetch_team_stats(season=int(season), refresh=False)),
        (
            "player_stats_weekly",
            lambda: fetch_player_stats_weekly(season=int(season), weeks=None, refresh=False),
        ),
        (
            "player_usage",
            lambda: fetch_player_usage(season=int(season), weeks=None, refresh=False),
        ),
    )
    for name, fetch in pulls:
        try:
            rows, meta = fetch()
        except (GangstashDataError, GangstashDataKeyMissing):
            continue
        _remember(capture, name, rows, meta)


def load_slate(
    args: argparse.Namespace,
    today: date,
    *,
    as_of: datetime | None = None,
    point_in_time: bool = False,
) -> tuple:
    """Score the slate. Return ``(season, week, entries, capture)``.

    The default path reads the current game lines, depth chart, props
    board, and ``dataset=injuries``. ``point_in_time`` reads snapshot
    datasets at ``as_of``, including ``injury_snapshots`` with baseline
    rows excluded. Both paths drop Out/IR/NA and promote depth the same way.
    """
    moment = as_of or datetime.now(timezone.utc).replace(microsecond=0)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc).replace(microsecond=0)
    as_of_text = normalize_as_of(moment.isoformat())
    capture = RunCapture()
    capture.point_in_time = bool(point_in_time)
    refresh = bool(args.refresh)
    if point_in_time:
        season, week, line_rows, line_meta = resolve_nfl_week_as_of(
            today,
            refresh=refresh,
            as_of=as_of_text,
            season=args.season,
            week=args.week,
        )
        _remember(capture, "game_line_snapshots", line_rows, line_meta)
        depth_rows, depth_meta = _load_depth_weekly(season, week, refresh, as_of_text)
        _remember(capture, "depth_charts_weekly", depth_rows, depth_meta)
    else:
        season, week, line_rows = resolve_nfl_week(
            today,
            refresh=refresh,
            season=args.season,
            week=args.week,
        )
        # Score the rows resolve just returned. A second read is only for
        # meta, and only replaces the board when it is the same payload.
        cached_rows, line_meta = fetch_game_lines(
            season=season,
            week=week,
            refresh=False,
        )
        _stale(line_meta, f"game_lines {season} week {week}")
        if cached_rows == line_rows:
            line_rows = cached_rows
        _remember(capture, "game_lines", line_rows, line_meta)
        depth_rows, depth_meta = _load_depth(refresh)
        _remember(capture, "depth_charts", depth_rows, depth_meta)
    capture.line_rows = list(line_rows)
    target_rows, target_ids, target_meta = _load_targets(season, refresh)
    _remember(capture, "targets", target_ids, target_meta)
    snap_rows, snap_ids, snap_meta = _load_snaps(season, refresh)
    _remember(capture, "snaps", snap_ids, snap_meta)
    csv_players = load_fanduel_csv(args.csv) if args.csv else None
    if point_in_time:
        raw_injuries, injury_meta = fetch_injury_snapshots(
            season=season,
            week=week,
            refresh=refresh,
            as_of=as_of_text,
        )
        _stale(injury_meta, "injury_snapshots")
        injury_rows, injury_observed = _injury_records(raw_injuries)
        _remember(
            capture,
            "injury_snapshots",
            injury_rows,
            injury_meta,
            observed_at=injury_observed,
            normalized=injury_rows,
            observed_set=True,
        )
    else:
        from nfl.sim_feed import load_week_injuries

        injury_rows, injury_meta = load_week_injuries(
            season=season,
            week=week,
            refresh=refresh,
        )
        _stale(injury_meta, "injuries")
        _remember(capture, "injuries", injury_rows, injury_meta)
    entries = build_entries(
        line_rows,
        depth_rows,
        target_rows=target_rows,
        snap_rows=snap_rows,
        csv_players=csv_players,
        extra_id_rows=list(target_ids) + list(snap_ids),
        injury_rows=injury_rows,
        injury_season=season,
        injury_week=week,
    )
    if point_in_time:
        prop_rows, prop_meta = fetch_props_snapshots(
            season=season,
            week=week,
            refresh=refresh,
            as_of=as_of_text,
        )
        _stale(prop_meta, "props_snapshots")
        adapted = [_with_prop_stamp(row) for row in prop_rows if isinstance(row, dict)]
        _remember(capture, "props_snapshots", adapted, prop_meta)
        try:
            by_pid, prop_stats = ingest_slate_props(
                [e.player for e in entries],
                rows=adapted,
                fetch_meta=prop_meta,
            )
        except PropsKeyMissing as e:
            raise PublishError(str(e)) from e
        except PropsError as e:
            raise PublishError(str(e)) from e
    else:
        try:
            by_pid, prop_stats = ingest_slate_props(
                [e.player for e in entries],
                refresh=refresh,
                keep_rows=True,
            )
        except PropsKeyMissing as e:
            raise PublishError(str(e)) from e
        except PropsError as e:
            raise PublishError(str(e)) from e
        if prop_stats.get("cache_stale"):
            raise StaleInputs("props cache is stale")
        prop_rows = list(prop_stats.pop("_rows", []) or [])
        # /props has no response meta. Freshness is the newest scraped_at.
        server = prop_stats.get("response_meta") if isinstance(prop_stats, dict) else None
        server_obs = server.get("observed_at") if isinstance(server, dict) else None
        if server_obs:
            _remember(capture, "props", prop_rows, prop_stats)
        else:
            _remember(
                capture,
                "props",
                prop_rows,
                prop_stats,
                observed_at=_max_row_stamp(prop_rows, ("scraped_at",)),
                observed_set=True,
            )
    scored = attach_props([e.player for e in entries], by_pid)
    by_scored = {pl.pid: pl for pl in scored}
    entries = [
        PublishEntry(
            player=by_scored[e.player.pid],
            gsis_id=e.gsis_id,
            player_id=e.player_id,
            game_id=e.game_id,
            fanduel_id=e.fanduel_id,
            salary=e.salary,
        )
        for e in entries
    ]
    # Props rescore from depth. Re-apply so an Out stays at 0 and the
    # promoted player keeps the role, now including any prop tilt.
    entries = _apply_entry_injuries(
        entries,
        injury_rows,
        season=season,
        week=week,
        csv_players=csv_players,
    )
    n_out = sum(1 for entry in entries if is_pool_out(entry.player))
    print(
        f"injuries out {n_out}  rows {len(injury_rows)}",
        file=sys.stderr,
    )
    run_ids, runs_meta, runs_warning = _load_input_run_ids(moment, refresh=refresh)
    if runs_warning:
        print(f"publish projections: {runs_warning}", file=sys.stderr)
        capture.notes.append(runs_warning)
        capture.input_run_ids = []
    else:
        _remember(capture, "collector_runs", [], runs_meta)
        # The run log is provenance, not a scored board. Drop it from the
        # manifest dataset map so a missing timestamp on the log is not a
        # stale-input warning about the lines themselves.
        capture.datasets.pop("collector_runs", None)
        capture.input_run_ids = run_ids
    return season, week, entries, capture


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--season-type", default="REG")
    ap.add_argument("--csv", default=None, help="FanDuel players list; attaches salary and Id")
    ap.add_argument(
        "--sim",
        type=int,
        default=0,
        help="Also publish model=sim with N draws (0 off). "
        "mean/p10/p50/p90 from simulate_games with the optimizer's "
        "--projection-source sim inputs.",
    )
    ap.add_argument("--sim-seed", type=int, default=1)
    ap.add_argument(
        "--sim-efficiency",
        choices=("placeholder", "data"),
        default="data",
        help="layer-4 efficiency for --sim (default data). placeholder keeps "
        "league averages. Missing sim inputs fall back to placeholder and "
        "the board, and the log says so. Same flag as nfl.optimize.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=f"Write JSON and CSV under {OUT_DIR} and do not POST",
    )
    ap.add_argument(
        "--refresh",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Bypass same-day cache (default). --no-refresh reads cache.",
    )
    ap.add_argument("--out-dir", default=None)
    ap.add_argument(
        "--report",
        action="store_true",
        help="After a successful POST or --dry-run, write the Monte Carlo "
        "report under nfl/reports/ and print its path",
    )
    ap.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 point in time (no offset means UTC). Default is this "
        "run's start, which keeps the current game lines, depth chart, "
        "props board, and injuries dataset and still records provenance. "
        "An explicit value reads game_line_snapshots, props_snapshots, "
        "depth_charts_weekly, and injury_snapshots at that time "
        "(is_baseline rows excluded).",
    )
    return ap.parse_args(argv)


def emit_projection_report(
    args: argparse.Namespace,
    rows: list[dict],
    game_draws: tuple,
    *,
    season: int,
    week: int,
    run_at: str,
    notes: list | None = None,
    extra: list | None = None,
    line_rows: list | None = None,
    report_filename: str | None = None,
    sidecar_filename: str | None = None,
) -> Path:
    """Write the markdown report and, when sim game draws exist, the sidecar."""
    from nfl.report import (
        efficiency_from_rows,
        summarize_game_draws,
        vegas_games_from_lines,
        write_games_sidecar,
        write_report,
    )

    summaries = summarize_game_draws(game_draws)
    if summaries:
        games: list[dict] = summaries
        write_games_sidecar(
            season=season,
            week=week,
            run_at=run_at,
            games=summaries,
            filename=sidecar_filename,
        )
    else:
        games = []
        try:
            if line_rows is None:
                line_rows, _meta = fetch_game_lines(
                    season=int(season),
                    week=int(week),
                    refresh=False,
                )
            games = vegas_games_from_lines(line_rows)
        except Exception as e:
            print(f"report: game lines unavailable ({e})", file=sys.stderr)
    sim_rows = [row for row in rows if row.get("model") == "sim"]
    used = sim_rows or rows
    has_sim = bool(sim_rows)
    return write_report(
        used,
        games,
        season=season,
        week=week,
        run_at=run_at,
        draws=int(args.sim) if has_sim and args.sim else None,
        efficiency=(
            args.sim_efficiency
            if has_sim and efficiency_from_rows(used) == "unknown"
            else efficiency_from_rows(used)
        ),
        notes=notes,
        extra=extra,
        filename=report_filename,
    )


def _publish_side_files(
    args: argparse.Namespace,
    rows: list[dict],
    game_draws: tuple,
    capture: RunCapture,
    *,
    season: int,
    week: int,
    run_at: str,
    started: datetime,
    as_of_text: str,
    cli_args: list,
) -> int:
    """Manifest, provenance warnings, and the optional markdown report.

    An explicit ``--as-of`` uses a distinct filename for the manifest,
    the report, and the games sidecar. The nightly names stay put.
    """
    explicit = args.as_of is not None
    names = publish_artifact_names(season, week, run_at, as_of_text if explicit else None)
    as_of_dt = _parse_utc(as_of_text) if explicit else None
    warnings = provenance_warnings(capture, now=started, as_of=as_of_dt)
    fresh = freshness_warnings(
        capture,
        now=started,
        as_of=as_of_dt if capture.point_in_time else None,
    )
    other = [line for line in warnings if line not in fresh]
    already = {str(note) for note in capture.notes}
    for line in other:
        if line not in already:
            print(f"publish projections: {line}", file=sys.stderr)
    if fresh:
        print(
            "publish projections: stale inputs: " + "; ".join(fresh),
            file=sys.stderr,
        )
    try:
        manifest = build_manifest(
            capture,
            seed=int(args.sim_seed),
            draws=int(args.sim),
            as_of=as_of_text,
            cli_args=cli_args,
            warnings=warnings,
        )
        manifest_file = write_manifest(
            manifest,
            season=season,
            week=week,
            run_at=run_at,
            filename=names["manifest"],
        )
    except OSError as e:
        print(f"publish projections: {e}", file=sys.stderr)
        return 1
    print(f"manifest {manifest_file}", file=sys.stderr)
    if not args.report:
        return 0
    try:
        path = emit_projection_report(
            args,
            rows,
            game_draws,
            season=season,
            week=week,
            run_at=run_at,
            notes=fresh or None,
            extra=other or None,
            line_rows=capture.line_rows or None,
            report_filename=names["report"],
            sidecar_filename=names["sidecar"],
        )
    except Exception as e:
        print(f"publish projections: {e}", file=sys.stderr)
        return 1
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.sim < 0:
        print("publish projections: --sim must be >= 0", file=sys.stderr)
        return 1
    cli_args = list(sys.argv[1:] if argv is None else argv)
    started = datetime.now(timezone.utc).replace(microsecond=0)
    point_in_time = args.as_of is not None
    try:
        as_of_text = normalize_as_of(args.as_of) if point_in_time else normalize_as_of(started.isoformat())
    except GangstashDataError as e:
        print(f"publish projections: {e}", file=sys.stderr)
        return 1
    as_of = _parse_utc(as_of_text) or started
    key = None
    if not args.dry_run:
        try:
            key = projections_key()
        except ProjectionsKeyMissing as e:
            print(f"publish projections: {e}", file=sys.stderr)
            return 1
    today = datetime.now(ET).date()
    try:
        loaded = load_slate(args, today, as_of=as_of, point_in_time=point_in_time)
        if len(loaded) == 4:
            season, week, entries, capture = loaded
        else:
            season, week, entries = loaded
            capture = RunCapture()
            capture.point_in_time = point_in_time
        sim = maybe_sim(
            entries,
            args.sim,
            args.sim_seed,
            season=season,
            refresh=bool(args.refresh),
            week=week,
            sim_efficiency=args.sim_efficiency,
        )
        sim_by_pid, used_efficiency, game_draws = sim_parts(sim)
        if args.sim > 0 and len(loaded) == 4 and load_simulate_games() is not None:
            _capture_sim_feeds(capture, season)
    except StaleInputs as e:
        print(f"publish projections: {e}", file=sys.stderr)
        return 1
    except (GangstashDataKeyMissing, GangstashKeyMissing) as e:
        print(
            f"publish projections: {missing_read_key_message(str(e))}",
            file=sys.stderr,
        )
        return 1
    except (
        PublishError,
        GangstashDataError,
        UnmappedTeam,
    ) as e:
        print(f"publish projections: {e}", file=sys.stderr)
        return 1
    run_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    version = model_version()
    rows = projection_rows(
        entries,
        season=season,
        week=week,
        season_type=args.season_type,
        run_at=run_at,
        model_version=version,
        sim_by_pid=sim_by_pid,
        sim_efficiency=used_efficiency,
        as_of=as_of_text,
        input_run_ids=list(capture.input_run_ids),
    )
    print(
        f"projections {season} week {week} {args.season_type} "
        f"run_at={run_at} model_version={version} sim_efficiency={used_efficiency} "
        f"as_of={as_of_text}",
        file=sys.stderr,
    )
    print(summarize(rows), file=sys.stderr)
    if args.dry_run:
        dest = Path(args.out_dir) if args.out_dir else OUT_DIR
        stamp = run_at.replace("+00:00", "Z").replace(":", "")
        stem = f"{season}-w{int(week):02d}-{stamp}"
        json_path, csv_path = write_local(rows, dest, stem)
        print(f"dry-run wrote {json_path} and {csv_path}; not posted", file=sys.stderr)
        return _publish_side_files(
            args,
            rows,
            game_draws,
            capture,
            season=season,
            week=week,
            run_at=run_at,
            started=started,
            as_of_text=as_of_text,
            cli_args=cli_args,
        )
    assert key is not None
    try:
        inserted, updated = post_projection_rows(rows, key=key)
    except PublishError as e:
        print(f"publish projections: {e}", file=sys.stderr)
        return 1
    print(f"posted inserted={inserted} updated={updated}", file=sys.stderr)
    return _publish_side_files(
        args,
        rows,
        game_draws,
        capture,
        season=season,
        week=week,
        run_at=run_at,
        started=started,
        as_of_text=as_of_text,
        cli_args=cli_args,
    )


if __name__ == "__main__":
    sys.exit(main())
