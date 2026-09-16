"""Odds API NFL player-prop overlay.

Slate games only. Markets: pass/rush/rec yds, receptions, pass TDs.
Skip anytime_td this pass. FanDuel book else median Over.
NFL scoring includes 100/300 bonuses when the *line* is ≥ threshold.
Jr. name join. Volume line is a ±20% tilt on implied, not an override.
Missing props → model path (factor 1.0).
Cache: nfl/data/odds-props/YYYY-MM-DD/. ~60 credits + 1 events.
"""

from __future__ import annotations

import json
import statistics
import urllib.parse
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from nfl import env as envmod
from nfl.http import HttpError, http_json
from nfl.lines import filter_commence, infer_slate_date, slate_from_players, slate_window
from nfl.names import match_key
from nfl.players import Player
from nfl.projections import week1_score
from nfl.rules import FANDUEL_NFL
from nfl.teams import lookup_odds

ODDS_EVENTS = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events"
ODDS_EVENT_ODDS = (
    "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{eid}/odds"
)
CACHE_DIR = Path(__file__).resolve().parent / "data" / "odds-props"
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
        sc = FANDUEL_NFL.scoring
        pts = 0.0
        n = 0
        if self.pass_yds is not None:
            pts += self.pass_yds * sc["pass_yd"]
            if self.pass_yds >= 300:
                pts += sc["bonus_pass_yd_300"]
            n += 1
        if self.pass_tds is not None:
            pts += self.pass_tds * sc["pass_td"]
            n += 1
        if self.rush_yds is not None:
            pts += self.rush_yds * sc["rush_yd"]
            if self.rush_yds >= 100:
                pts += sc["bonus_rush_yd_100"]
            n += 1
        if self.rec_yds is not None:
            pts += self.rec_yds * sc["rec_yd"]
            if self.rec_yds >= 100:
                pts += sc["bonus_rec_yd_100"]
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
    if path.is_file() and path.stat().st_size > 2 and not refresh:
        return json.loads(path.read_text(encoding="utf-8")), None
    url = ODDS_EVENTS + "?" + urllib.parse.urlencode({"apiKey": _key()})
    try:
        payload, hdrs = http_json(url)
    except HttpError as e:
        raise PropsError(f"Odds events {e}") from e
    if not isinstance(payload, list):
        raise PropsError("Odds events did not return an array")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload, hdrs.get("x-requests-remaining")


def fetch_event_props(event_id: str, *, refresh: bool = False) -> tuple[dict, str | None]:
    path = _cache_day() / f"{event_id}.json"
    if path.is_file() and path.stat().st_size > 2 and not refresh:
        return json.loads(path.read_text(encoding="utf-8")), None
    params = {
        "apiKey": _key(),
        "regions": "us",
        "markets": ",".join(MARKETS),
        "oddsFormat": "american",
        "bookmakers": "fanduel,draftkings,betmgm,bovada",
    }
    url = ODDS_EVENT_ODDS.format(eid=event_id) + "?" + urllib.parse.urlencode(params)
    try:
        payload, hdrs = http_json(url)
    except HttpError as e:
        raise PropsError(f"Odds event {event_id}: {e}") from e
    if not isinstance(payload, dict):
        raise PropsError(f"Odds event {event_id} did not return an object")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload, hdrs.get("x-requests-remaining")


def _median_over(points: list[float]) -> float | None:
    if not points:
        return None
    return float(statistics.median(points))


def _collect_overs(book: dict) -> dict[str, dict[str, float]]:
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
    slate_day: date | None = None,
) -> tuple[dict[str, PlayerProp], dict]:
    events, remaining = fetch_events(refresh=refresh)
    day = slate_day or infer_slate_date()
    start, end = slate_window(day)
    events = filter_commence(events, start, end)
    slate = slate_from_players(pool)
    joined: dict[str, PlayerProp] = {}
    unmatched: list[dict] = []
    games_hit = 0
    games_miss = 0
    last_remaining = remaining
    for _game, away_fd, home_fd in slate:
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


def _unmatched_lasts(unmatched: list, team: str) -> set[str]:
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
        if pl.position == "D":
            status = None
            obj = week1_score(
                pl.implied_total or 0.0,
                depth_rank=pl.depth_rank,
                position=pl.position,
                implied_opp=pl.implied_opp,
            )
            out.append(replace(pl, objective=obj, prop_status=status))
            continue
        if prop is not None and prop.has_volume():
            status = "props"
        elif prop is not None:
            status = "props"
            fd = prop.fd_points()
        else:
            lasts = _unmatched_lasts(um, pl.team)
            last = match_key(pl.name).split()[-1] if pl.name else ""
            status = "unmatched" if last and last in lasts else "no_market"
            fd = None
        # volume props join → prop_fd (week1_score tilts ±20%, does not override)
        use_fd = fd if (prop is not None and prop.has_volume()) else None
        obj = week1_score(
            pl.implied_total or 0.0,
            depth_rank=pl.depth_rank,
            position=pl.position,
            prop_fd=use_fd,
            implied_opp=pl.implied_opp,
        )
        out.append(
            replace(
                pl,
                prop_fd=use_fd,
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
