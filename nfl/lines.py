"""Ingest NFL spread/total and derive implied team totals.

Game lines come from gangstash only (`dataset=game_lines`, BettingPros
consensus). `home_team_fd` / `away_team_fd` map through FanDuel abbrevs.
There is no Odds API client and no odds fallback. A missing gangstash key
or cache is `LINES_GANGSTASH_KEY` and stops the run.

`--lines-file` (CSV or JSON) and `--lines-json` replay a simple file and
ignore the live source. An Odds API dump (`bookmakers`) is rejected.
A past `--week` prefers `dataset=closing_lines`, then `game_lines`. A lines
file accepts FanDuel columns and the nflverse schedule columns (`home_team`,
`away_team`, `spread_line`, `total_line`, `home_implied_tt`,
`away_implied_tt`, `season`, `week`). `JAX`→`JAC` and `LA`→`LAR`.
A file with ``season``, ``week``, ``home_team``, ``away_team``,
``spread_line``, and ``total_line`` is an nflverse schedule: other columns
are ignored. ``spread_line`` is positive when the home team is favored, so
the home spread is ``-spread_line``. ``home_implied_tt`` and
``away_implied_tt`` win when both are present. A simple file still errors
on an unrecognized column. Zero matched games is an error either way.

implied_home = (total - home_spread) / 2
implied_away = (total + home_spread) / 2
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nfl.gangstash import GangstashDataError, GangstashDataKeyMissing, GangstashTruncated
from nfl.gangstash_data import (
    GangstashGameLine,
    fetch_closing_lines,
    fetch_game_lines,
    map_game_lines,
)
from nfl.teams import UnmappedTeam, require_fd, require_mapped

CHICAGO = ZoneInfo("America/Chicago")


class LinesError(Exception):
    """Fatal lines ingest."""


class LinesKeyMissing(LinesError):
    gate = "LINES_KEY"


class LinesAuthError(LinesError):
    gate = "LINES_AUTH"


class LinesGangstashError(LinesError):
    """Gangstash game-lines HTTP or payload failure."""


class LinesGangstashKeyMissing(LinesKeyMissing):
    gate = "LINES_GANGSTASH_KEY"


@dataclass(frozen=True)
class TeamLine:
    game: str
    home_fd: str
    away_fd: str
    home_spread: float
    total: float
    implied_home: float
    implied_away: float
    home_moneyline: float | None
    away_moneyline: float | None
    provider: str
    source: str
    commence_time: str | None = None

    def implied_for(self, fd: str) -> float:
        if fd == self.home_fd:
            return self.implied_home
        if fd == self.away_fd:
            return self.implied_away
        raise LinesError(f"{fd} not in {self.game}")

    def spread_for(self, fd: str) -> float:
        if fd == self.home_fd:
            return self.home_spread
        if fd == self.away_fd:
            return -self.home_spread
        raise LinesError(f"{fd} not in {self.game}")

    def moneyline_for(self, fd: str) -> float | None:
        if fd == self.home_fd:
            return self.home_moneyline
        if fd == self.away_fd:
            return self.away_moneyline
        raise LinesError(f"{fd} not in {self.game}")

    def to_dict(self) -> dict:
        return {
            "game": self.game,
            "away": self.away_fd,
            "home": self.home_fd,
            "spread_home": self.home_spread,
            "spread_away": -self.home_spread,
            "total": self.total,
            "implied_home": round(self.implied_home, 4),
            "implied_away": round(self.implied_away, 4),
            "moneyline_home": self.home_moneyline,
            "moneyline_away": self.away_moneyline,
            "provider": self.provider,
            "source": self.source,
            "commence_time": self.commence_time,
        }


def implied_totals(total: float, home_spread: float) -> tuple[float, float]:
    """Return (implied_home, implied_away). home_spread negative ⇒ home favorite."""
    home = (total - home_spread) / 2.0
    away = (total + home_spread) / 2.0
    return home, away


def parse_fanduel_game(game: str) -> tuple[str, str]:
    """FanDuel `Game` column: AWAY@HOME. Returns (away_fd, home_fd)."""
    raw = (game or "").strip().upper()
    if "@" not in raw:
        raise LinesError(f"FanDuel Game {game!r} is not AWAY@HOME")
    away, home = raw.split("@", 1)
    away, home = away.strip(), home.strip()
    if not away or not home:
        raise LinesError(f"FanDuel Game {game!r} is not AWAY@HOME")
    return away, home


def slate_from_players(players) -> list[tuple[str, str, str]]:
    """Unique (game, away_fd, home_fd) from a FanDuel pool."""
    seen: dict[str, tuple[str, str, str]] = {}
    for pl in players:
        game = pl.game.strip()
        if game in seen:
            continue
        away, home = parse_fanduel_game(game)
        if pl.team == home and pl.opponent == away:
            pass
        elif pl.team == away and pl.opponent == home:
            pass
        else:
            raise LinesError(
                f"{pl.name} team={pl.team} opp={pl.opponent} does not match Game {game}"
            )
        seen[game] = (game, away, home)
    return [seen[k] for k in sorted(seen)]


def infer_slate_date(csv_path: Path | None = None) -> date:
    """FanDuel filename `NFL-YYYY CDT-MM CDT-DD` else 2026-09-13."""
    if csv_path is not None:
        m = re.search(
            r"NFL-(\d{4})\s*CDT-(\d{2})\s*CDT-(\d{2})",
            csv_path.name,
            re.I,
        )
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return date(2026, 9, 13)


def slate_window(slate_day: date) -> tuple[datetime, datetime]:
    """Chicago midnight on slate day through 06:00 the next morning (SNF)."""
    start = datetime(slate_day.year, slate_day.month, slate_day.day, tzinfo=CHICAGO)
    end = start + timedelta(days=1, hours=6)
    return start, end


def parse_commence(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def in_window(commence: str, start: datetime, end: datetime) -> bool:
    dt = parse_commence(commence)
    if dt is None:
        return False
    return start <= dt < end


def filter_commence(
    payload: list[dict], start: datetime, end: datetime
) -> list[dict]:
    return [row for row in payload if in_window(str(row.get("commence_time") or ""), start, end)]


def _num(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _team_line(
    *,
    game: str,
    home_fd: str,
    away_fd: str,
    home_spread: float,
    total: float,
    home_ml: float | None,
    away_ml: float | None,
    provider: str,
    source: str,
    commence_time: str | None = None,
) -> TeamLine:
    implied_home, implied_away = implied_totals(total, home_spread)
    return TeamLine(
        game=game,
        home_fd=home_fd,
        away_fd=away_fd,
        home_spread=home_spread,
        total=total,
        implied_home=implied_home,
        implied_away=implied_away,
        home_moneyline=home_ml,
        away_moneyline=away_ml,
        provider=provider,
        source=source,
        commence_time=commence_time,
    )


# Simple files (CSV or JSON). An nflverse schedule is
# recognized by its required columns and may carry the rest of the schedule
# (game_id, moneylines, home_line, *_spread_odds, …). Those are ignored.
_NFLVERSE_REQUIRED = (
    "season",
    "week",
    "home_team",
    "away_team",
    "spread_line",
    "total_line",
)
# Columns that identify an nflverse schedule. ``season`` and ``week`` alone
# do not: a simple file may carry those next to ``home`` / ``spread``.
_NFLVERSE_MARKERS = frozenset(
    {
        "home_team",
        "away_team",
        "spread_line",
        "total_line",
        "home_implied_tt",
        "away_implied_tt",
        "game_id",
        "gameday",
        "gametime",
        "home_line",
        "away_line",
        "home_spread_odds",
        "away_spread_odds",
    }
)
_LINE_COLUMNS = frozenset(
    {
        "home",
        "away",
        "home_team",
        "away_team",
        "home_team_fd",
        "away_team_fd",
        "spread",
        "total",
        "overUnder",
        "spread_line",
        "total_line",
        "home_implied",
        "away_implied",
        "home_implied_total",
        "away_implied_total",
        "home_implied_tt",
        "away_implied_tt",
        "implied_home",
        "implied_away",
        "home_team_total",
        "away_team_total",
        "home_moneyline",
        "away_moneyline",
        "homeMoneyline",
        "awayMoneyline",
        "provider",
        "commence_time",
        "season",
        "week",
    }
)


def _header_names(rows: list[dict]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            name = "" if key is None else str(key).strip()
            if not name:
                name = "(blank)"
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return names


def _check_line_columns(rows: list[dict]) -> None:
    """nflverse schedules must have the key columns. Other columns are kept.

    A file that is not an nflverse schedule still errors on a column this
    reader does not know.
    """
    headers = _header_names(rows)
    header_set = set(headers)
    if header_set & _NFLVERSE_MARKERS:
        missing = [name for name in _NFLVERSE_REQUIRED if name not in header_set]
        if missing:
            raise LinesError(
                "lines file missing required columns: " + ", ".join(missing)
            )
        return
    unknown = [name for name in headers if name not in _LINE_COLUMNS]
    if unknown:
        raise LinesError(
            "unrecognized line columns: " + ", ".join(sorted(unknown))
        )


def _cell_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError) as e:
        raise LinesError(f"bad season or week in lines file: {value!r}") from e


def _filter_line_rows(
    rows: list[dict],
    *,
    season: int | None,
    week: int | None,
) -> list[dict]:
    """Keep one season and week when the file has those columns.

    A file with no season/week columns is already one slate. Several
    seasons or weeks and no ``--season`` / ``--week`` is an error.
    """
    has_season = any(row.get("season") not in (None, "") for row in rows)
    has_week = any(row.get("week") not in (None, "") for row in rows)
    if season is None and week is None:
        seasons = {
            str(_cell_int(row.get("season")))
            for row in rows
            if row.get("season") not in (None, "")
        }
        weeks = {
            str(_cell_int(row.get("week")))
            for row in rows
            if row.get("week") not in (None, "")
        }
        if len(seasons) > 1 or len(weeks) > 1:
            raise LinesError(
                "lines file has more than one season or week; pass --season and --week"
            )
        return rows
    kept: list[dict] = []
    for row in rows:
        if season is not None and has_season:
            raw = _cell_int(row.get("season"))
            if raw is None or raw != int(season):
                continue
        if week is not None and has_week:
            raw = _cell_int(row.get("week"))
            if raw is None or raw != int(week):
                continue
        kept.append(row)
    if not kept:
        raise LinesError(
            f"lines file matched 0 games for season {season} week {week}"
        )
    return kept


def _normalize_line_row(row: dict) -> dict:
    """Copy nflverse names onto the simple-file names."""
    out = dict(row)
    if not (out.get("home") or out.get("home_team_fd")) and out.get("home_team"):
        out["home"] = out["home_team"]
    if not (out.get("away") or out.get("away_team_fd")) and out.get("away_team"):
        out["away"] = out["away_team"]
    # nflverse ``total`` is the final score. The closing number is ``total_line``.
    # ``spread_line`` is flipped in ``_file_spread_total`` (positive = home favored).
    if out.get("home_implied_tt") not in (None, "") and not any(
        out.get(key) not in (None, "")
        for key in (
            "home_implied",
            "home_implied_total",
            "implied_home",
            "home_team_total",
        )
    ):
        out["home_implied"] = out["home_implied_tt"]
    if out.get("away_implied_tt") not in (None, "") and not any(
        out.get(key) not in (None, "")
        for key in (
            "away_implied",
            "away_implied_total",
            "implied_away",
            "away_team_total",
        )
    ):
        out["away_implied"] = out["away_implied_tt"]
    return out


def _prepare_simple_rows(
    rows: list[dict],
    *,
    season: int | None,
    week: int | None,
) -> list[dict]:
    if not rows:
        raise LinesError("lines file has no line rows")
    _check_line_columns(rows)
    return [
        _normalize_line_row(row)
        for row in _filter_line_rows(rows, season=season, week=week)
    ]


def _fd_cell(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return require_fd(text).fd
    except UnmappedTeam:
        return text.upper()


def _implied_pair(row: dict) -> tuple[float | None, float | None]:
    home_impl = _num(
        row.get("home_implied")
        or row.get("home_implied_total")
        or row.get("home_implied_tt")
        or row.get("implied_home")
        or row.get("home_team_total")
    )
    away_impl = _num(
        row.get("away_implied")
        or row.get("away_implied_total")
        or row.get("away_implied_tt")
        or row.get("implied_away")
        or row.get("away_team_total")
    )
    return home_impl, away_impl


def _file_spread_total(row: dict) -> tuple[float | None, float | None]:
    """Home spread is negative when home is favored.

    Implied team totals win when both are present. nflverse ``spread_line``
    is the opposite sign (positive means the home team is favored), so the
    home spread is ``-spread_line`` and the total is ``total_line`` (not the
    final-score ``total`` column). A simple ``spread`` column is already the
    home spread.
    """
    home_impl, away_impl = _implied_pair(row)
    if home_impl is not None and away_impl is not None:
        return away_impl - home_impl, home_impl + away_impl
    spread_line = _num(row.get("spread_line"))
    total_line = _num(row.get("total_line"))
    if spread_line is not None and total_line is not None:
        return -spread_line, total_line
    spread = _num(row.get("spread"))
    total = _num(row.get("total") if "total" in row else row.get("overUnder"))
    if spread is not None and total is not None:
        return spread, total
    return spread, total


def parse_simple_games(
    payload: list[dict],
    slate: list[tuple[str, str, str]],
    *,
    source: str = "lines-json",
) -> dict[str, TeamLine]:
    """Replay JSON or CSV rows with FanDuel abbrevs.

    ``spread`` + ``total``, or home and away implied totals. ``home`` /
    ``away`` or ``home_team_fd`` / ``away_team_fd``.
    """
    index: dict[tuple[str, str], dict] = {}
    for row in payload:
        away = _fd_cell(row.get("away") or row.get("away_team_fd"))
        home = _fd_cell(row.get("home") or row.get("home_team_fd"))
        if not away or not home:
            continue
        index[(away, home)] = row
    if not index:
        raise LinesError("lines file matched 0 games")
    out: dict[str, TeamLine] = {}
    missing: list[str] = []
    for game, away_fd, home_fd in slate:
        row = index.get((away_fd, home_fd))
        if row is None:
            missing.append(game)
            continue
        spread, total = _file_spread_total(row)
        if spread is None or total is None:
            missing.append(game)
            continue
        out[home_fd] = _team_line(
            game=game,
            home_fd=home_fd,
            away_fd=away_fd,
            home_spread=spread,
            total=total,
            home_ml=_num(row.get("home_moneyline") or row.get("homeMoneyline")),
            away_ml=_num(row.get("away_moneyline") or row.get("awayMoneyline")),
            provider=str(row.get("provider") or "file"),
            source=source,
            commence_time=str(row.get("commence_time") or "") or None,
        )
        out[away_fd] = out[home_fd]
    if missing:
        raise LinesError("no file line for slate game(s): " + ", ".join(missing))
    return {fd: out[fd] for fd in {h for _, _, h in slate} | {a for _, a, _ in slate}}


def load_lines_json(
    path: Path,
    slate: list[tuple[str, str, str]],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    source: str = "lines-json",
    season: int | None = None,
    week: int | None = None,
) -> dict[str, TeamLine]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "games" in raw:
        raw = raw["games"]
    if not isinstance(raw, list) or not raw:
        raise LinesError(f"{path} is not a JSON array of games")
    first = raw[0]
    if "bookmakers" in first and "home_team" in first:
        raise LinesError(
            "Odds API line files are not supported; game lines come from gangstash "
            "(GANGSTASH_API_KEY) or a simple spread/total file"
        )
    prepared = _prepare_simple_rows(raw, season=season, week=week)
    return parse_simple_games(prepared, slate, source=source)


def load_lines_csv(
    path: Path,
    slate: list[tuple[str, str, str]],
    *,
    season: int | None = None,
    week: int | None = None,
) -> dict[str, TeamLine]:
    """CSV with FanDuel or nflverse line columns.

    An nflverse schedule (``home_team``, ``spread_line``, ``total_line``, …)
    may carry the rest of the schedule file. A simple file errors on a
    column this reader does not know.
    """
    with Path(path).open(newline="", encoding="utf-8") as fh:
        rows = [dict(row) for row in csv.DictReader(fh)]
    if not rows:
        raise LinesError(f"{path} has no line rows")
    prepared = _prepare_simple_rows(rows, season=season, week=week)
    return parse_simple_games(prepared, slate, source="lines-file")


def load_lines_file(
    path: Path,
    slate: list[tuple[str, str, str]],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    season: int | None = None,
    week: int | None = None,
) -> dict[str, TeamLine]:
    """CSV or JSON. Wins over the live lines source."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return load_lines_csv(path, slate, season=season, week=week)
    return load_lines_json(
        path,
        slate,
        start=start,
        end=end,
        source="lines-file",
        season=season,
        week=week,
    )


def team_lines_from_gangstash(
    games: list[GangstashGameLine],
    slate: list[tuple[str, str, str]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, TeamLine]:
    """Join gangstash rows onto the FanDuel slate. Home spread sign matches Odds."""
    index: dict[tuple[str, str], GangstashGameLine] = {}
    for game in games:
        if game.commence_time and not in_window(game.commence_time, start, end):
            continue
        index[(game.away_fd, game.home_fd)] = game
    out: dict[str, TeamLine] = {}
    missing: list[str] = []
    for game, away_fd, home_fd in slate:
        row = index.get((away_fd, home_fd))
        if row is None:
            missing.append(game)
            continue
        out[home_fd] = _team_line(
            game=game,
            home_fd=home_fd,
            away_fd=away_fd,
            home_spread=row.spread,
            total=row.total,
            home_ml=row.home_moneyline,
            away_ml=row.away_moneyline,
            provider="bettingpros",
            source="gangstash",
            commence_time=row.commence_time,
        )
        out[away_fd] = out[home_fd]
    if missing:
        raise LinesGangstashError(
            "no gangstash line for slate game(s): " + ", ".join(missing)
        )
    return {fd: out[fd] for fd in {h for _, _, h in slate} | {a for _, a, _ in slate}}


def _ingest_gangstash_lines(
    slate: list[tuple[str, str, str]],
    *,
    slate_day: date,
    refresh: bool,
) -> dict[str, TeamLine]:
    start, end = slate_window(slate_day)
    try:
        raw, _meta = fetch_game_lines(on_date=slate_day, refresh=refresh)
    except GangstashDataKeyMissing as e:
        raise LinesGangstashKeyMissing(
            "GANGSTASH_API_KEY is not set and no gangstash game-lines cache exists"
        ) from e
    except (GangstashTruncated, GangstashDataError) as e:
        raise LinesGangstashError(str(e)) from e
    return team_lines_from_gangstash(
        map_game_lines(raw), slate, start=start, end=end
    )


_HISTORICAL_START = datetime(2000, 1, 1, tzinfo=timezone.utc)
_HISTORICAL_END = datetime(2100, 1, 1, tzinfo=timezone.utc)


def _lines_from_rows(
    raw: list[dict],
    slate: list[tuple[str, str, str]],
) -> dict[str, TeamLine]:
    """Map rows onto the slate. An unparseable close is a lines error.

    ``_ingest_week_lines`` catches that and tries ``game_lines``.
    """
    try:
        games = map_game_lines(raw)
    except GangstashDataError as e:
        raise LinesError(str(e)) from e
    if not games:
        raise LinesError("line rows had no parseable spread/total")
    return team_lines_from_gangstash(
        games,
        slate,
        start=_HISTORICAL_START,
        end=_HISTORICAL_END,
    )


def _ingest_week_lines(
    slate: list[tuple[str, str, str]],
    *,
    season: int,
    week: int,
    refresh: bool,
) -> dict[str, TeamLine]:
    """Past week: ``closing_lines`` first, then ``game_lines``."""
    key_misses: list[Exception] = []
    other: list[Exception] = []
    for fetch in (
        lambda: fetch_closing_lines(season=season, week=week, refresh=refresh),
        lambda: fetch_game_lines(season=season, week=week, refresh=refresh),
    ):
        try:
            raw, _meta = fetch()
        except GangstashDataKeyMissing as e:
            key_misses.append(e)
            continue
        except (GangstashTruncated, GangstashDataError) as e:
            other.append(e)
            continue
        if not raw:
            continue
        try:
            return _lines_from_rows(list(raw), slate)
        except LinesError as e:
            other.append(e)
            continue
    if key_misses and not other:
        raise LinesGangstashKeyMissing(
            "GANGSTASH_API_KEY is not set and no closing or game-lines cache exists"
        )
    detail = str(other[-1]) if other else "empty closing_lines and game_lines"
    raise LinesGangstashError(
        f"no closing_lines or game_lines for season {season} week {week}: {detail}"
    )


def ingest_slate_lines(
    players,
    *,
    lines_json: Path | None = None,
    lines_file: Path | None = None,
    slate_day: date | None = None,
    refresh: bool = False,
    source: str = "gangstash",
    season: int | None = None,
    week: int | None = None,
) -> dict[str, TeamLine]:
    """Return TeamLine keyed by FanDuel abbrev for every slate team.

    The live source is gangstash. `--lines-file` wins, then `--lines-json`.
    A set ``week`` prefers ``closing_lines`` and then ``game_lines``.
    """
    slate = slate_from_players(players)
    fd_teams = {a for _, a, _ in slate} | {h for _, _, h in slate}
    require_mapped(fd_teams)
    day = slate_day or date(2026, 9, 13)
    start, end = slate_window(day)

    if lines_file is not None:
        return load_lines_file(
            lines_file, slate, start=start, end=end, season=season, week=week
        )
    if lines_json is not None:
        return load_lines_json(
            lines_json, slate, start=start, end=end, season=season, week=week
        )

    src = (source or "gangstash").strip().lower()
    if src != "gangstash":
        raise LinesError(
            f"unknown --lines-source {source!r}; game lines come from gangstash only "
            "(GANGSTASH_API_KEY, dataset=game_lines or closing_lines)"
        )
    if week is not None:
        return _ingest_week_lines(
            slate,
            season=int(season or day.year),
            week=int(week),
            refresh=refresh,
        )
    return _ingest_gangstash_lines(slate, slate_day=day, refresh=refresh)


def unique_games(by_team: dict[str, TeamLine]) -> list[TeamLine]:
    seen: dict[str, TeamLine] = {}
    for line in by_team.values():
        seen.setdefault(line.game, line)
    return [seen[k] for k in sorted(seen)]
