"""ESPN NFL injury report. Drop Out / Doubtful / IR / Suspension.

Ignore Active blurbs. Name join + Jr. strip. Unmatched names stay in the pool.
Cache: nfl/data/espn-injuries/.

Use site.web.api — site.api is often HTTP 403 from datacenter/residential egress.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from nfl.http import HttpError, http_json
from nfl.names import match_key
from nfl.players import Player
from nfl.projections import score_player
from nfl.teams import UnmappedTeam, lookup_odds, require_fd

ESPN_INJURIES = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "espn-injuries"
DROP_STATUSES = frozenset(
    {"out", "doubtful", "injured reserve", "ir", "suspension", "suspended"}
)
# FanDuel Injury Indicator, plus the words gangstash/ESPN use for the same
# states. Q is questionable and stays the starter. NA matches the optimizer
# pool filter (inactive, not a body part).
INACTIVE_CODES = frozenset({"O", "D", "IR", "NA", "SUSP"})
_CODE_WORDS = {
    "o": "O",
    "out": "O",
    "d": "D",
    "doubtful": "D",
    "ir": "IR",
    "injured reserve": "IR",
    "na": "NA",
    "q": "Q",
    "questionable": "Q",
    "susp": "SUSP",
    "suspension": "SUSP",
    "suspended": "SUSP",
}


class InjuryError(Exception):
    """Fatal ESPN injury ingest."""


@dataclass(frozen=True)
class InjuryRow:
    name: str
    team: str
    status: str
    gsis_id: str | None = None
    player_id: str | None = None


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


def _blank(value: object) -> bool:
    return value is None or value == ""


def _opt_id(row: dict, key: str) -> str | None:
    text = str(row.get(key) or "").strip()
    return text or None


def injury_rows_from_records(
    rows: list[dict],
    *,
    season: int,
    week: int,
) -> list[InjuryRow]:
    """Gangstash ``dataset=injuries`` rows for one season and week.

    ``season`` is required on the query. ``week``, ``team``, ``gsis_id``,
    and ``status`` may be absent. A row is kept with ``player_id`` or
    ``gsis_id`` even when the name is blank. A mismatched season or week
    is dropped. If any row carries a week, undated rows are dropped. If
    none do, they are kept: the query was already that season and week.
    """
    saw_week = any(
        isinstance(row, dict) and not _blank(row.get("week")) for row in rows
    )
    out: list[InjuryRow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_season = row.get("season")
        row_week = row.get("week")
        if not _blank(row_season):
            try:
                if int(row_season) != int(season):
                    continue
            except (TypeError, ValueError):
                continue
        if not _blank(row_week):
            try:
                if int(row_week) != int(week):
                    continue
            except (TypeError, ValueError):
                continue
        elif saw_week:
            continue
        name = str(row.get("player_name") or row.get("name") or "").strip()
        status = str(row.get("status") or "").strip()
        team_raw = str(row.get("team_fd") or row.get("team") or "").strip()
        gsis_id = _opt_id(row, "gsis_id")
        player_id = _opt_id(row, "player_id")
        if not name and not gsis_id and not player_id:
            continue
        team = ""
        if team_raw:
            try:
                team = require_fd(team_raw).fd
            except UnmappedTeam:
                if not gsis_id and not player_id:
                    continue
        elif not gsis_id and not player_id:
            continue
        out.append(
            InjuryRow(
                name=name,
                team=team,
                status=status,
                gsis_id=gsis_id,
                player_id=player_id,
            )
        )
    return out


def injury_code(status: str) -> str:
    """FanDuel letter for a CSV indicator or a gangstash/ESPN status word."""
    text = (status or "").strip()
    if not text:
        return ""
    mapped = _CODE_WORDS.get(text.casefold())
    if mapped:
        return mapped
    return text.upper()


def is_inactive(player: Player) -> bool:
    """O, D, IR, or NA. Questionable is still active."""
    return injury_code(player.injury) in INACTIVE_CODES


def stamp_injuries(players: list[Player], rows: list[InjuryRow]) -> list[Player]:
    """Write gangstash statuses onto matching players. CSV codes stay otherwise.

    A row matches ``Player.pid`` against ``gsis_id`` or ``player_id`` first
    (depth-chart pools use the gsis id as the pid), then ``(team, name)``.
    """
    by_id: dict[str, str] = {}
    by_key: dict[tuple[str, str], str] = {}
    for row in rows:
        code = injury_code(row.status)
        if not code:
            continue
        if row.gsis_id:
            by_id[row.gsis_id] = code
        if row.player_id:
            by_id[row.player_id] = code
        if row.name and row.team:
            by_key[(row.team, match_key(row.name))] = code
    out: list[Player] = []
    for pl in players:
        code = by_id.get(pl.pid) or by_key.get((pl.team, match_key(pl.name)))
        if not code:
            out.append(pl)
            continue
        out.append(replace(pl, injury=code))
    return out


def handoff_chart(players: list[Player]) -> list[Player]:
    """Promote the next healthy charted player when a starter is O, D, IR, or NA.

    Ranks are rewritten inside each team and position. Inactive charted
    players lose the slot. Unlisted players do not jump the chart.
    Questionable keeps the pre-game rank. Objectives are rescored.
    """
    groups: dict[tuple[str, str], list[Player]] = {}
    for pl in players:
        pos = (pl.position or "").upper()
        if pos in {"D", "DEF"}:
            continue
        groups.setdefault(((pl.team or "").upper(), pos), []).append(pl)
    new_rank: dict[str, int | None] = {}
    for group in groups.values():
        charted = [pl for pl in group if pl.depth_rank is not None]
        if not charted:
            continue
        healthy = [pl for pl in charted if not is_inactive(pl)]
        healthy.sort(
            key=lambda pl: (
                int(pl.depth_rank or 0),
                -(pl.salary or 0),
                pl.pid,
            )
        )
        for index, pl in enumerate(healthy, start=1):
            new_rank[pl.pid] = index
        for pl in charted:
            if pl.pid not in new_rank:
                new_rank[pl.pid] = None
    out: list[Player] = []
    for pl in players:
        if pl.pid not in new_rank or new_rank[pl.pid] == pl.depth_rank:
            out.append(pl)
            continue
        patched = replace(pl, depth_rank=new_rank[pl.pid])
        out.append(replace(patched, objective=score_player(patched)))
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
