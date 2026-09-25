"""Publish nightly NFL projections to gangstash.

Board rows are `week1_score` for every skill player and DEF on the current
week's slate. Inputs are gangstash game lines, depth, targets, snaps, and
props. A FanDuel players CSV is optional (salary and FanDuel id only).

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
snaps, and props. If it is unset the command exits 1 before posting.
There is no other read source. `--refresh` (the default) does not
fall back to a cache when the key is missing.
`--report` writes `nfl/reports/<season>-w<week>-<date>.md` after a
successful POST or `--dry-run` and prints that path.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
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
)
from nfl.gangstash_data import (
    aggregate_snap_window,
    aggregate_target_window,
    fetch_depth_charts,
    fetch_game_lines,
    fetch_snaps,
    fetch_targets,
)
from nfl.http import HttpError, http_json_post
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
)


class PublishError(Exception):
    """Projections publish failed."""


class ProjectionsKeyMissing(PublishError):
    """No GANGSTASH_PROJECTIONS_WRITER_KEY."""


class StaleInputs(PublishError):
    """Game lines or depth are missing or from a stale cache."""


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
        "snaps, and props from gangstash and exits without posting. "
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
        from nfl.sim_feed import resolve_sim_inputs
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
            [e.player for e in entries],
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


def build_entries(
    line_rows: list[dict],
    depth_rows: list[dict],
    *,
    target_rows: list[TargetWeekRow] | None = None,
    snap_rows: list[SnapWeekRow] | None = None,
    prop_by_pid: dict | None = None,
    csv_players: list[Player] | None = None,
    extra_id_rows: list[dict] | None = None,
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
    return _apply_csv(triples, csv_players)


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
    if player.target_share is not None:
        tags.append("gs-tgt" if player.targets_source == "gangstash" else "lineups-tgt")
    if player.snap_share is not None:
        tags.append("gs-snap" if player.snaps_source == "gangstash" else "lineups-snap")
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
            )
        )
    if not sim_by_pid:
        return rows
    missing = 0
    for entry in entries:
        stats = sim_by_pid.get(entry.player.pid)
        if stats is None:
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
    if not rows:
        raise StaleInputs("no depth_charts rows")
    return rows, meta


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


def _load_targets(season: int, refresh: bool) -> tuple[list[TargetWeekRow], list[dict]]:
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
    return rows, normalized


def _load_snaps(season: int, refresh: bool) -> tuple[list[SnapWeekRow], list[dict]]:
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
    return rows, normalized


def load_slate(args: argparse.Namespace, today: date) -> tuple[int, int, list[PublishEntry]]:
    season, week, line_rows = resolve_nfl_week(
        today,
        refresh=bool(args.refresh),
        season=args.season,
        week=args.week,
    )
    depth_rows, _depth_meta = _load_depth(bool(args.refresh))
    target_rows, target_ids = _load_targets(season, bool(args.refresh))
    snap_rows, snap_ids = _load_snaps(season, bool(args.refresh))
    csv_players = load_fanduel_csv(args.csv) if args.csv else None
    entries = build_entries(
        line_rows,
        depth_rows,
        target_rows=target_rows,
        snap_rows=snap_rows,
        csv_players=csv_players,
        extra_id_rows=list(target_ids) + list(snap_ids),
    )
    try:
        by_pid, prop_meta = ingest_slate_props(
            [e.player for e in entries],
            refresh=bool(args.refresh),
        )
    except PropsKeyMissing as e:
        raise PublishError(str(e)) from e
    except PropsError as e:
        raise PublishError(str(e)) from e
    if prop_meta.get("cache_stale"):
        raise StaleInputs("props cache is stale")
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
    return season, week, entries


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
    return ap.parse_args(argv)


def emit_projection_report(
    args: argparse.Namespace,
    rows: list[dict],
    game_draws: tuple,
    *,
    season: int,
    week: int,
    run_at: str,
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
        write_games_sidecar(season=season, week=week, run_at=run_at, games=summaries)
    else:
        games = []
        try:
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
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.sim < 0:
        print("publish projections: --sim must be >= 0", file=sys.stderr)
        return 1
    key = None
    if not args.dry_run:
        try:
            key = projections_key()
        except ProjectionsKeyMissing as e:
            print(f"publish projections: {e}", file=sys.stderr)
            return 1
    today = datetime.now(ET).date()
    try:
        season, week, entries = load_slate(args, today)
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
    )
    print(
        f"projections {season} week {week} {args.season_type} "
        f"run_at={run_at} model_version={version} sim_efficiency={used_efficiency}",
        file=sys.stderr,
    )
    print(summarize(rows), file=sys.stderr)
    if args.dry_run:
        dest = Path(args.out_dir) if args.out_dir else OUT_DIR
        stamp = run_at.replace("+00:00", "Z").replace(":", "")
        stem = f"{season}-w{int(week):02d}-{stamp}"
        json_path, csv_path = write_local(rows, dest, stem)
        print(f"dry-run wrote {json_path} and {csv_path}; not posted", file=sys.stderr)
        if args.report:
            try:
                path = emit_projection_report(
                    args,
                    rows,
                    game_draws,
                    season=season,
                    week=week,
                    run_at=run_at,
                )
            except Exception as e:
                print(f"publish projections: {e}", file=sys.stderr)
                return 1
            print(path)
        return 0
    assert key is not None
    try:
        inserted, updated = post_projection_rows(rows, key=key)
    except PublishError as e:
        print(f"publish projections: {e}", file=sys.stderr)
        return 1
    print(f"posted inserted={inserted} updated={updated}", file=sys.stderr)
    if args.report:
        try:
            path = emit_projection_report(
                args,
                rows,
                game_draws,
                season=season,
                week=week,
                run_at=run_at,
            )
        except Exception as e:
            print(f"publish projections: {e}", file=sys.stderr)
            return 1
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
