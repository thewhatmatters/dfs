"""Board vs sim error against gangstash ``player_stats_weekly`` ``fd_points``.

```
python3 -m nfl.backtest --csv nfl/data/<players-list>.csv --season 2026 --week 2
```

Builds the board and the sim the way ``nfl.optimize`` does for that week:
closing lines (then game lines), the FanDuel injury column, depth, prior-week
targets and snaps, and the latest pre-kickoff prop snapshot. An empty
optional source is named on ``missing:`` and the week still scores. Lines
are not optional: no lines is a hard stop unless ``--allow-missing-lines``.
Targets and snaps are prior weeks only. Props from another week are dropped.

``--lines-file`` is a CSV or JSON of historical lines (FanDuel or nflverse
columns). An nflverse schedule is recognized by ``season``, ``week``,
``home_team``, ``away_team``, ``spread_line``, and ``total_line``; the rest
of that file is ignored. ``spread_line`` is positive when home is favored.
``--starters-only`` prints the effective depth chart who played:
QB/RB/TE depth 1, WR depth 1–3, plus DEF. The default prints the full pool,
that starters slice, and a hindsight slice (the QB who actually took the
snaps). A questionable player is not handed off. A Q with no stat row, or
0 offensive snaps, is counted as a DNP and the projection stays.

``--projection-source`` stays ``board`` on the optimizer. This command
only compares the two. ``--sim-efficiency data`` (the default) scores the
sim with shrunk prior-week rates. ``placeholder`` keeps the league averages.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from nfl.depth import attach_depth_ranks, depth_rows_for_backtest, scope_depth_rows
from nfl.gangstash import (
    GangstashDataError,
    GangstashDataKeyMissing,
    GangstashError,
    GangstashKeyMissing,
    GangstashTruncated,
    fetch_props,
)
from nfl.gangstash_data import (
    fetch_closing_lines,
    fetch_depth_charts,
    fetch_game_lines,
    fetch_player_stats_weekly,
    fetch_week_injuries,
    map_depth_slots,
    map_game_lines,
    parse_player_stat_row,
)
from nfl.injuries import handoff_chart, injury_rows_from_records, stamp_injuries
from nfl.lines import (
    LinesError,
    load_lines_file,
    slate_from_players,
    team_lines_from_gangstash,
)
from nfl.names import match_key
from nfl.ourlads import DepthRow, skill_pos
from nfl.players import Player, load_fanduel_csv
from nfl.projections import attach_team_lines, score_player
from nfl.props import attach_props, parse_stamp, props_for_week, rows_to_props
from nfl.sim import simulate_games
from nfl.sim_efficiency import build_efficiency
from nfl.sim_feed import resolve_sim_inputs
from nfl.sim_inputs import SimInputError, SimInputs
from nfl.snaps import SnapsError, attach_snaps, load_optimizer_snaps
from nfl.targets import TargetsError, attach_targets, load_optimizer_targets
from nfl.teams import UnmappedTeam

_POS_ORDER = ("QB", "RB", "WR", "TE", "DEF")
_COUNT_KEYS = (
    "offense_snaps",
    "snaps",
    "pass_attempts",
    "carries",
    "rushing_attempts",
    "targets",
)
_MISSING_ORDER = (
    "closing_lines",
    "game_lines",
    "depth",
    "injuries",
    "targets",
    "snaps",
    "props",
    "sim_inputs",
    "player_stats_weekly",
)
_WIDE_START = datetime(2000, 1, 1, tzinfo=timezone.utc)
_WIDE_END = datetime(2100, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class PosError:
    position: str
    n: int
    board_me: float
    board_mae: float
    sim_me: float
    sim_mae: float


@dataclass(frozen=True)
class BacktestReport:
    season: int
    week: int
    missing: tuple[str, ...]
    rows: tuple[PosError, ...]
    n: int
    starters: tuple[PosError, ...] = ()
    starters_n: int = 0
    hindsight: tuple[PosError, ...] = ()
    hindsight_n: int = 0
    pool: str = "full"
    notes: tuple[str, ...] = ()
    show_hindsight: bool = False

    def to_text(self) -> str:
        lines = [
            f"backtest season {self.season} week {self.week}  n {self.n}",
        ]
        if self.missing:
            lines.append("missing: " + ", ".join(self.missing))
        else:
            lines.append("missing: none")
        for note in self.notes:
            lines.append(note)
        if self.pool == "starters":
            lines.append("pool: starters")
            lines.extend(_format_rows(self.rows))
        else:
            lines.append("pool: full")
            lines.extend(_format_rows(self.rows))
            lines.append("pool: starters")
            lines.extend(_format_rows(self.starters))
        if self.show_hindsight:
            lines.append("pool: hindsight (actual QB1 by snaps)")
            lines.extend(_format_rows(self.hindsight))
        return "\n".join(lines)


def _format_rows(rows: tuple[PosError, ...]) -> list[str]:
    lines = [
        f"{'pos':<5} {'n':>4} {'board_me':>9} {'board_mae':>10} "
        f"{'sim_me':>8} {'sim_mae':>8}"
    ]
    if not rows:
        lines.append("(no joined players)")
        return lines
    for row in rows:
        lines.append(
            f"{row.position:<5} {row.n:4d} {row.board_me:9.2f} "
            f"{row.board_mae:10.2f} {row.sim_me:8.2f} {row.sim_mae:8.2f}"
        )
    return lines


def _position(raw: str) -> str:
    pos = (raw or "").upper()
    if pos in {"D", "DST"}:
        return "DEF"
    return pos or "?"


def prior_weeks(week: int) -> list[int]:
    """Weeks whose targets and snaps were known before ``week``."""
    return list(range(1, int(week)))


def _order_missing(names: list[str]) -> list[str]:
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    ranked = [name for name in _MISSING_ORDER if name in seen]
    ranked.extend(name for name in seen if name not in _MISSING_ORDER)
    return ranked


def _me_mae(pred: list[float], actual: list[float]) -> tuple[float, float]:
    n = len(pred)
    if n == 0:
        return 0.0, 0.0
    err = [pred[i] - actual[i] for i in range(n)]
    me = sum(err) / n
    mae = sum(abs(e) for e in err) / n
    return me, mae


def summarize_errors(
    paired: list[tuple[str, float, float, float]],
    *,
    season: int,
    week: int,
    missing: list[str],
) -> BacktestReport:
    """``paired`` is ``(position, board, sim, actual)``.

    Mean error is prediction minus actual (positive = too high).
    """
    buckets: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for pos, board, sim, actual in paired:
        buckets[_position(pos)].append((board, sim, actual))
    rows: list[PosError] = []
    all_board: list[float] = []
    all_sim: list[float] = []
    all_act: list[float] = []

    def _add(pos: str, chunk: list[tuple[float, float, float]]) -> None:
        board = [c[0] for c in chunk]
        sim = [c[1] for c in chunk]
        actual = [c[2] for c in chunk]
        b_me, b_mae = _me_mae(board, actual)
        s_me, s_mae = _me_mae(sim, actual)
        rows.append(
            PosError(
                position=pos,
                n=len(chunk),
                board_me=round(b_me, 4),
                board_mae=round(b_mae, 4),
                sim_me=round(s_me, 4),
                sim_mae=round(s_mae, 4),
            )
        )
        all_board.extend(board)
        all_sim.extend(sim)
        all_act.extend(actual)

    for pos in _POS_ORDER:
        chunk = buckets.get(pos) or []
        if chunk:
            _add(pos, chunk)
    for pos, chunk in buckets.items():
        if pos in _POS_ORDER or not chunk:
            continue
        _add(pos, chunk)
    n = len(all_board)
    if all_board:
        b_me, b_mae = _me_mae(all_board, all_act)
        s_me, s_mae = _me_mae(all_sim, all_act)
        rows.append(
            PosError(
                position="ALL",
                n=n,
                board_me=round(b_me, 4),
                board_mae=round(b_mae, 4),
                sim_me=round(s_me, 4),
                sim_mae=round(s_mae, 4),
            )
        )
    return BacktestReport(
        season=int(season),
        week=int(week),
        missing=tuple(_order_missing(list(missing))),
        rows=tuple(rows),
        n=n,
    )


def _played(row: dict) -> bool:
    """True when a counting stat is positive, or the row has none of them."""
    present: list[str] = []
    for key in _COUNT_KEYS:
        if key in row and row[key] not in (None, ""):
            present.append(key)
    if not present:
        return True
    for key in present:
        try:
            if float(row[key]) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


_STARTER_RANKS = {
    "QB": frozenset({1}),
    "RB": frozenset({1}),
    "TE": frozenset({1}),
    "WR": frozenset({1, 2, 3}),
}


def _offense_snaps(row: dict) -> float | None:
    """Offensive snaps when the column exists. None when it does not."""
    for key in ("offense_snaps", "offensive_snaps", "snaps"):
        if key not in row or row[key] in (None, ""):
            continue
        try:
            return float(row[key])
        except (TypeError, ValueError):
            return None
    return None


def _qb_play_volume(row: dict) -> float:
    """Snaps when the weekly row has them, otherwise pass attempts."""
    snaps = _offense_snaps(row)
    if snaps is not None:
        return snaps
    raw = row.get("pass_attempts")
    if raw in (None, ""):
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def hindsight_qb_pids(
    players: list[Player],
    indexed: dict[tuple[str, str], dict],
) -> set[str]:
    """The QB with the most offensive snaps on each team. Ties break on pid."""
    by_team: dict[str, list[tuple[float, str]]] = {}
    for pl in players:
        if (pl.position or "").upper() != "QB":
            continue
        info = indexed.get(((pl.team or "").upper(), match_key(pl.name)))
        if info is None:
            continue
        volume = _qb_play_volume(info["row"])
        if volume <= 0:
            continue
        by_team.setdefault((pl.team or "").upper(), []).append((volume, pl.pid))
    out: set[str] = set()
    for rows in by_team.values():
        rows.sort(key=lambda item: (-item[0], item[1]))
        out.add(rows[0][1])
    return out


def is_hindsight_starter(player: Player, row: dict, qb_pids: set[str]) -> bool:
    """Who played, with the actual QB1 by snaps.

    Pre-game depth still picks RB, TE, and the top three WRs. The QB is the
    teammate who took the snaps, including a backup behind a questionable
    starter who sat. Questionable players are not handed off before the fact.
    """
    pos = (player.position or "").upper()
    if pos in {"D", "DEF"}:
        return True
    if pos == "QB":
        return player.pid in qb_pids
    return is_starter(player, row)


def is_starter(player: Player, row: dict) -> bool:
    """Effective chart who played, plus every DEF.

    QB, RB, and TE are depth 1 after an O/D/IR/NA handoff. WR is the top
    three. A promoted backup counts.
    """
    pos = (player.position or "").upper()
    if pos in {"D", "DEF"}:
        return True
    ranks = _STARTER_RANKS.get(pos)
    if ranks is None or player.depth_rank not in ranks:
        return False
    return _played(row)


def index_actuals(rows: list[dict]) -> dict[tuple[str, str], float]:
    """``(team_fd, match_key) → fd_points``. Later rows overwrite."""
    return {key: info["fd_points"] for key, info in index_actual_rows(rows).items()}


def index_actual_rows(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """``(team_fd, match_key) → {fd_points, row}``. Later rows overwrite."""
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = row
        if item.get("fd_points") is None or not item.get("team_fd") or not item.get("player_name"):
            parsed = parse_player_stat_row(row)
            if parsed is None:
                continue
            item = parsed
        fd = item.get("fd_points")
        team = item.get("team_fd")
        name = item.get("player_name")
        if fd is None or not team or not name:
            continue
        out[(str(team).upper(), match_key(str(name)))] = {
            "fd_points": float(fd),
            "row": row,
        }
    return out


def _join_line_rows(
    players: list[Player], rows: list[dict]
) -> tuple[list[Player], bool]:
    """Attach rows when they cover the slate. Failure leaves players unchanged."""
    if not rows:
        return players, False
    probe: list[str] = []
    joined = _apply_lines(players, rows, probe)
    if probe or not any(pl.implied_total for pl in joined):
        return players, False
    return joined, True


def pair_projections(
    players: list[Player],
    actuals: dict[tuple[str, str], float],
    sim_by_pid: dict,
) -> list[tuple[str, float, float, float]]:
    paired: list[tuple[str, float, float, float]] = []
    for pl in players:
        key = ((pl.team or "").upper(), match_key(pl.name))
        if key not in actuals:
            continue
        st = sim_by_pid.get(pl.pid)
        if st is None:
            continue
        paired.append(
            (_position(pl.position), float(score_player(pl)), float(st.mean), actuals[key])
        )
    return paired


def _try_rows(label: str, fn, missing: list[str]) -> list[dict]:
    try:
        rows, _meta = fn()
    except (
        GangstashDataKeyMissing,
        GangstashKeyMissing,
        GangstashTruncated,
        GangstashDataError,
        GangstashError,
    ):
        missing.append(label)
        return []
    if not isinstance(rows, list) or not rows:
        missing.append(label)
        return []
    return [row for row in rows if isinstance(row, dict)]


def rows_for_season_week(rows: list[dict], season: int, week: int) -> list[dict]:
    """Keep rows for this season and week.

    A payload whose rows have no ``season`` or ``week`` is already the
    response for that query, so those rows stay. Mixed payloads drop the
    other weeks.
    """
    dated: list[dict] = []
    undated: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_season = row.get("season")
        raw_week = row.get("week")
        if raw_season in (None, "") and raw_week in (None, ""):
            undated.append(row)
            continue
        try:
            if raw_season not in (None, "") and int(raw_season) != int(season):
                continue
            if raw_week not in (None, "") and int(raw_week) != int(week):
                continue
        except (TypeError, ValueError):
            continue
        dated.append(row)
    return dated or undated


def earliest_kickoff(rows: list[dict]) -> datetime | None:
    stamps = [
        stamp
        for row in rows
        if (stamp := parse_stamp(row.get("commence_time"))) is not None
    ]
    return min(stamps) if stamps else None


def _apply_lines(players: list[Player], rows: list[dict], missing: list[str]) -> list[Player]:
    if not rows:
        return players
    slate = slate_from_players(players)
    if not slate:
        if "game_lines" not in missing:
            missing.append("game_lines")
        return players
    try:
        by_team = team_lines_from_gangstash(
            map_game_lines(rows),
            slate,
            start=_WIDE_START,
            end=_WIDE_END,
        )
    except LinesError:
        if "game_lines" not in missing:
            missing.append("game_lines")
        return players
    if not by_team:
        if "game_lines" not in missing:
            missing.append("game_lines")
        return players
    return attach_team_lines(players, by_team)


def _apply_props(
    players: list[Player],
    rows: list[dict],
    missing: list[str],
    *,
    season: int,
    week: int,
    kickoff: datetime | None,
) -> list[Player]:
    scoped = props_for_week(rows, season=season, week=week, kickoff=kickoff)
    if not scoped:
        if "props" not in missing:
            missing.append("props")
        return players
    by_key, _meta = rows_to_props(scoped)
    if not by_key:
        if "props" not in missing:
            missing.append("props")
        return players
    buckets: dict[str, list[Player]] = defaultdict(list)
    for pl in players:
        if pl.position == "D":
            continue
        buckets[match_key(pl.name)].append(pl)
    joined = {}
    for key, prop in by_key.items():
        hits = buckets.get(key) or []
        pids = {pl.pid: pl for pl in hits}
        if len(pids) == 1:
            joined[next(iter(pids))] = prop
    if not joined:
        if "props" not in missing:
            missing.append("props")
        return players
    return attach_props(players, joined)


def _depth_table(rows: list[dict], *, season: int, week: int) -> tuple[list[DepthRow], str]:
    scoped, note = scope_depth_rows(rows, season=season, week=week)
    if not scoped:
        return [], note
    try:
        slots = map_depth_slots(scoped)
    except GangstashDataError:
        return [], note
    out: list[DepthRow] = []
    for slot in slots:
        pos = skill_pos(slot.position)
        if pos is None:
            continue
        out.append(
            DepthRow(
                team=slot.team_fd,
                pos=pos,
                rank=slot.rank,
                name=slot.player_name,
                source_url="gangstash",
                fetched_at="",
            )
        )
    return out, note


def apply_week_context(
    players: list[Player],
    *,
    season: int,
    week: int,
    line_rows: list[dict] | None = None,
    depth_raw: list[dict] | None = None,
    injury_raw: list[dict] | None = None,
    target_rows: list | None = None,
    snap_rows: list | None = None,
    prop_rows: list[dict] | None = None,
    kickoff: datetime | None = None,
    lines_attached: bool = False,
    depth_note: str | None = None,
) -> tuple[list[Player], list[str], list[str]]:
    """Join the same overlays the optimizer uses. Empty bags are missing.

    ``lines_attached`` means a ``--lines-file`` already set implied totals.
    Injury codes come from the FanDuel CSV. ``injury_raw`` is used only
    when a gangstash injuries payload was actually returned.
    """
    gaps: list[str] = []
    notes: list[str] = []
    if not lines_attached:
        scoped_lines = rows_for_season_week(line_rows or [], season, week)
        if scoped_lines:
            players = _apply_lines(players, scoped_lines, gaps)
            if kickoff is None:
                kickoff = earliest_kickoff(scoped_lines)
        elif "game_lines" not in gaps:
            gaps.append("game_lines")

    if injury_raw:
        parsed = injury_rows_from_records(injury_raw, season=season, week=week)
        if parsed:
            players = stamp_injuries(players, parsed)

    chart, note = _depth_table(depth_raw or [], season=season, week=week)
    if depth_note:
        note = depth_note
    elif note.startswith("depth chart has no week column"):
        note = f"depth: current chart (no cached snapshot before week {int(week)})"
    if chart:
        players, _stats = attach_depth_ranks(
            players,
            chart,
            score_fn=lambda pl, rank: score_player(pl, rank),
            source="gangstash",
        )
    else:
        gaps.append("depth")
    players = handoff_chart(players)
    if note:
        notes.append(note)

    if target_rows:
        players, _stats = attach_targets(players, target_rows)
    else:
        gaps.append("targets")
    if snap_rows:
        players, _stats = attach_snaps(players, snap_rows)
    else:
        gaps.append("snaps")

    players = _apply_props(
        players,
        prop_rows or [],
        gaps,
        season=season,
        week=week,
        kickoff=kickoff,
    )
    return players, gaps, notes


def run_backtest(
    players: list[Player],
    actual_rows: list[dict],
    *,
    season: int,
    week: int,
    sim_inputs: SimInputs | None = None,
    missing: list[str] | None = None,
    notes: list[str] | None = None,
    n: int = 400,
    seed: int = 1,
    starters_only: bool = False,
    efficiency: str = "data",
) -> BacktestReport:
    """Score ``players`` against weekly ``fd_points``. No network."""
    noted = _order_missing(list(missing or []))
    indexed = index_actual_rows(actual_rows)
    dnp = _questionable_dnps(players, indexed)
    notes = list(notes or ())
    notes.append(f"questionable DNP: {dnp} (projection kept)")
    notes.append(f"sim efficiency: {efficiency}")
    model = build_efficiency(efficiency, sim_inputs, before_week=week)
    qb_pids = hindsight_qb_pids(players, indexed)
    if not indexed and "player_stats_weekly" not in noted:
        noted = _order_missing(noted + ["player_stats_weekly"])
    game_sim = simulate_games(
        players,
        n=max(1, int(n)),
        seed=int(seed),
        inputs=sim_inputs,
        efficiency=model,
    )
    full: list[tuple[str, float, float, float]] = []
    starters: list[tuple[str, float, float, float]] = []
    hindsight: list[tuple[str, float, float, float]] = []
    for pl in players:
        key = ((pl.team or "").upper(), match_key(pl.name))
        info = indexed.get(key)
        if info is None:
            continue
        st = game_sim.by_pid.get(pl.pid)
        if st is None:
            continue
        row = (
            _position(pl.position),
            float(score_player(pl)),
            float(st.mean),
            float(info["fd_points"]),
        )
        full.append(row)
        if is_starter(pl, info["row"]):
            starters.append(row)
        if is_hindsight_starter(pl, info["row"], qb_pids):
            hindsight.append(row)
    full_report = summarize_errors(full, season=season, week=week, missing=noted)
    starter_report = summarize_errors(starters, season=season, week=week, missing=noted)
    hindsight_report = summarize_errors(hindsight, season=season, week=week, missing=noted)
    if starters_only:
        return BacktestReport(
            season=full_report.season,
            week=full_report.week,
            missing=full_report.missing,
            rows=starter_report.rows,
            n=starter_report.n,
            starters=starter_report.rows,
            starters_n=starter_report.n,
            hindsight=hindsight_report.rows,
            hindsight_n=hindsight_report.n,
            pool="starters",
            notes=tuple(notes or ()),
            show_hindsight=True,
        )
    return BacktestReport(
        season=full_report.season,
        week=full_report.week,
        missing=full_report.missing,
        rows=full_report.rows,
        n=full_report.n,
        starters=starter_report.rows,
        starters_n=starter_report.n,
        hindsight=hindsight_report.rows,
        hindsight_n=hindsight_report.n,
        pool="full",
        notes=tuple(notes or ()),
        show_hindsight=True,
    )


def _questionable_dnps(
    players: list[Player],
    indexed: dict[tuple[str, str], dict],
) -> int:
    """Q who has no stat row, or 0 offensive snaps. The projection stays.

    A missing row is a DNP. When the row has no snap column, a row with no
    positive counting stat is a DNP too.
    """
    n = 0
    for pl in players:
        if (pl.injury or "").strip().upper() != "Q":
            continue
        info = indexed.get(((pl.team or "").upper(), match_key(pl.name)))
        if info is None:
            n += 1
            continue
        snaps = _offense_snaps(info["row"])
        if snaps is not None:
            if snaps <= 0:
                n += 1
            continue
        if not _played(info["row"]):
            n += 1
    return n


def _load_usage(
    players: list[Player],
    *,
    season: int,
    week: int,
    missing: list[str],
) -> tuple[list, list]:
    """Prior-week targets and snaps. Week 1 has none."""
    window = prior_weeks(week)
    if not window:
        missing.append("targets")
        missing.append("snaps")
        return [], []
    targets: list = []
    snaps: list = []
    try:
        targets, _meta = load_optimizer_targets(
            source="gangstash",
            csv_path=Path("unused.csv"),
            week=None,
            weeks=window,
            season=season,
        )
    except (
        TargetsError,
        GangstashDataKeyMissing,
        GangstashTruncated,
        GangstashDataError,
        GangstashError,
        UnmappedTeam,
    ):
        missing.append("targets")
        targets = []
    try:
        snaps, _meta = load_optimizer_snaps(
            source="gangstash",
            csv_path=Path("unused.csv"),
            week=None,
            weeks=window,
            season=season,
        )
    except (
        SnapsError,
        GangstashDataKeyMissing,
        GangstashTruncated,
        GangstashDataError,
        GangstashError,
        UnmappedTeam,
    ):
        missing.append("snaps")
        snaps = []
    if not targets and "targets" not in missing:
        missing.append("targets")
    if not snaps and "snaps" not in missing:
        missing.append("snaps")
    return targets, snaps


def _fetch_rows(fn) -> tuple[list[dict], str]:
    """``(rows, error)``. An empty payload is rows ``[]`` and error ``""``."""
    try:
        rows, _meta = fn()
    except (
        GangstashDataKeyMissing,
        GangstashKeyMissing,
        GangstashTruncated,
        GangstashDataError,
        GangstashError,
    ) as e:
        return [], str(e)
    if not isinstance(rows, list):
        return [], ""
    return [row for row in rows if isinstance(row, dict)], ""


def _load_live(
    players: list[Player],
    *,
    season: int,
    week: int,
    lines_file: Path | None = None,
    allow_missing_lines: bool = False,
) -> tuple[list[Player], list[dict], SimInputs | None, list[str], list[str]]:
    missing: list[str] = []
    notes: list[str] = []
    try:
        actual_rows, _meta = fetch_player_stats_weekly(season=season, week=week)
    except (
        GangstashDataKeyMissing,
        GangstashKeyMissing,
        GangstashTruncated,
        GangstashDataError,
        GangstashError,
    ) as e:
        raise GangstashDataError(str(e)) from e
    if not actual_rows:
        missing.append("player_stats_weekly")

    lines_attached = False
    kickoff: datetime | None = None
    line_rows: list[dict] = []
    closing_unknown = False
    if lines_file is not None:
        slate = slate_from_players(players)
        try:
            by_team = load_lines_file(
                lines_file,
                slate,
                start=_WIDE_START,
                end=_WIDE_END,
                season=season,
                week=week,
            )
        except (LinesError, OSError, ValueError) as e:
            raise LinesError(str(e)) from e
        players = attach_team_lines(players, by_team)
        lines_attached = True
        stamps = [
            parse_stamp(line.commence_time)
            for line in by_team.values()
            if line.commence_time
        ]
        stamps = [stamp for stamp in stamps if stamp is not None]
        if stamps:
            kickoff = min(stamps)
    else:
        closing, closing_err = _fetch_rows(
            lambda: fetch_closing_lines(season=season, week=week)
        )
        if closing_err and "unknown dataset" in closing_err.lower():
            closing_unknown = True
            print(
                "closing_lines: not available (Unknown dataset)",
                file=sys.stderr,
            )
            notes.append("closing_lines: not available (Unknown dataset)")
        if closing_err or not closing:
            missing.append("closing_lines")
        closing = rows_for_season_week(closing, season, week)
        joined, ok = _join_line_rows(players, closing)
        if ok:
            players = joined
            lines_attached = True
            kickoff = earliest_kickoff(closing)
            line_rows = closing
            missing = [name for name in missing if name != "closing_lines"]
        else:
            if "closing_lines" not in missing:
                missing.append("closing_lines")
            game, game_err = _fetch_rows(
                lambda: fetch_game_lines(season=season, week=week)
            )
            if game_err or not game:
                missing.append("game_lines")
            game = rows_for_season_week(game, season, week)
            joined, ok = _join_line_rows(players, game)
            if ok:
                players = joined
                lines_attached = True
                kickoff = earliest_kickoff(game)
                line_rows = game
                missing = [name for name in missing if name != "game_lines"]
            elif "game_lines" not in missing:
                missing.append("game_lines")
        if not lines_attached and not allow_missing_lines:
            detail = "no lines for this slate"
            if closing_unknown:
                detail += "; closing_lines: not available (Unknown dataset)"
            detail += "; pass --lines-file or --allow-missing-lines"
            raise LinesError(detail)

    injury_raw, injury_err = _fetch_rows(
        lambda: fetch_week_injuries(season=season, week=week)
    )
    if injury_err:
        injury_raw = []
    depth_fetched, _depth_err = _fetch_rows(
        lambda: fetch_depth_charts(season=season, week=week)
    )
    depth_raw, depth_note = depth_rows_for_backtest(
        depth_fetched,
        season=season,
        week=week,
        kickoff=kickoff,
    )
    target_rows, snap_rows = _load_usage(
        players, season=season, week=week, missing=missing
    )
    prop_rows = _try_rows(
        "props",
        lambda: fetch_props(season=season, week=week),
        missing,
    )
    players, gaps, context_notes = apply_week_context(
        players,
        season=season,
        week=week,
        line_rows=line_rows,
        depth_raw=depth_raw,
        injury_raw=injury_raw,
        target_rows=target_rows,
        snap_rows=snap_rows,
        prop_rows=prop_rows,
        kickoff=kickoff or earliest_kickoff(line_rows),
        lines_attached=lines_attached,
        depth_note=depth_note,
    )
    notes.extend(context_notes)
    missing.extend(gaps)
    window = prior_weeks(week)
    try:
        sim_inputs, note = resolve_sim_inputs(
            path=None,
            season=season,
            weeks=window,
        )
    except SimInputError as e:
        print(f"sim inputs: {e}", file=sys.stderr)
        sim_inputs = None
        note = ""
    if sim_inputs is None:
        missing.append("sim_inputs")
    elif note:
        print(note, file=sys.stderr)
    return players, list(actual_rows or []), sim_inputs, _order_missing(missing), notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="FanDuel players-list CSV")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--n", type=int, default=400, help="sim draws (default 400)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument(
        "--lines-file",
        default=None,
        help="CSV or JSON historical lines (FanDuel or nflverse columns). "
        "Wins over closing_lines and game_lines.",
    )
    ap.add_argument(
        "--allow-missing-lines",
        action="store_true",
        help="score even when no closing, game, or file lines joined",
    )
    ap.add_argument(
        "--starters-only",
        action="store_true",
        help="score QB/RB/TE depth 1 and WR depth 1-3 who played, plus DEF",
    )
    ap.add_argument(
        "--sim-efficiency",
        choices=("placeholder", "data"),
        default="data",
        help="layer-4 efficiency: data uses prior-week gangstash rates "
        "(default); placeholder keeps the league-average rates",
    )
    args = ap.parse_args(argv)
    if args.week < 1 or args.season < 1:
        print("choke BACKTEST: season and week must be >= 1", file=sys.stderr)
        return 1
    try:
        players = load_fanduel_csv(Path(args.csv))
    except (OSError, ValueError) as e:
        print(f"choke CSV_FANDUEL: {e}", file=sys.stderr)
        return 1
    lines_path = Path(args.lines_file).expanduser() if args.lines_file else None
    try:
        players, actual_rows, sim_inputs, missing, notes = _load_live(
            players,
            season=args.season,
            week=args.week,
            lines_file=lines_path,
            allow_missing_lines=args.allow_missing_lines,
        )
    except LinesError as e:
        print(f"choke LINES: {e}", file=sys.stderr)
        return 1
    except (
        GangstashDataKeyMissing,
        GangstashKeyMissing,
        GangstashTruncated,
        GangstashDataError,
        GangstashError,
    ) as e:
        print(f"choke PLAYER_STATS_WEEKLY: {e}", file=sys.stderr)
        return 1
    report = run_backtest(
        players,
        actual_rows,
        season=args.season,
        week=args.week,
        sim_inputs=sim_inputs,
        missing=missing,
        notes=notes,
        n=args.n,
        seed=args.seed,
        starters_only=args.starters_only,
        efficiency=args.sim_efficiency,
    )
    print(report.to_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
