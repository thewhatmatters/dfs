"""Board vs sim error against gangstash ``player_stats_weekly`` ``fd_points``.

```
python3 -m nfl.backtest --csv nfl/data/<players-list>.csv --season 2026 --week 2
```

Reads ``dataset=player_stats_weekly&season=&week=`` through
``nfl.gangstash_data.fetch_player_stats_weekly`` (header ``x-api-key``,
same-day cache). Game lines, props, and the sim feed are optional. An
empty board is reported and the week still scores with whatever is left.
``--projection-source`` stays ``board`` on the optimizer; this command
only compares the two.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from nfl.gangstash import (
    GangstashDataError,
    GangstashDataKeyMissing,
    GangstashError,
    GangstashKeyMissing,
    GangstashTruncated,
    fetch_props,
)
from nfl.gangstash_data import (
    fetch_game_lines,
    fetch_player_stats_weekly,
    map_game_lines,
    parse_player_stat_row,
)
from nfl.lines import LinesError, slate_from_players, team_lines_from_gangstash
from nfl.names import match_key
from nfl.players import Player, load_fanduel_csv
from nfl.projections import attach_team_lines, score_player
from nfl.props import attach_props, rows_to_props
from nfl.sim import simulate_games
from nfl.sim_feed import resolve_sim_inputs
from nfl.sim_inputs import SimInputError, SimInputs

_POS_ORDER = ("QB", "RB", "WR", "TE", "DEF")


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

    def to_text(self) -> str:
        lines = [
            f"backtest season {self.season} week {self.week}  n {self.n}",
        ]
        if self.missing:
            lines.append("missing: " + ", ".join(self.missing))
        else:
            lines.append("missing: none")
        lines.append(
            f"{'pos':<5} {'n':>4} {'board_me':>9} {'board_mae':>10} "
            f"{'sim_me':>8} {'sim_mae':>8}"
        )
        if not self.rows:
            lines.append("(no joined players)")
            return "\n".join(lines)
        for row in self.rows:
            lines.append(
                f"{row.position:<5} {row.n:4d} {row.board_me:9.2f} "
                f"{row.board_mae:10.2f} {row.sim_me:8.2f} {row.sim_mae:8.2f}"
            )
        return "\n".join(lines)


def _position(raw: str) -> str:
    pos = (raw or "").upper()
    if pos in {"D", "DST"}:
        return "DEF"
    return pos or "?"


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
    for pos in _POS_ORDER:
        chunk = buckets.get(pos) or []
        if not chunk:
            continue
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
    for pos, chunk in buckets.items():
        if pos in _POS_ORDER or not chunk:
            continue
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
    if all_board:
        b_me, b_mae = _me_mae(all_board, all_act)
        s_me, s_mae = _me_mae(all_sim, all_act)
        rows.append(
            PosError(
                position="ALL",
                n=len(all_board),
                board_me=round(b_me, 4),
                board_mae=round(b_mae, 4),
                sim_me=round(s_me, 4),
                sim_mae=round(s_mae, 4),
            )
        )
    return BacktestReport(
        season=int(season),
        week=int(week),
        missing=tuple(missing),
        rows=tuple(rows),
        n=len(all_board),
    )


def index_actuals(rows: list[dict]) -> dict[tuple[str, str], float]:
    """``(team_fd, match_key) → fd_points``. Later rows overwrite."""
    out: dict[tuple[str, str], float] = {}
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
        out[(str(team).upper(), match_key(str(name)))] = float(fd)
    return out


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
    except (GangstashDataKeyMissing, GangstashKeyMissing, GangstashTruncated, GangstashDataError, GangstashError):
        missing.append(label)
        return []
    if not isinstance(rows, list) or not rows:
        missing.append(label)
        return []
    return [row for row in rows if isinstance(row, dict)]


def _apply_lines(players: list[Player], rows: list[dict], missing: list[str]) -> list[Player]:
    if not rows:
        return players
    slate = slate_from_players(players)
    if not slate:
        missing.append("game_lines")
        return players
    # Historical weeks sit outside today's slate window. Accept any commence.
    start_dt = datetime(2000, 1, 1, tzinfo=timezone.utc)
    end_dt = datetime(2100, 1, 1, tzinfo=timezone.utc)
    try:
        by_team = team_lines_from_gangstash(
            map_game_lines(rows),
            slate,
            start=start_dt,
            end=end_dt,
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


def _apply_props(players: list[Player], rows: list[dict], missing: list[str]) -> list[Player]:
    if not rows:
        return players
    by_key, _meta = rows_to_props(rows)
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


def run_backtest(
    players: list[Player],
    actual_rows: list[dict],
    *,
    season: int,
    week: int,
    sim_inputs: SimInputs | None = None,
    missing: list[str] | None = None,
    n: int = 400,
    seed: int = 1,
) -> BacktestReport:
    """Score ``players`` against weekly ``fd_points``. No network."""
    noted = list(missing or [])
    actuals = index_actuals(actual_rows)
    if not actuals:
        if "player_stats_weekly" not in noted:
            noted.append("player_stats_weekly")
    game_sim = simulate_games(
        players,
        n=max(1, int(n)),
        seed=int(seed),
        inputs=sim_inputs,
    )
    paired = pair_projections(players, actuals, game_sim.by_pid)
    return summarize_errors(paired, season=season, week=week, missing=noted)


def _load_live(
    players: list[Player],
    *,
    season: int,
    week: int,
) -> tuple[list[Player], list[dict], SimInputs | None, list[str]]:
    missing: list[str] = []
    try:
        actual_rows, _meta = fetch_player_stats_weekly(season=season, week=week)
    except (GangstashDataKeyMissing, GangstashKeyMissing, GangstashTruncated, GangstashDataError, GangstashError) as e:
        raise GangstashDataError(str(e)) from e
    if not actual_rows:
        missing.append("player_stats_weekly")
    line_rows = _try_rows(
        "game_lines",
        lambda: fetch_game_lines(season=season, week=week),
        missing,
    )
    prop_rows = _try_rows("props", fetch_props, missing)
    players = _apply_lines(players, line_rows, missing)
    players = _apply_props(players, prop_rows, missing)
    prior = list(range(1, int(week))) or [int(week)]
    try:
        sim_inputs, note = resolve_sim_inputs(
            path=None,
            season=season,
            weeks=prior,
        )
    except SimInputError as e:
        print(f"sim inputs: {e}", file=sys.stderr)
        sim_inputs = None
        note = ""
    if sim_inputs is None:
        missing.append("sim_inputs")
    elif note:
        print(note, file=sys.stderr)
    return players, list(actual_rows or []), sim_inputs, missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="FanDuel players-list CSV")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--n", type=int, default=400, help="sim draws (default 400)")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    if args.week < 1 or args.season < 1:
        print("choke BACKTEST: season and week must be >= 1", file=sys.stderr)
        return 1
    try:
        players = load_fanduel_csv(Path(args.csv))
    except (OSError, ValueError) as e:
        print(f"choke CSV_FANDUEL: {e}", file=sys.stderr)
        return 1
    try:
        players, actual_rows, sim_inputs, missing = _load_live(
            players, season=args.season, week=args.week
        )
    except (GangstashDataKeyMissing, GangstashKeyMissing, GangstashTruncated, GangstashDataError, GangstashError) as e:
        print(f"choke PLAYER_STATS_WEEKLY: {e}", file=sys.stderr)
        return 1
    report = run_backtest(
        players,
        actual_rows,
        season=args.season,
        week=args.week,
        sim_inputs=sim_inputs,
        missing=missing,
        n=args.n,
        seed=args.seed,
    )
    print(report.to_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
