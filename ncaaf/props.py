"""Odds API NCAAF player-prop overlay.

Per-event GET (markets × 1 region credits). Cache under ncaaf/data/odds-props/.
When a player has volume lines, objective is FanDuel points from those lines.
Everyone else stays implied-total × depth × position share (same units).
Never log the API key (query param is their auth contract).
"""

from __future__ import annotations

import json
import ssl
import statistics
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from ncaaf import env as envmod
from ncaaf.depth import NAME_OVERRIDES
from ncaaf.lines import USER_AGENT, slate_from_players
from ncaaf.ourlads import match_key
from ncaaf.players import Player
from ncaaf.projections import week1_score
from ncaaf.rules import FANDUEL_NCAAF
from ncaaf.teams import lookup_odds

ODDS_EVENTS = "https://api.the-odds-api.com/v4/sports/americanfootball_ncaaf/events"
ODDS_EVENT_ODDS = (
    "https://api.the-odds-api.com/v4/sports/americanfootball_ncaaf/events/{eid}/odds"
)
CACHE_DIR = Path(__file__).resolve().parent / "data" / "odds-props"
# 5 markets × slate games ≈ 70 credits + 1 events list.
MARKETS = (
    "player_pass_yds",
    "player_pass_tds",
    "player_rush_yds",
    "player_reception_yds",
    "player_receptions",
)
MARKET_FIELD = {
    "player_pass_yds": "pass_yds",
    "player_pass_tds": "pass_tds",
    "player_rush_yds": "rush_yds",
    "player_reception_yds": "rec_yds",
    "player_receptions": "receptions",
}


class PropsError(Exception):
    """Fatal props ingest."""


class PropsKeyMissing(PropsError):
    gate = "PROPS_ODDS_KEY"


@dataclass(frozen=True)
class PlayerProp:
    name: str
    pass_yds: float | None = None
    pass_tds: float | None = None
    rush_yds: float | None = None
    rec_yds: float | None = None
    receptions: float | None = None
    book: str | None = None

    def fd_points(self) -> float | None:
        sc = FANDUEL_NCAAF.scoring
        pts = 0.0
        n = 0
        if self.pass_yds is not None:
            pts += self.pass_yds * sc["pass_yd"]
            n += 1
        if self.pass_tds is not None:
            pts += self.pass_tds * sc["pass_td"]
            n += 1
        if self.rush_yds is not None:
            pts += self.rush_yds * sc["rush_yd"]
            n += 1
        if self.rec_yds is not None:
            pts += self.rec_yds * sc["rec_yd"]
            n += 1
        if self.receptions is not None:
            pts += self.receptions * sc["rec"]
            n += 1
        if n == 0:
            return None
        return pts

    def has_volume(self) -> bool:
        return any(
            x is not None
            for x in (self.pass_yds, self.rush_yds, self.rec_yds, self.receptions)
        )


def _ssl() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(url: str) -> tuple[Any, dict[str, str]]:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl()) as resp:
            body = resp.read().decode("utf-8")
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", errors="replace")[:240]
        raise PropsError(f"Odds API HTTP {e.code}: {payload}") from e
    except urllib.error.URLError as e:
        raise PropsError(f"Odds API unreachable: {e.reason}") from e
    try:
        return json.loads(body), hdrs
    except json.JSONDecodeError as e:
        raise PropsError(f"Odds API non-JSON: {e}") from e


def _key() -> str:
    k = envmod.get("ODDS_API_KEY") or envmod.get("THE_ODDS_API_KEY")
    if not k:
        raise PropsKeyMissing("ODDS_API_KEY is not set")
    return k


def _cache_day() -> Path:
    d = CACHE_DIR / date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_events(*, refresh: bool = False) -> tuple[list[dict], str | None]:
    path = _cache_day() / "events.json"
    if path.is_file() and not refresh:
        return json.loads(path.read_text(encoding="utf-8")), None
    url = ODDS_EVENTS + "?" + urllib.parse.urlencode({"apiKey": _key()})
    payload, hdrs = _get(url)
    if not isinstance(payload, list):
        raise PropsError("Odds events did not return an array")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload, hdrs.get("x-requests-remaining")


def fetch_event_props(event_id: str, *, refresh: bool = False) -> tuple[dict, str | None]:
    path = _cache_day() / f"{event_id}.json"
    if path.is_file() and not refresh:
        return json.loads(path.read_text(encoding="utf-8")), None
    params = {
        "apiKey": _key(),
        "regions": "us",
        "markets": ",".join(MARKETS),
        "oddsFormat": "american",
        "bookmakers": "fanduel,draftkings,betmgm,bovada",
    }
    url = ODDS_EVENT_ODDS.format(eid=event_id) + "?" + urllib.parse.urlencode(params)
    payload, hdrs = _get(url)
    if not isinstance(payload, dict):
        raise PropsError(f"Odds event {event_id} did not return an object")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload, hdrs.get("x-requests-remaining")


def _median_over(points: list[float]) -> float | None:
    if not points:
        return None
    return float(statistics.median(points))


def _collect_overs(book: dict) -> dict[str, dict[str, float]]:
    """match_key → field → over point for one book."""
    out: dict[str, dict[str, float]] = {}
    for market in book.get("markets") or []:
        field = MARKET_FIELD.get(str(market.get("key") or ""))
        if not field:
            continue
        for o in market.get("outcomes") or []:
            if str(o.get("name") or "").casefold() != "over":
                continue
            desc = (o.get("description") or "").strip()
            pt = o.get("point")
            if not desc or pt is None:
                continue
            try:
                val = float(pt)
            except (TypeError, ValueError):
                continue
            out.setdefault(match_key(desc), {})[field] = val
    return out


def parse_event_props(payload: dict) -> list[PlayerProp]:
    """One line per player. FanDuel book wins a field; else median of others."""
    books = list(payload.get("bookmakers") or [])
    fd = next((b for b in books if b.get("key") == "fanduel"), None)
    fd_map = _collect_overs(fd) if fd else {}
    others = [_collect_overs(b) for b in books if b.get("key") != "fanduel"]
    names = set(fd_map) | {n for m in others for n in m}
    out: list[PlayerProp] = []
    for mk in names:
        fields: dict[str, float] = {}
        for field in MARKET_FIELD.values():
            if mk in fd_map and field in fd_map[mk]:
                fields[field] = fd_map[mk][field]
                continue
            pts = [m[mk][field] for m in others if mk in m and field in m[mk]]
            med = _median_over(pts)
            if med is not None:
                fields[field] = med
        if not fields:
            continue
        prop = PlayerProp(
            name=mk,
            pass_yds=fields.get("pass_yds"),
            pass_tds=fields.get("pass_tds"),
            rush_yds=fields.get("rush_yds"),
            rec_yds=fields.get("rec_yds"),
            receptions=fields.get("receptions"),
            book="fanduel" if mk in fd_map else "median",
        )
        if prop.has_volume() or prop.pass_tds is not None:
            out.append(prop)
    return out


def _match_event(events: list[dict], away_fd: str, home_fd: str) -> dict | None:
    for ev in events:
        home = lookup_odds(str(ev.get("home_team") or ""))
        away = lookup_odds(str(ev.get("away_team") or ""))
        if home is None or away is None:
            continue
        if home.fd == home_fd and away.fd == away_fd:
            return ev
    return None


def _join_prop(
    prop: PlayerProp,
    pool: list[Player],
    home_fd: str,
    away_fd: str,
) -> Player | None:
    teams = {home_fd, away_fd}
    want = match_key(prop.name)
    hits = [p for p in pool if p.team in teams and match_key(p.name) == want]
    if len(hits) == 1:
        return hits[0]
    for (team, src), nick in NAME_OVERRIDES.items():
        if team not in teams:
            continue
        if match_key(src) == want and match_key(nick) != want:
            hits = [p for p in pool if p.team == team and match_key(p.name) == match_key(nick)]
            if len(hits) == 1:
                return hits[0]
    # unique last name on the two teams
    last = want.split()[-1] if want else ""
    if last:
        last_hits = [
            p
            for p in pool
            if p.team in teams and match_key(p.name).split()[-1] == last
        ]
        if len(last_hits) == 1:
            return last_hits[0]
    return None


def ingest_slate_props(
    pool: list[Player],
    *,
    refresh: bool = False,
) -> tuple[dict[str, PlayerProp], dict]:
    """Return pid → PlayerProp for slate players with volume lines."""
    events, remaining = fetch_events(refresh=refresh)
    slate = slate_from_players(pool)
    joined: dict[str, PlayerProp] = {}
    unmatched: list[dict] = []
    games_hit = 0
    games_miss = 0
    last_remaining = remaining
    for game, away_fd, home_fd in slate:
        ev = _match_event(events, away_fd, home_fd)
        if ev is None:
            games_miss += 1
            continue
        payload, rem = fetch_event_props(str(ev["id"]), refresh=refresh)
        if rem is not None:
            last_remaining = rem
        games_hit += 1
        for prop in parse_event_props(payload):
            pl = _join_prop(prop, pool, home_fd, away_fd)
            if pl is None:
                unmatched.append(
                    {"name": prop.name, "home": home_fd, "away": away_fd}
                )
                continue
            joined[pl.pid] = prop
    stats = {
        "games_with_props": games_hit,
        "games_unmatched": games_miss,
        "players_with_props": len(joined),
        "credits_remaining": last_remaining,
        "markets": list(MARKETS),
        "unmatched": unmatched,
    }
    return joined, stats


def _unmatched_lasts(
    unmatched: list,
    team: str,
    opponent: str,
) -> set[str]:
    """Last names of book props that did not join, on this player's game."""
    out: set[str] = set()
    for u in unmatched:
        if isinstance(u, dict):
            name = str(u.get("name") or "")
            home, away = u.get("home"), u.get("away")
            if home or away:
                if team not in {home, away}:
                    continue
        else:
            name = getattr(u, "name", "") or ""
        last = match_key(name).split()[-1] if name else ""
        if last:
            out.add(last)
    return out


def attach_props(
    players: list[Player],
    by_pid: dict[str, PlayerProp],
    unmatched: list | None = None,
) -> list[Player]:
    um = unmatched or []
    out: list[Player] = []
    for pl in players:
        prop = by_pid.get(pl.pid)
        fd = prop.fd_points() if prop is not None else None
        if prop is not None:
            status = "props"
        else:
            lasts = _unmatched_lasts(um, pl.team, pl.opponent)
            last = match_key(pl.name).split()[-1] if pl.name else ""
            status = "unmatched" if last and last in lasts else "no_market"
        nxt = replace(pl, prop_fd=fd)
        obj = week1_score(
            nxt.implied_total or 0.0,
            nxt.salary,
            depth_rank=nxt.depth_rank,
            position=nxt.position,
            prop_fd=fd,
            pass_rate=nxt.pass_rate,
            opp_pass_rate=nxt.opp_pass_rate,
            team_spread=nxt.spread,
            rush_share=nxt.rush_share,
            target_share=nxt.target_share,
            opp_pass_ppa=nxt.opp_pass_ppa,
            opp_rush_ppa=nxt.opp_rush_ppa,
        )
        out.append(
            replace(
                pl,
                prop_fd=fd,
                prop_pass_yds=None if prop is None else prop.pass_yds,
                prop_pass_tds=None if prop is None else prop.pass_tds,
                prop_rush_yds=None if prop is None else prop.rush_yds,
                prop_rec_yds=None if prop is None else prop.rec_yds,
                prop_receptions=None if prop is None else prop.receptions,
                prop_book=None if prop is None else prop.book,
                prop_status=status,
                objective=obj,
            )
        )
    return out
