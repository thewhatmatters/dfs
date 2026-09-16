"""CFBD 2026 team pass mix and player usage.

Team `pass_rate` / `opp_pass_rate` / opponent def PPA feed `script_mult`. Player `rush_share`
(RB) and `target_share` (WR/TE) replace the OurLads depth prior on the
implied×share path. Props tilt that base ±20%; they do not skip mix.

Empty 2026 → no-op (spread-only script, depth prior). Never use 2025.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path
from ncaaf import env as envmod
from ncaaf.lines import LinesAuthError, _http_json
from ncaaf.ourlads import match_key
from ncaaf.players import Player
from ncaaf.projections import score_player
from ncaaf.teams import TEAMS

CFBD_ADV_URL = "https://api.collegefootballdata.com/stats/season/advanced"
CFBD_USAGE_URL = "https://api.collegefootballdata.com/player/usage"
USER_AGENT = "dfs-ncaaf/0.1 (cfbd-mix)"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "cfbd-mix"


class MixError(Exception):
    """Fatal CFBD mix ingest."""


@dataclass(frozen=True)
class TeamMix:
    pass_rate: float
    opp_pass_rate: float | None  # pass share faced by this team's defense
    def_rush_ppa: float | None  # defense rushingPlays.ppa (higher = worse)
    def_pass_ppa: float | None  # defense passingPlays.ppa


@dataclass(frozen=True)
class PlayerUsage:
    team_cfbd: str
    name: str
    position: str
    rush: float | None
    catch: float | None  # usage.pass — share of team pass plays


def _headers(key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {key}",
    }


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def _load_or_fetch(url: str, cache_name: str, *, refresh: bool) -> list[dict]:
    path = _cache_path(cache_name)
    if path.is_file() and not refresh:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return raw
    key = envmod.get("CFBD_API_KEY")
    if not key:
        raise MixError("no CFBD_API_KEY")
    try:
        payload = _http_json(url, _headers(key), timeout=60)
    except LinesAuthError as e:
        raise MixError(str(e)) from e
    if not isinstance(payload, list):
        raise MixError(f"CFBD mix did not return an array: {url}")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def parse_team_mix(rows: list[dict]) -> dict[str, TeamMix]:
    """CFBD school casefold → TeamMix. Skip rows with no passingPlays.rate."""
    out: dict[str, TeamMix] = {}
    for row in rows:
        school = (row.get("team") or "").strip()
        if not school:
            continue
        off = row.get("offense") or {}
        de = row.get("defense") or {}
        rate = (off.get("passingPlays") or {}).get("rate")
        if rate is None:
            continue
        faced = (de.get("passingPlays") or {}).get("rate")
        rush_ppa = (de.get("rushingPlays") or {}).get("ppa")
        pass_ppa = (de.get("passingPlays") or {}).get("ppa")
        out[school.casefold()] = TeamMix(
            pass_rate=float(rate),
            opp_pass_rate=None if faced is None else float(faced),
            def_rush_ppa=None if rush_ppa is None else float(rush_ppa),
            def_pass_ppa=None if pass_ppa is None else float(pass_ppa),
        )
    return out


def parse_usage(rows: list[dict]) -> dict[tuple[str, str], PlayerUsage]:
    """(cfbd school casefold, match_key) → usage."""
    out: dict[tuple[str, str], PlayerUsage] = {}
    for row in rows:
        school = (row.get("team") or "").strip()
        name = (row.get("name") or "").strip()
        if not school or not name:
            continue
        blob = row.get("usage") or {}
        rush = blob.get("rush")
        catch = blob.get("pass")
        rec = PlayerUsage(
            team_cfbd=school,
            name=name,
            position=(row.get("position") or "").strip().upper(),
            rush=None if rush is None else float(rush),
            catch=None if catch is None else float(catch),
        )
        out[(school.casefold(), match_key(name))] = rec
    return out


def ingest_mix(
    year: int,
    *,
    refresh: bool = False,
) -> tuple[dict[str, TeamMix], dict[tuple[str, str], PlayerUsage]]:
    """Season-to-date advanced + usage. Year < 2026 or no key → ({}, {})."""
    if year < 2026:
        return {}, {}
    envmod.load()
    if not envmod.get("CFBD_API_KEY"):
        return {}, {}
    adv_url = CFBD_ADV_URL + "?" + urllib.parse.urlencode({"year": str(year)})
    use_url = CFBD_USAGE_URL + "?" + urllib.parse.urlencode({"year": str(year)})
    advanced = _load_or_fetch(
        adv_url, f"{year}-season-advanced.json", refresh=refresh
    )
    usage_rows = _load_or_fetch(
        use_url, f"{year}-player-usage.json", refresh=refresh
    )
    return parse_team_mix(advanced), parse_usage(usage_rows)


def attach_mix(
    players: list[Player],
    team_mix: dict[str, TeamMix],
    usage: dict[tuple[str, str], PlayerUsage],
) -> tuple[list[Player], dict[str, int]]:
    """Set pass_rate / opp_pass_rate / rush_share / target_share; rescore."""
    out: list[Player] = []
    teams_hit = 0
    usage_hit = 0
    seen_team: set[str] = set()
    for pl in players:
        school = _school_for_fd(pl.team)
        opp_school = _school_for_fd(pl.opponent) if pl.opponent else None
        mix = team_mix.get(school) if school else None
        opp_mix = team_mix.get(opp_school) if opp_school else None
        pass_rate = mix.pass_rate if mix else None
        # Pass share **faced** by the opponent's defense.
        opp_pass_rate = opp_mix.opp_pass_rate if opp_mix else None
        opp_rush_ppa = opp_mix.def_rush_ppa if opp_mix else None
        opp_pass_ppa = opp_mix.def_pass_ppa if opp_mix else None
        if mix and pl.team not in seen_team:
            teams_hit += 1
            seen_team.add(pl.team)
        rush = catch = None
        if school:
            hit = usage.get((school, match_key(pl.name)))
            if hit is not None:
                usage_hit += 1
                rush, catch = hit.rush, hit.catch
        nxt = replace(
            pl,
            pass_rate=pass_rate,
            opp_pass_rate=opp_pass_rate,
            opp_rush_ppa=opp_rush_ppa,
            opp_pass_ppa=opp_pass_ppa,
            rush_share=rush,
            target_share=catch,
        )
        if nxt.implied_total is not None:
            nxt = replace(nxt, objective=score_player(nxt))
        out.append(nxt)
    stats = {
        "teams_with_mix": teams_hit,
        "usage_joined": usage_hit,
        "players": len(players),
        "year_rows": len(team_mix),
    }
    return out, stats


def _school_for_fd(abbrev: str) -> str | None:
    ref = TEAMS.get((abbrev or "").strip().upper())
    if ref is None:
        return None
    return ref.cfbd.casefold()
