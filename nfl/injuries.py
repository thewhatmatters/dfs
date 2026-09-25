"""ESPN NFL injury report. Drop Out / Doubtful / IR / Suspension.

Ignore Active blurbs. Name join + Jr. strip. Unmatched names stay in the pool.
Cache: nfl/data/espn-injuries/.

Use site.web.api — site.api is often HTTP 403 from datacenter/residential egress.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from nfl.http import HttpError, http_json
from nfl.names import match_key
from nfl.players import Player
from nfl.teams import UnmappedTeam, lookup_odds, require_fd

ESPN_INJURIES = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "espn-injuries"
DROP_STATUSES = frozenset(
    {"out", "doubtful", "injured reserve", "ir", "suspension", "suspended"}
)


class InjuryError(Exception):
    """Fatal ESPN injury ingest."""


@dataclass(frozen=True)
class InjuryRow:
    name: str
    team: str
    status: str


def _cache_path() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{date.today().isoformat()}.json"


def fetch_injuries(*, refresh: bool = False) -> dict:
    path = _cache_path()
    if path.is_file() and path.stat().st_size > 2 and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        payload, _hdrs = http_json(ESPN_INJURIES)
    except HttpError as e:
        raise InjuryError(f"ESPN injuries {e}") from e
    if not isinstance(payload, dict):
        raise InjuryError("ESPN injuries did not return an object")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _team_fd(team_block: dict, athlete: dict) -> str | None:
    abbr = ""
    team = athlete.get("team") if isinstance(athlete.get("team"), dict) else None
    if team:
        abbr = str(team.get("abbreviation") or "")
    if not abbr:
        abbr = str(team_block.get("abbreviation") or "")
    ref = lookup_odds(abbr) if abbr else None
    if ref is not None:
        return ref.fd
    name = str(team_block.get("displayName") or team_block.get("name") or "")
    ref = lookup_odds(name)
    return ref.fd if ref else None


def parse_injuries(payload: dict) -> list[InjuryRow]:
    out: list[InjuryRow] = []
    for team_block in payload.get("injuries") or []:
        if not isinstance(team_block, dict):
            continue
        for row in team_block.get("injuries") or []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip()
            if not status or status.casefold() == "active":
                continue
            athlete = row.get("athlete") if isinstance(row.get("athlete"), dict) else {}
            name = str(athlete.get("displayName") or "").strip()
            if not name:
                continue
            fd = _team_fd(team_block, athlete)
            if not fd:
                continue
            out.append(InjuryRow(name=name, team=fd, status=status))
    return out


def injury_rows_from_records(
    rows: list[dict],
    *,
    season: int,
    week: int,
) -> list[InjuryRow]:
    """Gangstash ``dataset=injuries`` rows for one season and week.

    A row with no ``season`` or ``week`` is a live dump and is dropped so
    today's report is not applied to a past week.
    """
    out: list[InjuryRow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_season = row.get("season")
        row_week = row.get("week")
        if row_season in (None, "") or row_week in (None, ""):
            continue
        try:
            if int(row_season) != int(season) or int(row_week) != int(week):
                continue
        except (TypeError, ValueError):
            continue
        name = str(row.get("player_name") or row.get("name") or "").strip()
        status = str(row.get("status") or "").strip()
        team_raw = str(row.get("team_fd") or row.get("team") or "").strip()
        if not name or not status or not team_raw:
            continue
        try:
            team = require_fd(team_raw).fd
        except UnmappedTeam:
            continue
        out.append(InjuryRow(name=name, team=team, status=status))
    return out


def drop_keys(rows: list[InjuryRow]) -> set[tuple[str, str]]:
    """(team, match_key) for Out / Doubtful / IR / Suspension."""
    keys: set[tuple[str, str]] = set()
    for r in rows:
        if r.status.strip().casefold() in DROP_STATUSES:
            keys.add((r.team, match_key(r.name)))
    return keys


def apply_injuries(
    players: list[Player],
    rows: list[InjuryRow],
) -> tuple[list[Player], dict]:
    """Drop joined Out/Doubtful/IR/Suspension. Unmatched ESPN names stay unused."""
    drop = drop_keys(rows)
    pool_keys = {(p.team, match_key(p.name)) for p in players}
    unmatched = sorted(
        f"{r.team} {r.name}"
        for r in rows
        if r.status.strip().casefold() in DROP_STATUSES
        and (r.team, match_key(r.name)) not in pool_keys
    )
    kept: list[Player] = []
    dropped = 0
    for pl in players:
        if (pl.team, match_key(pl.name)) in drop:
            dropped += 1
            continue
        kept.append(pl)
    stats = {
        "dropped": dropped,
        "unmatched": len(unmatched),
        "unmatched_names": unmatched,
        "drop_rows": len(drop),
    }
    return kept, stats


def ingest_slate_injuries(
    players: list[Player],
    *,
    refresh: bool = False,
) -> tuple[list[Player], dict]:
    payload = fetch_injuries(refresh=refresh)
    rows = parse_injuries(payload)
    return apply_injuries(players, rows)
