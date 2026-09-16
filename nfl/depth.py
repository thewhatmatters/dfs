"""ESPN NFL depth charts for slate teams only.

Rank = list order (1 = starter). WR rows are X/Y/Z — multiple rank-1s OK.
Unlisted skill prior is applied later (0.05). Cache: nfl/data/espn-depth/.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from nfl.http import HttpError, http_json
from nfl.names import match_key
from nfl.players import Player
from nfl.teams import TeamRef, require_mapped

ESPN_DEPTH = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{id}/depthcharts"
)
CACHE_DIR = Path(__file__).resolve().parent / "data" / "espn-depth"

# Offense alignment keys → FanDuel position. wr1/wr2/wr3 are X/Y/Z.
SKILL_POS = {
    "qb": "QB",
    "rb": "RB",
    "hb": "RB",
    "te": "TE",
    "wr": "WR",
    "wr1": "WR",
    "wr2": "WR",
    "wr3": "WR",
    "wrx": "WR",
    "wry": "WR",
    "wrz": "WR",
    "lwr": "WR",
    "rwr": "WR",
    "slwr": "WR",
    "slot": "WR",
}


class DepthError(Exception):
    """Fatal ESPN depth ingest."""


@dataclass(frozen=True)
class DepthRow:
    team: str
    pos: str
    rank: int
    name: str
    alignment: str


def _day_dir() -> Path:
    d = CACHE_DIR / date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_team_depth(espn_id: str, *, refresh: bool = False) -> dict:
    path = _day_dir() / f"{espn_id}.json"
    if path.is_file() and path.stat().st_size > 2 and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    url = ESPN_DEPTH.format(id=espn_id)
    try:
        payload, _hdrs = http_json(url)
    except HttpError as e:
        raise DepthError(f"ESPN depth team {espn_id}: {e}") from e
    if not isinstance(payload, dict):
        raise DepthError(f"ESPN depth team {espn_id} did not return an object")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _offense_chart(charts: list[dict]) -> dict | None:
    best = None
    best_n = -1
    for ch in charts:
        if not isinstance(ch, dict):
            continue
        pos = ch.get("positions") or {}
        n = sum(1 for k in pos if str(k).lower() in SKILL_POS)
        if n > best_n:
            best, best_n = ch, n
    return best


def parse_team_depth(payload: dict, team: TeamRef) -> list[DepthRow]:
    charts = payload.get("depthchart") or payload.get("items") or []
    if not isinstance(charts, list):
        return []
    offense = _offense_chart(charts)
    if offense is None:
        return []
    rows: list[DepthRow] = []
    positions = offense.get("positions") or {}
    for key, block in positions.items():
        pos = SKILL_POS.get(str(key).lower())
        if pos is None or not isinstance(block, dict):
            continue
        athletes = block.get("athletes") or []
        for i, ath in enumerate(athletes):
            if not isinstance(ath, dict):
                continue
            nested = ath.get("athlete") if isinstance(ath.get("athlete"), dict) else None
            src = nested or ath
            name = str(src.get("displayName") or src.get("fullName") or "").strip()
            if not name:
                continue
            rank = i + 1
            rows.append(
                DepthRow(
                    team=team.fd,
                    pos=pos,
                    rank=rank,
                    name=name,
                    alignment=str(key).lower(),
                )
            )
    return rows


def ingest_slate_depth(
    slate_teams: set[str],
    *,
    refresh: bool = False,
) -> list[DepthRow]:
    refs = require_mapped(slate_teams)
    rows: list[DepthRow] = []
    for fd in sorted(refs):
        ref = refs[fd]
        payload = fetch_team_depth(ref.espn_id, refresh=refresh)
        rows.extend(parse_team_depth(payload, ref))
    return rows


def depth_index(rows: list[DepthRow]) -> dict[tuple[str, str], int]:
    """(team, match_key) → best (min) rank across alignments."""
    out: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r.team, match_key(r.name))
        prev = out.get(key)
        if prev is None or r.rank < prev:
            out[key] = r.rank
    return out


def attach_depth_ranks(
    players: list[Player],
    rows: list[DepthRow],
    score_fn=None,
) -> tuple[list[Player], dict]:
    idx = depth_index(rows)
    out: list[Player] = []
    matched = 0
    for pl in players:
        if pl.position == "D":
            out.append(pl)
            continue
        rank = idx.get((pl.team, match_key(pl.name)))
        if rank is not None:
            matched += 1
        obj = pl.objective
        if score_fn is not None:
            obj = score_fn(pl, rank)
        out.append(replace(pl, depth_rank=rank, objective=obj))
    stats = {
        "depth_rows": len(rows),
        "players": len(players),
        "matched": matched,
        "unmatched_players": sum(1 for p in players if p.position != "D") - matched,
    }
    return out, stats
