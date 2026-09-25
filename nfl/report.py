"""Nightly Monte Carlo projections report.

``build_report`` is pure. ``write_report`` writes
``nfl/reports/<season>-w<week>-<YYYY-MM-DD>.md`` (America/Chicago date).
A sidecar ``<season>-w<week>-games.json`` stores sim team medians and
p10/p90. The CLI reads stored ``model=sim`` rows and uses that sidecar
when ``run_at`` matches.

    python3 -m nfl.report --season 2026 --week 3
    python3 -m nfl.report --season 2026 --week 3 --csv nfl/data/players.csv

Game draws are ``GameSim.game_draws``: one
``(game, away, home, away_pts, home_pts)`` row per game per draw.
Missing draws fall back to gangstash ``game_lines`` implied totals.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nfl.names import match_key

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
RUN_AT_TOLERANCE = timedelta(minutes=5)
MAX_WIDTH = 100
POSITIONS = (
    ("QB", "QB TOP 5", 5),
    ("RB", "RB TOP 10", 10),
    ("WR", "WR TOP 10", 10),
    ("TE", "TE TOP 10", 10),
    ("D", "DEF TOP 5", 5),
)
VEGAS_LABEL = "Vegas implied totals (not sim)"
DASH = "—"


def graph_table(
    title: str,
    headers: list[str],
    rows: list[list[object]],
    *,
    aligns: list[str] | None = None,
    footer: list[object] | None = None,
    shrink: tuple[int, ...] = (),
    max_width: int = MAX_WIDTH,
) -> str:
    """mdxcn GraphTable ASCII frame. Every line the same width, at most ``max_width``."""
    n = len(headers)
    if n == 0:
        raise ValueError("graph table needs a header")
    if aligns is None:
        aligns = ["left"] + ["right"] * (n - 1)
    if len(aligns) != n:
        raise ValueError("align count does not match headers")

    def cell(value: object) -> str:
        if value is None:
            return ""
        return str(value)

    matrix = [[cell(h) for h in headers]]
    for row in rows:
        matrix.append([cell(c) for c in row])
    foot = [cell(c) for c in footer] if footer is not None else None

    def col_widths() -> list[int]:
        widths = [0] * n
        groups = matrix + ([foot] if foot is not None else [])
        for group in groups:
            for i, text in enumerate(group):
                widths[i] = max(widths[i], len(text))
        return widths

    def line_width(widths: list[int]) -> int:
        return sum(widths) + 3 * n + 1

    def clip(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        if limit <= 1:
            return text[:limit]
        return text[: limit - 1] + "…"

    overflow = line_width(col_widths()) - max_width
    if overflow > 0 and shrink:
        for index in shrink:
            widths = col_widths()
            overflow = line_width(widths) - max_width
            if overflow <= 0:
                break
            floor = max(len(matrix[0][index]), 4)
            room = widths[index] - floor
            if room <= 0:
                continue
            take = min(room, overflow)
            limit = widths[index] - take
            for group in matrix:
                group[index] = clip(group[index], limit)
            if foot is not None:
                foot[index] = clip(foot[index], limit)

    widths = col_widths()

    def align_cell(text: str, index: int) -> str:
        width = widths[index]
        if aligns[index] == "right":
            return text.rjust(width)
        return text.ljust(width)

    def fmt(row: list[str]) -> str:
        return "| " + " | ".join(align_cell(text, i) for i, text in enumerate(row)) + " |"

    def rule() -> str:
        if n == 1:
            chunks = ["-" * widths[0]]
        else:
            chunks = []
            for i, width in enumerate(widths):
                pad = 1 if i == 0 or i == n - 1 else 2
                chunks.append("-" * (width + pad))
        return "| " + "+".join(chunks) + " |"

    body = [fmt(matrix[0]), rule()]
    for row in matrix[1:]:
        body.append(fmt(row))
    if foot is not None:
        body.append(rule())
        body.append(fmt(foot))

    width = max(len(line) for line in body)
    label = f" [ {title} ] "
    if len(label) + 2 > max_width:
        room = max(1, max_width - 2 - 6)
        label = f" [ {title[:room]} ] "
    width = max(width, len(label) + 2)
    if width > max_width:
        width = max(len(line) for line in body)

    def pad(line: str) -> str:
        if len(line) > width:
            raise ValueError(f"table line is {len(line)} chars")
        if len(line) == width:
            return line
        return line[:-1] + (" " * (width - len(line))) + "|"

    body = [pad(line) for line in body]
    inner = width - 2
    left = max(0, (inner - len(label)) // 2)
    right = max(0, inner - len(label) - left)
    top = "+" + ("-" * left) + label + ("-" * right) + "+"
    if len(top) < width:
        top = top[:-1] + ("-" * (width - len(top))) + "+"
    blank = "|" + (" " * (width - 2)) + "|"
    bottom = "+" + ("-" * (width - 2)) + "+"
    framed = "\n".join([top, blank, *body, blank, bottom])
    return framed


def _percentile(sorted_xs: list[float], p: float) -> float:
    n = len(sorted_xs)
    if n == 1:
        return sorted_xs[0]
    idx = p * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    weight = idx - lo
    return sorted_xs[lo] * (1.0 - weight) + sorted_xs[hi] * weight


def _band(values: list[float]) -> tuple[float, float, float]:
    ordered = sorted(values)
    return (
        _percentile(ordered, 0.10),
        _percentile(ordered, 0.50),
        _percentile(ordered, 0.90),
    )


def summarize_game_draws(game_draws) -> list[dict]:
    """Collapse ``(game, away, home, away_pts, home_pts)`` rows into team bands."""
    buckets: dict[tuple[str, str, str], tuple[list[float], list[float]]] = {}
    order: list[tuple[str, str, str]] = []
    for row in game_draws or ():
        if not isinstance(row, (tuple, list)) or len(row) != 5:
            continue
        game, away, home, away_pts, home_pts = row
        if not away or not home:
            continue
        try:
            away_val = float(away_pts)
            home_val = float(home_pts)
        except (TypeError, ValueError):
            continue
        key = (str(game), str(away), str(home))
        slot = buckets.get(key)
        if slot is None:
            slot = ([], [])
            buckets[key] = slot
            order.append(key)
        slot[0].append(away_val)
        slot[1].append(home_val)
    games: list[dict] = []
    for key in order:
        away_pts, home_pts = buckets[key]
        if not away_pts or not home_pts:
            continue
        away_p10, away_p50, away_p90 = _band(away_pts)
        home_p10, home_p50, home_p90 = _band(home_pts)
        game, away, home = key
        games.append(
            {
                "game": game,
                "away": away,
                "home": home,
                "source": "sim",
                "away_median": round(away_p50, 2),
                "away_p10": round(away_p10, 2),
                "away_p90": round(away_p90, 2),
                "home_median": round(home_p50, 2),
                "home_p10": round(home_p10, 2),
                "home_p90": round(home_p90, 2),
            }
        )
    return games


def vegas_games_from_lines(rows: list[dict]) -> list[dict]:
    """Implied team totals from gangstash ``game_lines``. No simulated bands."""
    from nfl.lines import implied_totals
    from nfl.teams import UnmappedTeam, require_fd

    games: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            home = require_fd(str(row.get("home_team_fd") or "")).fd
            away = require_fd(str(row.get("away_team_fd") or "")).fd
        except UnmappedTeam:
            continue
        spread = row.get("spread")
        total = row.get("total")
        if spread is None or total is None or spread == "" or total == "":
            continue
        implied_home, implied_away = implied_totals(float(total), float(spread))
        game_id = row.get("game_id")
        games.append(
            {
                "game": "" if game_id is None else str(game_id),
                "away": away,
                "home": home,
                "source": "vegas",
                "away_median": round(float(implied_away), 2),
                "away_p10": None,
                "away_p90": None,
                "home_median": round(float(implied_home), 2),
                "home_p10": None,
                "home_p90": None,
            }
        )
    return games


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_run_ct(run_at: str | None) -> str:
    parsed = _parse_dt(run_at)
    if parsed is None:
        return "unknown"
    return parsed.astimezone(CT).strftime("%Y-%m-%d %H:%M CT")


def report_day(run_at: str | None) -> str:
    parsed = _parse_dt(run_at)
    if parsed is None:
        parsed = datetime.now(CT)
    return parsed.astimezone(CT).date().isoformat()


def runs_match(stored_run_at: str | None, sidecar_run_at: str | None) -> bool:
    if not stored_run_at or not sidecar_run_at:
        return False
    if str(stored_run_at) == str(sidecar_run_at):
        return True
    left = _parse_dt(stored_run_at)
    right = _parse_dt(sidecar_run_at)
    if left is None or right is None:
        return False
    return abs(left - right) <= RUN_AT_TOLERANCE


def run_at_of(rows: list[dict]) -> str | None:
    counts = Counter(str(row.get("run_at")) for row in rows if row.get("run_at"))
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _inputs(row: dict) -> dict:
    raw = row.get("inputs")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def efficiency_from_rows(rows: list[dict]) -> str:
    for row in rows:
        value = _inputs(row).get("sim_efficiency")
        if value:
            return str(value)
    return "unknown"


def _num(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pos(row: dict) -> str:
    pos = str(row.get("position") or "").upper()
    if pos in {"D", "DEF", "DST"}:
        return "D"
    return pos


def _name(row: dict) -> str:
    return str(row.get("player_name") or row.get("name") or "").strip()


def _team(row: dict) -> str:
    return str(row.get("team") or "").upper()


def _opp(row: dict) -> str:
    return str(row.get("opponent") or row.get("opp") or "").upper()


def projection_rows(rows: list[dict]) -> list[dict]:
    """Prefer ``model=sim``. Board rows are used only when no sim rows exist."""
    clean = [row for row in rows if isinstance(row, dict)]
    sim = [row for row in clean if str(row.get("model") or "") == "sim"]
    if sim:
        return sim
    if any("model" in row for row in clean):
        return [row for row in clean if str(row.get("model") or "") == "board"]
    return clean


def _ranked(rows: list[dict], pos: str) -> list[dict]:
    pool = [row for row in rows if _pos(row) == pos and _num(row.get("mean")) is not None]
    pool.sort(key=lambda row: (-float(_num(row.get("mean")) or 0.0), _name(row), _team(row)))
    return pool


def fmt_salary(value: object) -> str:
    number = _num(value)
    if number is None:
        return DASH
    return f"{int(number):,}"


def fmt_points(value: object) -> str:
    number = _num(value)
    if number is None:
        return DASH
    return f"{number:.1f}"


def fmt_band(p10: object, p90: object) -> str:
    low = _num(p10)
    high = _num(p90)
    if low is None or high is None:
        return DASH
    return f"{low:.1f}–{high:.1f}"


def fmt_per_k(mean: object, salary: object) -> str:
    points = _num(mean)
    dollars = _num(salary)
    if points is None or dollars is None or dollars <= 0:
        return DASH
    return f"{points * 1000.0 / dollars:.2f}"


def _coerce_games(games) -> list[dict]:
    if not games:
        return []
    first = games[0]
    if isinstance(first, (tuple, list)) and len(first) == 5:
        return summarize_game_draws(games)
    out: list[dict] = []
    for game in games:
        if isinstance(game, dict):
            out.append(game)
    return out


def _sim_games(games: list[dict]) -> bool:
    return bool(games) and all(str(game.get("source") or "") == "sim" for game in games)


def _scorers(rows: list[dict], away: str, home: str) -> list[dict]:
    teams = {away.upper(), home.upper()}
    pool = [row for row in rows if _team(row) in teams and _num(row.get("mean")) is not None]
    pool.sort(key=lambda row: (-float(_num(row.get("mean")) or 0.0), _name(row)))
    return pool[:3]


def _games_table(games: list[dict], rows: list[dict]) -> str:
    headers = ["Game", "Team", "Median", "p10–p90", "#", "Player", "Pos", "Mean", "Salary"]
    aligns = ["left", "left", "right", "right", "right", "left", "left", "right", "right"]
    body: list[list[object]] = []
    for game in games:
        away = str(game.get("away") or "")
        home = str(game.get("home") or "")
        label = f"{away}@{home}" if away and home else str(game.get("game") or "")
        scorers = _scorers(rows, away, home)
        teams = (
            (away, game.get("away_median"), game.get("away_p10"), game.get("away_p90")),
            (home, game.get("home_median"), game.get("home_p10"), game.get("home_p90")),
        )
        height = max(len(scorers), len(teams), 1)
        for index in range(height):
            team = median = low = high = ""
            if index < len(teams) and teams[index][0]:
                team, median, low, high = teams[index]
            scorer = scorers[index] if index < len(scorers) else None
            body.append(
                [
                    label if index == 0 else "",
                    team,
                    fmt_points(median) if team else "",
                    fmt_band(low, high) if team else "",
                    str(index + 1) if scorer else "",
                    _name(scorer) if scorer else "",
                    _pos(scorer) if scorer else "",
                    fmt_points(scorer.get("mean")) if scorer else "",
                    fmt_salary(scorer.get("salary")) if scorer else "",
                ]
            )
    return graph_table("GAMES", headers, body, aligns=aligns, shrink=(5,))


def _position_table(title: str, rows: list[dict], limit: int) -> str:
    headers = ["#", "player", "team", "opp", "salary", "proj", "p10–p90", "pts/$1k"]
    aligns = ["right", "left", "left", "left", "right", "right", "right", "right"]
    body: list[list[object]] = []
    for index, row in enumerate(rows[:limit], start=1):
        body.append(
            [
                str(index),
                _name(row),
                _team(row),
                _opp(row),
                fmt_salary(row.get("salary")),
                fmt_points(row.get("mean")),
                fmt_band(row.get("p10"), row.get("p90")),
                fmt_per_k(row.get("mean"), row.get("salary")),
            ]
        )
    return graph_table(title, headers, body, aligns=aligns, shrink=(1,))


def _fence(table: str) -> str:
    return "```\n" + table + "\n```"


def build_report(
    rows: list[dict],
    games,
    *,
    season: int,
    week: int,
    run_at: str | None,
    draws: int | None,
    efficiency: str | None,
) -> str:
    """Render the nightly report. ``games`` is sim bands, raw draws, or vegas totals."""
    shown = projection_rows(list(rows or []))
    slate = _coerce_games(games)
    eff = efficiency if efficiency else efficiency_from_rows(shown)
    if not eff:
        eff = "unknown"
    draw_text = "unknown" if draws is None else str(int(draws))
    lines = [
        f"season {int(season)}",
        f"week {int(week)}",
        f"run {format_run_ct(run_at)}",
        f"draws {draw_text}",
        f"efficiency {eff}",
        f"games {len(slate)}",
        "",
    ]
    if not _sim_games(slate):
        lines.append(VEGAS_LABEL)
        lines.append("")
    lines.append(_fence(_games_table(slate, shown)))
    for pos, title, limit in POSITIONS:
        lines.append("")
        lines.append(_fence(_position_table(title, _ranked(shown, pos), limit)))
    lines.append("")
    return "\n".join(lines)


def report_path(season: int, week: int, run_at: str | None, root: Path | None = None) -> Path:
    dest = root or REPORTS_DIR
    return dest / f"{int(season)}-w{int(week)}-{report_day(run_at)}.md"


def games_sidecar_path(season: int, week: int, root: Path | None = None) -> Path:
    dest = root or REPORTS_DIR
    return dest / f"{int(season)}-w{int(week)}-games.json"


def write_report(
    rows: list[dict],
    games,
    *,
    season: int,
    week: int,
    run_at: str | None,
    draws: int | None,
    efficiency: str | None,
    dest: Path | None = None,
) -> Path:
    text = build_report(
        rows,
        games,
        season=season,
        week=week,
        run_at=run_at,
        draws=draws,
        efficiency=efficiency,
    )
    path = report_path(season, week, run_at, dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_games_sidecar(
    *,
    season: int,
    week: int,
    run_at: str,
    games: list[dict],
    dest: Path | None = None,
) -> Path:
    payload = {
        "season": int(season),
        "week": int(week),
        "run_at": run_at,
        "games": [
            {
                "game": game.get("game") or f"{game.get('away')}@{game.get('home')}",
                "away": game.get("away"),
                "home": game.get("home"),
                "away_median": game.get("away_median"),
                "away_p10": game.get("away_p10"),
                "away_p90": game.get("away_p90"),
                "home_median": game.get("home_median"),
                "home_p10": game.get("home_p10"),
                "home_p90": game.get("home_p90"),
            }
            for game in games
        ],
    }
    path = games_sidecar_path(season, week, dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_games_sidecar(season: int, week: int, root: Path | None = None) -> dict | None:
    path = games_sidecar_path(season, week, root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def fill_salaries(rows: list[dict], csv_path: str | Path) -> list[dict]:
    """Fill a null salary from a FanDuel players list (name + team + position)."""
    from nfl.players import load_fanduel_csv

    index: dict[tuple[str, str, str], int] = {}
    for player in load_fanduel_csv(csv_path):
        index[(match_key(player.name), player.team.upper(), player.position.upper())] = int(
            player.salary
        )
    filled: list[dict] = []
    for row in rows:
        copy = dict(row)
        if _num(copy.get("salary")) is None:
            pos = _pos(copy)
            key = (match_key(_name(copy)), _team(copy), pos)
            salary = index.get(key)
            if salary is None and pos == "D":
                salary = index.get((key[0], key[1], "DEF"))
            if salary is not None:
                copy["salary"] = salary
        filled.append(copy)
    return filled


def resolve_games(rows: list[dict], *, season: int, week: int, refresh_lines: bool = False) -> list[dict]:
    """Sidecar sim scores when ``run_at`` matches; otherwise vegas implied totals."""
    from nfl.gangstash_data import fetch_game_lines

    side = load_games_sidecar(season, week)
    stored = run_at_of(rows)
    if side and side.get("games") and runs_match(stored, side.get("run_at")):
        games = []
        for game in side["games"]:
            if not isinstance(game, dict):
                continue
            item = dict(game)
            item["source"] = "sim"
            games.append(item)
        if games:
            return games
    line_rows, _meta = fetch_game_lines(season=int(season), week=int(week), refresh=refresh_lines)
    return vegas_games_from_lines(line_rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--csv", default=None, help="FanDuel players list; fills null salaries")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from nfl.gangstash import (
        GangstashDataError,
        GangstashDataKeyMissing,
        GangstashKeyMissing,
        fetch_dataset,
    )

    args = parse_args(argv)
    if (args.season is None) ^ (args.week is None):
        print("report: pass both --season and --week", file=sys.stderr)
        return 1
    try:
        if args.season is None:
            from nfl.publish_projections import resolve_nfl_week

            today = datetime.now(ET).date()
            season, week, _lines = resolve_nfl_week(today, refresh=False)
        else:
            season, week = int(args.season), int(args.week)
        rows, _meta = fetch_dataset(
            "projections",
            {
                "season": str(season),
                "week": str(week),
                "model": "sim",
                "latest": "true",
            },
            refresh=True,
        )
        if args.csv:
            rows = fill_salaries(rows, args.csv)
        games = resolve_games(rows, season=season, week=week, refresh_lines=False)
    except (GangstashDataKeyMissing, GangstashDataError, GangstashKeyMissing) as e:
        print(f"report: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"report: {e}", file=sys.stderr)
        return 1
    run_at = run_at_of(rows)
    path = write_report(
        rows,
        games,
        season=season,
        week=week,
        run_at=run_at,
        draws=None,
        efficiency=efficiency_from_rows(rows),
        dest=None,
    )
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
