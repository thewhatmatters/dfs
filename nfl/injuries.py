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
# Nightly projections. Out and IR leave the sim pool. Doubtful stays
# at full value and is only flagged. Questionable is unchanged.
POOL_OUT_CODES = frozenset({"O", "IR", "NA", "SUSP"})
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
    player_key: str | None = None


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
    and ``status`` may be absent. Live rows use ``full_name``,
    ``report_status``, ``gsis_id``, and ``player_key`` (``practice_status``
    and ``date_modified`` are ignored). Older rows used ``player_name`` /
    ``name``, ``status``, and ``player_id``. Both shapes parse.

    A row is kept with ``player_id``, ``gsis_id``, or ``player_key`` even
    when the name is blank. A mismatched season or week is dropped. If
    any row carries a week, undated rows are dropped. If none do, they
    are kept: the query was already that season and week.
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
        name = str(
            row.get("full_name") or row.get("player_name") or row.get("name") or ""
        ).strip()
        status = str(row.get("report_status") or row.get("status") or "").strip()
        team_raw = str(row.get("team_fd") or row.get("team") or "").strip()
        player_key = _opt_id(row, "player_key")
        gsis_id = _opt_id(row, "gsis_id") or player_key
        player_id = _opt_id(row, "player_id")
        if not name and not gsis_id and not player_id and not player_key:
            continue
        team = ""
        if team_raw:
            try:
                team = require_fd(team_raw).fd
            except UnmappedTeam:
                if not gsis_id and not player_id and not player_key:
                    continue
        elif not gsis_id and not player_id and not player_key:
            continue
        out.append(
            InjuryRow(
                name=name,
                team=team,
                status=status,
                gsis_id=gsis_id,
                player_id=player_id,
                player_key=player_key,
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


def is_pool_out(player: Player) -> bool:
    """Out, IR, NA, or suspended. Doubtful and Questionable stay."""
    return injury_code(player.injury) in POOL_OUT_CODES


def stamp_injuries(players: list[Player], rows: list[InjuryRow]) -> list[Player]:
    """Write gangstash statuses onto matching players. CSV codes stay otherwise.

    A row matches ``Player.pid`` against ``gsis_id``, ``player_key``, or
    ``player_id`` first (depth-chart pools use the gsis id as the pid),
    then ``(team, name)``.
    """
    by_id: dict[str, str] = {}
    by_key: dict[tuple[str, str], str] = {}
    for row in rows:
        code = injury_code(row.status)
        if not code:
            continue
        for ident in (row.gsis_id, row.player_key, row.player_id):
            if ident:
                by_id[ident] = code
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


def _zero_out(player: Player) -> Player:
    """Out of the sim. Objective is 0 so a board row cannot keep the old mean."""
    return replace(
        player,
        depth_rank=None,
        target_share=None,
        snap_share=None,
        objective=0.0,
    )


def promote_out_chart(players: list[Player]) -> list[Player]:
    """Fill an Out/IR/NA/SUSP depth slot with the next player at that position.

    Ranks are rewritten inside each team and position. The player who
    steps into a vacated slot inherits that slot's target and snap share
    when the vacated player had one, then the objective is rescored.
    That is the starter role coefficient and usage share. Doubtful and
    Questionable keep their rank, share, and objective. An out player's
    objective is 0.
    """
    groups: dict[tuple[str, str], list[Player]] = {}
    for pl in players:
        pos = (pl.position or "").upper()
        if pos in {"D", "DEF"}:
            continue
        groups.setdefault(((pl.team or "").upper(), pos), []).append(pl)
    updates: dict[str, Player] = {}
    for group in groups.values():
        charted = [pl for pl in group if pl.depth_rank is not None]
        charted.sort(
            key=lambda pl: (
                int(pl.depth_rank or 0),
                -(pl.salary or 0),
                pl.pid,
            )
        )
        healthy = [pl for pl in charted if not is_pool_out(pl)]
        for index, pl in enumerate(healthy):
            role = charted[index]
            rank = index + 1
            if role.pid == pl.pid:
                target = pl.target_share
                snap = pl.snap_share
            else:
                target = role.target_share if role.target_share is not None else pl.target_share
                snap = role.snap_share if role.snap_share is not None else pl.snap_share
            if rank == pl.depth_rank and target == pl.target_share and snap == pl.snap_share:
                continue
            patched = replace(pl, depth_rank=rank, target_share=target, snap_share=snap)
            updates[pl.pid] = replace(patched, objective=score_player(patched))
        for pl in group:
            if is_pool_out(pl):
                updates[pl.pid] = _zero_out(pl)
    return [updates.get(pl.pid, pl) for pl in players]


def _csv_injury_index(
    csv_players: list[Player] | None,
) -> dict[tuple[str, str, str], str]:
    """``(team, match_key, position) → injury code`` from a FanDuel CSV."""
    out: dict[tuple[str, str, str], str] = {}
    for pl in csv_players or []:
        code = injury_code(pl.injury)
        if not code:
            continue
        try:
            team = require_fd(pl.team).fd
        except UnmappedTeam:
            continue
        pos = (pl.position or "").upper()
        if pos in {"DEF", "DST"}:
            pos = "D"
        out[(team, match_key(pl.name), pos)] = code
    return out


def _overlay_csv_injury(
    players: list[Player],
    csv_players: list[Player] | None,
) -> list[Player]:
    """FanDuel ``O`` / ``IR`` / ``NA`` forces a player out.

    ``Q`` and ``D`` fill a blank injury and do not clear a gangstash code.
    """
    index = _csv_injury_index(csv_players)
    if not index:
        return players
    out: list[Player] = []
    for pl in players:
        pos = (pl.position or "").upper()
        if pos in {"DEF", "DST"}:
            pos = "D"
        code = index.get((pl.team, match_key(pl.name), pos))
        if not code:
            out.append(pl)
            continue
        if code in POOL_OUT_CODES:
            if injury_code(pl.injury) == code:
                out.append(pl)
                continue
            out.append(replace(pl, injury=code))
            continue
        if pl.injury:
            out.append(pl)
            continue
        out.append(replace(pl, injury=code))
    return out


def apply_projection_injuries(
    players: list[Player],
    rows: list[dict] | None,
    *,
    season: int,
    week: int,
    csv_players: list[Player] | None = None,
) -> list[Player]:
    """Stamp the week, honor a CSV O/IR, then promote the vacated role.

    ``rows`` are raw gangstash injury records. ``None`` skips that feed.
    An empty list means the week had no injury rows.
    """
    stamped = players
    if rows is not None:
        parsed = injury_rows_from_records(rows, season=season, week=week)
        stamped = stamp_injuries(players, parsed)
    overlaid = _overlay_csv_injury(stamped, csv_players)
    return promote_out_chart(overlaid)


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
