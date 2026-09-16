"""Ingest NCAAF spread/total and derive implied team totals.

Spread-sign convention (CFBD `spread`, this module's `home_spread`):
  the number is the **home team's** spread. Negative = home favorite.
  Example: home Texas, spread -29.5, formattedSpread "Texas -29.5".

Implied totals (same units as the game total):
  implied_home = (total - home_spread) / 2
  implied_away = (total + home_spread) / 2
  Equivalently for any team: implied = (total - team_spread) / 2
  where that team's spread is negative if they are favored.

Join: FanDuel CSV `Game` is AWAY@HOME, `Team` is the FanDuel abbrev.
Map abbrev → CFBD school / Odds API name via ncaaf/teams.py. Unmapped
teams and slate games with no line fail loud.
"""

from __future__ import annotations

import json
import re
import ssl
import statistics
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ncaaf import env as envmod
from ncaaf.teams import lookup_cfbd, lookup_odds, require_fd, require_mapped

CFBD_LINES_URL = "https://api.collegefootballdata.com/lines"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_ncaaf/odds"
USER_AGENT = "dfs-ncaaf/0.1 (vegas-implied-totals)"

LINES_KEY_MISSING = """No betting-lines API key.

Week-1 default objective is Vegas implied team totals (spread + total),
not CSV FPPG (prior season / empty on this slate). Refusing to silently
use FPPG.

Install (pick one):
  1. CollegeFootballData (preferred)
     https://collegefootballdata.com/key
     export CFBD_API_KEY='your-key'
     # or: printf 'CFBD_API_KEY=your-key\\n' >> .env && chmod 600 .env
  2. The Odds API (fallback)
     https://the-odds-api.com/
     export ODDS_API_KEY='your-key'

Then re-run the optimizer. Opt-in only, labeled prior-season:
  python3 -m ncaaf.optimize --csv ... --use-fppg
"""


class LinesError(Exception):
    """Fatal lines ingest."""


class LinesKeyMissing(LinesError):
    gate = "LINES_KEY"


class LinesAuthError(LinesError):
    gate = "LINES_AUTH"


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


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # already on this machine; optional dep

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _http_json(url: str, headers: dict[str, str], timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            body = resp.read().decode("utf-8")
    except ssl.SSLError as e:
        raise LinesError(
            f"TLS verify failed ({e}). Install certifi (`python3 -m pip install certifi`) "
            "or run Python's Install Certificates.command"
        ) from e
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", errors="replace")[:300]
        if e.code in {401, 403}:
            raise LinesAuthError(
                f"lines API HTTP {e.code} (key rejected). {payload}"
            ) from e
        raise LinesError(f"lines API HTTP {e.code}: {payload}") from e
    except urllib.error.URLError as e:
        raise LinesError(f"lines API unreachable: {e.reason}") from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise LinesError(f"lines API returned non-JSON: {e}") from e


def _num(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


_SPREAD_RE = re.compile(
    r"^(?P<team>.+?)\s+(?P<sign>[+-])(?P<pts>\d+(?:\.\d+)?)\s*$"
)


def _home_spread_from_formatted(formatted: str, home_cfbd: str, away_cfbd: str) -> float | None:
    m = _SPREAD_RE.match((formatted or "").strip())
    if not m:
        return None
    team = m.group("team").strip()
    pts = float(m.group("sign") + m.group("pts"))
    t_cf = team.casefold()
    if t_cf == home_cfbd.casefold() or home_cfbd.casefold().startswith(t_cf):
        return pts
    if t_cf == away_cfbd.casefold() or away_cfbd.casefold().startswith(t_cf):
        return -pts
    href = lookup_cfbd(team)
    if href is not None:
        if href.cfbd.casefold() == home_cfbd.casefold():
            return pts
        if href.cfbd.casefold() == away_cfbd.casefold():
            return -pts
    return None


def _pick_cfbd_line(lines: list[dict], home_cfbd: str, away_cfbd: str) -> dict:
    usable = [
        ln
        for ln in lines
        if _num(ln.get("spread")) is not None and _num(ln.get("overUnder")) is not None
    ]
    if not usable:
        raise LinesError(f"no spread+total for {away_cfbd} @ {home_cfbd}")
    for ln in usable:
        if str(ln.get("provider") or "").strip().lower() == "consensus":
            chosen = ln
            break
    else:
        chosen = {
            "provider": "median",
            "spread": statistics.median(_num(ln["spread"]) for ln in usable),
            "overUnder": statistics.median(_num(ln["overUnder"]) for ln in usable),
            "homeMoneyline": next(
                (n for n in (_num(ln.get("homeMoneyline")) for ln in usable) if n is not None),
                None,
            ),
            "awayMoneyline": next(
                (n for n in (_num(ln.get("awayMoneyline")) for ln in usable) if n is not None),
                None,
            ),
            "formattedSpread": None,
        }
    spread = _num(chosen["spread"])
    total = _num(chosen["overUnder"])
    assert spread is not None and total is not None
    formatted = chosen.get("formattedSpread")
    if formatted:
        parsed = _home_spread_from_formatted(str(formatted), home_cfbd, away_cfbd)
        if parsed is not None and abs(parsed - spread) > 0.51:
            raise LinesError(
                f"CFBD spread sign mismatch {away_cfbd}@{home_cfbd}: "
                f"spread={spread} formattedSpread={formatted!r} parsed_home={parsed}"
            )
    return chosen


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
    )


def parse_cfbd_games(
    payload: list[dict],
    slate: list[tuple[str, str, str]],
) -> dict[str, TeamLine]:
    """Match CFBD BettingGame rows to FanDuel AWAY@HOME slate games."""
    index: dict[tuple[str, str], dict] = {}
    for row in payload:
        home_ref = lookup_cfbd(str(row.get("homeTeam") or ""))
        away_ref = lookup_cfbd(str(row.get("awayTeam") or ""))
        if home_ref is None or away_ref is None:
            continue
        index[(away_ref.fd, home_ref.fd)] = row

    out: dict[str, TeamLine] = {}
    missing: list[str] = []
    for game, away_fd, home_fd in slate:
        row = index.get((away_fd, home_fd))
        if row is None:
            missing.append(game)
            continue
        home_ref = require_fd(home_fd)
        away_ref = require_fd(away_fd)
        chosen = _pick_cfbd_line(row.get("lines") or [], home_ref.cfbd, away_ref.cfbd)
        out[home_fd] = _team_line(
            game=game,
            home_fd=home_fd,
            away_fd=away_fd,
            home_spread=float(chosen["spread"]),
            total=float(chosen["overUnder"]),
            home_ml=_num(chosen.get("homeMoneyline")),
            away_ml=_num(chosen.get("awayMoneyline")),
            provider=str(chosen.get("provider") or "cfbd"),
            source="cfbd",
        )
        out[away_fd] = out[home_fd]
    if missing:
        raise LinesError(
            "no CFBD line for slate game(s): "
            + ", ".join(missing)
            + " (check week/year, or try The Odds API)"
        )
    return {fd: out[fd] for fd in {h for _, _, h in slate} | {a for _, a, _ in slate}}


def _odds_market(book: dict, key: str) -> dict | None:
    for m in book.get("markets") or []:
        if m.get("key") == key:
            return m
    return None


def _odds_point(market: dict | None, name: str) -> float | None:
    if not market:
        return None
    want = name.casefold()
    for outcome in market.get("outcomes") or []:
        if str(outcome.get("name") or "").casefold() == want:
            return _num(outcome.get("point"))
    return None


def _odds_price(market: dict | None, name: str) -> float | None:
    if not market:
        return None
    want = name.casefold()
    for outcome in market.get("outcomes") or []:
        if str(outcome.get("name") or "").casefold() == want:
            return _num(outcome.get("price"))
    return None


def parse_odds_games(
    payload: list[dict],
    slate: list[tuple[str, str, str]],
) -> dict[str, TeamLine]:
    index: dict[tuple[str, str], dict] = {}
    for row in payload:
        home_ref = lookup_odds(str(row.get("home_team") or ""))
        away_ref = lookup_odds(str(row.get("away_team") or ""))
        if home_ref is None or away_ref is None:
            continue
        index[(away_ref.fd, home_ref.fd)] = row

    out: dict[str, TeamLine] = {}
    missing: list[str] = []
    for game, away_fd, home_fd in slate:
        row = index.get((away_fd, home_fd))
        if row is None:
            missing.append(game)
            continue
        home_ref = require_fd(home_fd)
        away_ref = require_fd(away_fd)
        books = list(row.get("bookmakers") or [])
        preferred = [b for b in books if b.get("key") in {"fanduel", "draftkings", "betmgm"}]
        books_ord = preferred + [b for b in books if b not in preferred]
        home_spreads: list[float] = []
        totals: list[float] = []
        home_mls: list[float] = []
        away_mls: list[float] = []
        provider = "median"
        for book in books_ord:
            spreads = _odds_market(book, "spreads")
            tot = _odds_market(book, "totals")
            h2h = _odds_market(book, "h2h")
            hs = None
            for odds_name in home_ref.odds:
                hs = _odds_point(spreads, odds_name)
                if hs is not None:
                    break
            over = _odds_point(tot, "Over")
            if hs is not None:
                home_spreads.append(hs)
            if over is not None:
                totals.append(over)
            for odds_name in home_ref.odds:
                ml = _odds_price(h2h, odds_name)
                if ml is not None:
                    home_mls.append(ml)
                    break
            for odds_name in away_ref.odds:
                ml = _odds_price(h2h, odds_name)
                if ml is not None:
                    away_mls.append(ml)
                    break
            if book.get("key") == "fanduel" and hs is not None and over is not None:
                provider = "fanduel"
                home_spreads = [hs]
                totals = [over]
                break
        if not home_spreads or not totals:
            missing.append(game)
            continue
        out[home_fd] = _team_line(
            game=game,
            home_fd=home_fd,
            away_fd=away_fd,
            home_spread=float(statistics.median(home_spreads)),
            total=float(statistics.median(totals)),
            home_ml=statistics.median(home_mls) if home_mls else None,
            away_ml=statistics.median(away_mls) if away_mls else None,
            provider=provider,
            source="odds-api",
        )
        out[away_fd] = out[home_fd]
    if missing:
        raise LinesError("no Odds API line for slate game(s): " + ", ".join(missing))
    return {fd: out[fd] for fd in {h for _, _, h in slate} | {a for _, a, _ in slate}}


def parse_simple_games(
    payload: list[dict],
    slate: list[tuple[str, str, str]],
) -> dict[str, TeamLine]:
    """Replay JSON: [{away, home, spread, total, ...}] with FanDuel abbrevs.

    `spread` is the home-team spread (same sign convention as CFBD).
    """
    index: dict[tuple[str, str], dict] = {}
    for row in payload:
        away = str(row.get("away") or "").strip().upper()
        home = str(row.get("home") or "").strip().upper()
        if not away or not home:
            continue
        index[(away, home)] = row
    out: dict[str, TeamLine] = {}
    missing: list[str] = []
    for game, away_fd, home_fd in slate:
        row = index.get((away_fd, home_fd))
        if row is None:
            missing.append(game)
            continue
        spread = _num(row.get("spread"))
        total = _num(row.get("total") if "total" in row else row.get("overUnder"))
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
            source="lines-json",
        )
        out[away_fd] = out[home_fd]
    if missing:
        raise LinesError("no file line for slate game(s): " + ", ".join(missing))
    return {fd: out[fd] for fd in {h for _, _, h in slate} | {a for _, a, _ in slate}}


def load_lines_json(path: Path, slate: list[tuple[str, str, str]]) -> dict[str, TeamLine]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "games" in raw:
        raw = raw["games"]
    if not isinstance(raw, list) or not raw:
        raise LinesError(f"{path} is not a JSON array of games")
    first = raw[0]
    if "lines" in first and "homeTeam" in first:
        return parse_cfbd_games(raw, slate)
    if "bookmakers" in first and "home_team" in first:
        return parse_odds_games(raw, slate)
    return parse_simple_games(raw, slate)


def fetch_cfbd(
    *,
    year: int,
    week: int | None,
    season_type: str = "regular",
) -> list[dict]:
    key = envmod.get("CFBD_API_KEY")
    if not key:
        raise LinesKeyMissing(LINES_KEY_MISSING)
    params = {"year": str(year), "seasonType": season_type}
    if week is not None:
        params["week"] = str(week)
    url = CFBD_LINES_URL + "?" + urllib.parse.urlencode(params)
    payload = _http_json(
        url,
        {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {key}",
        },
    )
    if not isinstance(payload, list):
        raise LinesError("CFBD /lines did not return an array")
    return payload


def fetch_odds() -> list[dict]:
    key = envmod.get("ODDS_API_KEY") or envmod.get("THE_ODDS_API_KEY")
    if not key:
        raise LinesKeyMissing(LINES_KEY_MISSING)
    # The Odds API authenticates via apiKey query param (their contract).
    params = {
        "apiKey": key,
        "regions": "us",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
    }
    url = ODDS_URL + "?" + urllib.parse.urlencode(params)
    payload = _http_json(
        url,
        {"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    if not isinstance(payload, list):
        raise LinesError("Odds API did not return an array")
    return payload


def available_source() -> str | None:
    envmod.load()
    if envmod.get("CFBD_API_KEY"):
        return "cfbd"
    if envmod.get("ODDS_API_KEY") or envmod.get("THE_ODDS_API_KEY"):
        return "odds"
    return None


def ingest_slate_lines(
    players,
    *,
    year: int,
    week: int | None = None,
    source: str = "auto",
    lines_json: Path | None = None,
) -> dict[str, TeamLine]:
    """Return TeamLine keyed by FanDuel abbrev for every slate team."""
    slate = slate_from_players(players)
    fd_teams = {a for _, a, _ in slate} | {h for _, _, h in slate}
    require_mapped(fd_teams)

    if lines_json is not None:
        return load_lines_json(lines_json, slate)

    src = source
    if src == "auto":
        picked = available_source()
        if picked is None:
            raise LinesKeyMissing(LINES_KEY_MISSING)
        src = picked

    if src == "cfbd":
        payload = fetch_cfbd(year=year, week=week)
        try:
            return parse_cfbd_games(payload, slate)
        except LinesError:
            if week is not None:
                payload = fetch_cfbd(year=year, week=None)
                return parse_cfbd_games(payload, slate)
            raise
    if src == "odds":
        return parse_odds_games(fetch_odds(), slate)
    raise LinesError(f"unknown lines source {source!r}")


def unique_games(by_team: dict[str, TeamLine]) -> list[TeamLine]:
    seen: dict[str, TeamLine] = {}
    for line in by_team.values():
        seen.setdefault(line.game, line)
    return [seen[k] for k in sorted(seen)]
