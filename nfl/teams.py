"""FanDuel NFL abbrev ↔ Odds API full name ↔ ESPN id/abbr.

JAC (FanDuel) ↔ JAX (Odds/ESPN). WAS (FanDuel) ↔ WSH (ESPN).
Unmapped FanDuel teams fail loud — do not guess.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TeamRef:
    fd: str
    odds: tuple[str, ...]
    espn_id: str
    espn_abbr: str


def _t(fd: str, espn_id: str, espn_abbr: str, *odds: str) -> TeamRef:
    names = odds if odds else (fd,)
    return TeamRef(fd=fd, odds=names, espn_id=str(espn_id), espn_abbr=espn_abbr)


# 32 NFL clubs. Odds names are The Odds API `americanfootball_nfl` full names.
TEAMS: dict[str, TeamRef] = {
    r.fd: r
    for r in (
        _t("ARI", "22", "ARI", "Arizona Cardinals"),
        _t("ATL", "1", "ATL", "Atlanta Falcons"),
        _t("BAL", "33", "BAL", "Baltimore Ravens"),
        _t("BUF", "2", "BUF", "Buffalo Bills"),
        _t("CAR", "29", "CAR", "Carolina Panthers"),
        _t("CHI", "3", "CHI", "Chicago Bears"),
        _t("CIN", "4", "CIN", "Cincinnati Bengals"),
        _t("CLE", "5", "CLE", "Cleveland Browns"),
        _t("DAL", "6", "DAL", "Dallas Cowboys"),
        _t("DEN", "7", "DEN", "Denver Broncos"),
        _t("DET", "8", "DET", "Detroit Lions"),
        _t("GB", "9", "GB", "Green Bay Packers"),
        _t("HOU", "34", "HOU", "Houston Texans"),
        _t("IND", "11", "IND", "Indianapolis Colts"),
        _t("JAC", "30", "JAX", "Jacksonville Jaguars", "JAX"),
        _t("KC", "12", "KC", "Kansas City Chiefs"),
        _t("LV", "13", "LV", "Las Vegas Raiders"),
        _t("LAC", "24", "LAC", "Los Angeles Chargers"),
        _t("LAR", "14", "LAR", "Los Angeles Rams"),
        _t("MIA", "15", "MIA", "Miami Dolphins"),
        _t("MIN", "16", "MIN", "Minnesota Vikings"),
        _t("NE", "17", "NE", "New England Patriots"),
        _t("NO", "18", "NO", "New Orleans Saints"),
        _t("NYG", "19", "NYG", "New York Giants"),
        _t("NYJ", "20", "NYJ", "New York Jets"),
        _t("PHI", "21", "PHI", "Philadelphia Eagles"),
        _t("PIT", "23", "PIT", "Pittsburgh Steelers"),
        _t("SF", "25", "SF", "San Francisco 49ers"),
        _t("SEA", "26", "SEA", "Seattle Seahawks"),
        _t("TB", "27", "TB", "Tampa Bay Buccaneers"),
        _t("TEN", "10", "TEN", "Tennessee Titans"),
        _t("WAS", "28", "WSH", "Washington Commanders", "WSH"),
    )
}

# FanDuel uses JAC/WAS/LAR. ESPN/Odds use JAX/WSH. nflverse uses JAX and LA (Rams).
ALIASES: dict[str, str] = {
    "JAX": "JAC",
    "WSH": "WAS",
    "LA": "LAR",
}


def _index_odds() -> dict[str, TeamRef]:
    out: dict[str, TeamRef] = {}
    for ref in TEAMS.values():
        for name in (ref.fd, ref.espn_abbr, *ref.odds):
            key = name.casefold()
            if key in out and out[key].fd != ref.fd:
                raise RuntimeError(f"duplicate odds name {name!r}")
            out[key] = ref
    for alias, fd in ALIASES.items():
        out[alias.casefold()] = TEAMS[fd]
    return out


BY_ODDS = _index_odds()


class UnmappedTeam(Exception):
    """FanDuel abbrev or provider name is not in TEAMS."""


def require_fd(abbrev: str) -> TeamRef:
    key = (abbrev or "").strip().upper()
    key = ALIASES.get(key, key)
    ref = TEAMS.get(key)
    if ref is None:
        raise UnmappedTeam(
            f"unmapped FanDuel team {key!r}; add it to nfl/teams.py "
            f"(Odds API name + ESPN id)"
        )
    return ref


def lookup_odds(name: str) -> TeamRef | None:
    return BY_ODDS.get((name or "").strip().casefold())


def lookup_espn_id(espn_id: str) -> TeamRef | None:
    want = str(espn_id or "").strip()
    for ref in TEAMS.values():
        if ref.espn_id == want:
            return ref
    return None


def require_mapped(abbrevs: set[str]) -> dict[str, TeamRef]:
    canon = {ALIASES.get(a.strip().upper(), a.strip().upper()) for a in abbrevs}
    missing = sorted(a for a in canon if a not in TEAMS)
    if missing:
        raise UnmappedTeam(
            "unmapped FanDuel team(s): "
            + ", ".join(missing)
            + " — add each to nfl/teams.py (JAC↔JAX, WAS↔WSH)"
        )
    return {a: TEAMS[a] for a in sorted(canon)}
