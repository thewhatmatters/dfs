"""NFL player-prop overlay from the gangstash props API.

Volume lines (pass/rush/rec yards, receptions, pass TDs) are a ±20% tilt on
implied, not an override. NFL scoring includes 100/300 bonuses when the *line*
is ≥ threshold. Jr. name join via match_key. Missing props → model path
(factor 1.0).

Game lines (spreads/totals) stay on The Odds API in nfl/lines.py. This module
does not call Odds and does not need ODDS_API_KEY.

Prop strings: see PROP_FIELD. Unmapped `prop` values are counted and returned
as unmapped_props (exact strings). They are not scored.
Live BettingPros board (2026-09-24, 671 rows): Pass YDs, Pass TDs, Rush YDs,
Rec YDs, and Recs are scored. INTs, Pass ATTs, Pass CMPs, Rush ATTs, and
Rsh + Rec stay unmapped.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

from nfl.gangstash import (
    GangstashError,
    GangstashKeyMissing,
    fetch_props,
)
from nfl.lines import slate_from_players
from nfl.names import match_key
from nfl.players import Player
from nfl.projections import week1_score
from nfl.rules import FANDUEL_NFL

# Normalized prop string → PlayerProp field. Normalization is casefold, strip
# punctuation, collapse space, drop a trailing over/under.
PROP_FIELD = {
    "passing yards": "pass_yds",
    "pass yards": "pass_yds",
    "pass yds": "pass_yds",
    "passing yds": "pass_yds",
    "player pass yds": "pass_yds",
    "player passing yards": "pass_yds",
    "passing touchdowns": "pass_tds",
    "passing tds": "pass_tds",
    "pass tds": "pass_tds",
    "pass td": "pass_tds",
    "passing td": "pass_tds",
    "player pass tds": "pass_tds",
    "player passing touchdowns": "pass_tds",
    "rushing yards": "rush_yds",
    "rush yards": "rush_yds",
    "rush yds": "rush_yds",
    "rushing yds": "rush_yds",
    "player rush yds": "rush_yds",
    "player rushing yards": "rush_yds",
    "receiving yards": "rec_yds",
    "rec yards": "rec_yds",
    "rec yds": "rec_yds",
    "receiving yds": "rec_yds",
    "reception yards": "rec_yds",
    "player reception yds": "rec_yds",
    "player receiving yards": "rec_yds",
    "receptions": "receptions",
    "player receptions": "receptions",
    "recs": "receptions",
    "rec": "receptions",
}

_TRAILING_SIDE = re.compile(r"\s+(?:over under|over|under)$")


class PropsError(GangstashError):
    """Fatal props ingest."""


class PropsKeyMissing(GangstashKeyMissing):
    gate = "PROPS_GANGSTASH_KEY"


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


def norm_prop(raw: str) -> str:
    s = (raw or "").casefold().replace("_", " ").replace("-", " ").replace("/", " ")
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _TRAILING_SIDE.sub("", s).strip()


def prop_field(raw: str) -> str | None:
    return PROP_FIELD.get(norm_prop(raw))


def _as_line(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scraped_at(row: dict) -> str:
    return str(row.get("scraped_at") or "")


def _as_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_stamp(value: object) -> datetime | None:
    """ISO timestamp. Naive values are treated as UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def props_for_week(
    rows: list[dict],
    *,
    season: int | None,
    week: int | None,
    kickoff: datetime | None = None,
) -> list[dict]:
    """Props that were knowable before this week.

    ``week is None`` keeps the live board (optimizer). A historical week
    keeps only rows whose ``season`` and ``week`` match, and only the
    latest ``scraped_at`` strictly before ``kickoff``. Rows with no week
    are the current board and are dropped. No surviving snapshot is an
    empty list — do not fill the week from a later board.
    """
    if week is None:
        return [row for row in rows if isinstance(row, dict)]
    kept: list[tuple[datetime | None, dict]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_season = _as_int(row.get("season"))
        row_week = _as_int(row.get("week"))
        if row_season is None or row_week is None:
            continue
        if season is not None and row_season != int(season):
            continue
        if row_week != int(week):
            continue
        stamp = parse_stamp(row.get("scraped_at"))
        if kickoff is not None and (stamp is None or stamp >= kickoff):
            continue
        kept.append((stamp, row))
    if not kept:
        return []
    dated = [stamp for stamp, _row in kept if stamp is not None]
    if not dated:
        return [row for _stamp, row in kept]
    latest = max(dated)
    return [row for stamp, row in kept if stamp == latest]


def rows_to_props(rows: list[dict]) -> tuple[dict[str, PlayerProp], dict]:
    """Collapse board rows into one PlayerProp per match_key.

    Newer scraped_at wins for the same player and field. Same timestamp and a
    different line keeps the first value and records a conflict. Unknown prop
    strings are counted under their exact text.
    """
    # match_key -> field -> (line, scraped_at, display_name)
    acc: dict[str, dict[str, tuple[float, str, str]]] = {}
    unmapped: dict[str, int] = {}
    skipped = 0
    conflicts: list[dict] = []
    mapped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        name = str(row.get("player_name") or "").strip()
        raw_prop = row.get("prop")
        line = _as_line(row.get("line"))
        if not name or raw_prop is None or line is None:
            skipped += 1
            continue
        field = prop_field(str(raw_prop))
        if field is None:
            exact = str(raw_prop)
            unmapped[exact] = unmapped.get(exact, 0) + 1
            continue
        mapped += 1
        key = match_key(name)
        stamp = _scraped_at(row)
        slot = acc.setdefault(key, {})
        prev = slot.get(field)
        if prev is None or stamp > prev[1]:
            slot[field] = (line, stamp, name)
            continue
        if stamp == prev[1] and line != prev[0]:
            conflicts.append(
                {
                    "player": name,
                    "prop": str(raw_prop),
                    "kept": prev[0],
                    "dropped": line,
                }
            )
    props: dict[str, PlayerProp] = {}
    for key, fields in acc.items():
        display = next(iter(fields.values()))[2]
        kwargs = {field: fields[field][0] for field in fields}
        prop = PlayerProp(name=display, book="gangstash", **kwargs)
        if prop.has_volume() or prop.pass_tds is not None:
            props[key] = prop
    meta = {
        "mapped_rows": mapped,
        "skipped_rows": skipped,
        "unmapped_props": unmapped,
        "conflicts": conflicts,
    }
    return props, meta


def ingest_slate_props(
    pool: list[Player],
    *,
    refresh: bool = False,
    slate_day: date | None = None,
) -> tuple[dict[str, PlayerProp], dict]:
    day = slate_day or date.today()
    try:
        rows, fetch_meta = fetch_props(refresh=refresh, cache_day=day)
    except GangstashKeyMissing as e:
        raise PropsKeyMissing(str(e)) from e
    except GangstashError as e:
        raise PropsError(str(e)) from e
    by_key, map_meta = rows_to_props(rows)
    buckets: dict[str, list[Player]] = defaultdict(list)
    for pl in pool:
        if pl.position == "D":
            continue
        buckets[match_key(pl.name)].append(pl)
    joined: dict[str, PlayerProp] = {}
    unmatched: list[dict] = []
    for key, prop in by_key.items():
        hits = buckets.get(key) or []
        pids = {pl.pid: pl for pl in hits}
        if len(pids) == 1:
            joined[next(iter(pids))] = prop
            continue
        if len(pids) > 1:
            unmatched.append({"name": prop.name, "reason": "ambiguous"})
    joined_teams = {pl.team for pl in pool if pl.pid in joined}
    games_hit = 0
    games_miss = 0
    for _game, away_fd, home_fd in slate_from_players(pool):
        if away_fd in joined_teams or home_fd in joined_teams:
            games_hit += 1
        else:
            games_miss += 1
    stats = {
        "source": "gangstash",
        "games_with_props": games_hit,
        "games_unmatched": games_miss,
        "players_with_props": len(joined),
        "rows": len(rows),
        "markets": ["pass_yds", "pass_tds", "rush_yds", "rec_yds", "receptions"],
        "unmatched": unmatched,
        **fetch_meta,
        **map_meta,
    }
    return joined, stats


def _unmatched_keys(unmatched: list) -> set[str]:
    out: set[str] = set()
    for u in unmatched:
        if isinstance(u, dict):
            name = str(u.get("name") or "")
        else:
            name = getattr(u, "name", "") or ""
        key = match_key(name)
        if key:
            out.add(key)
    return out


def attach_props(
    players: list[Player],
    by_pid: dict[str, PlayerProp],
    unmatched: list | None = None,
) -> list[Player]:
    ambiguous = _unmatched_keys(unmatched or [])
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
                target_share=pl.target_share,
                snap_share=pl.snap_share,
            )
            out.append(replace(pl, objective=obj, prop_status=status))
            continue
        if prop is not None and prop.has_volume():
            status = "props"
        elif prop is not None:
            status = "props"
            fd = prop.fd_points()
        else:
            status = "unmatched" if match_key(pl.name) in ambiguous else "no_market"
            fd = None
        use_fd = fd if (prop is not None and prop.has_volume()) else None
        obj = week1_score(
            pl.implied_total or 0.0,
            depth_rank=pl.depth_rank,
            position=pl.position,
            prop_fd=use_fd,
            implied_opp=pl.implied_opp,
            target_share=pl.target_share,
            snap_share=pl.snap_share,
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
