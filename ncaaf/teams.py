"""FanDuel NCAAF abbrev ↔ CFBD school name ↔ The Odds API name.

Unmapped FanDuel teams fail loud — do not guess (Texas vs Texas State vs
Texas A&M). Add a row here when a new abbrev appears on a slate.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TeamRef:
    fd: str
    cfbd: str
    odds: tuple[str, ...]


def _t(fd: str, cfbd: str, *odds: str) -> TeamRef:
    names = odds if odds else (cfbd,)
    return TeamRef(fd=fd, cfbd=cfbd, odds=names)


# Slates 133865 + 133970 + 134050 plus exact-name aliases used by CFBD / Odds.
TEAMS: dict[str, TeamRef] = {
    r.fd: r
    for r in (
        _t("ARIZ", "Arizona", "Arizona Wildcats"),
        _t("ARKST", "Arkansas State", "Arkansas State Red Wolves"),
        _t("ASU", "Arizona State", "Arizona State Sun Devils"),
        _t("AUB", "Auburn", "Auburn Tigers"),
        _t("BALL", "Ball State", "Ball State Cardinals"),
        _t("BAMA", "Alabama", "Alabama Crimson Tide"),
        _t("BAY", "Baylor", "Baylor Bears"),
        _t("BC", "Boston College", "Boston College Eagles"),
        _t("BOIS", "Boise State", "Boise State Broncos"),
        _t("BYU", "BYU", "BYU Cougars"),
        _t("CAL", "California", "California Golden Bears"),
        _t("CIN", "Cincinnati", "Cincinnati Bearcats"),
        _t("CLEM", "Clemson", "Clemson Tigers"),
        _t("DUKE", "Duke", "Duke Blue Devils"),
        _t("ECU", "East Carolina", "East Carolina Pirates"),
        _t("GT", "Georgia Tech", "Georgia Tech Yellow Jackets"),
        _t("HOU", "Houston", "Houston Cougars"),
        _t("ILL", "Illinois", "Illinois Fighting Illini"),
        _t("IND", "Indiana", "Indiana Hoosiers"),
        _t("IOWA", "Iowa", "Iowa Hawkeyes"),
        _t("ISU", "Iowa State", "Iowa State Cyclones"),
        _t("LOU", "Louisville", "Louisville Cardinals"),
        _t("LSU", "LSU", "LSU Tigers"),
        _t("MAR", "Marshall", "Marshall Thundering Herd"),
        _t("MEM", "Memphis", "Memphis Tigers"),
        _t("MICH", "Michigan", "Michigan Wolverines"),
        _t("MINN", "Minnesota", "Minnesota Golden Gophers"),
        _t("MISS", "Ole Miss", "Ole Miss Rebels"),
        _t("MOST", "Missouri State", "Missouri State Bears"),
        _t("MSST", "Mississippi State", "Mississippi State Bulldogs"),
        _t("ND", "Notre Dame", "Notre Dame Fighting Irish"),
        _t("NTEX", "North Texas", "North Texas Mean Green"),
        _t("OKST", "Oklahoma State", "Oklahoma State Cowboys"),
        _t("ORE", "Oregon", "Oregon Ducks"),
        _t("ORST", "Oregon State", "Oregon State Beavers"),
        _t("OSU", "Ohio State", "Ohio State Buckeyes"),
        _t("OU", "Oklahoma", "Oklahoma Sooners"),
        _t("PITT", "Pittsburgh", "Pittsburgh Panthers"),
        _t("PSU", "Penn State", "Penn State Nittany Lions"),
        _t("PUR", "Purdue", "Purdue Boilermakers"),
        _t("SYR", "Syracuse", "Syracuse Orange"),
        _t("TENN", "Tennessee", "Tennessee Volunteers"),
        _t("TEX", "Texas", "Texas Longhorns"),
        _t("TTU", "Texas Tech", "Texas Tech Red Raiders"),
        _t("TULN", "Tulane", "Tulane Green Wave"),
        _t("TXAM", "Texas A&M", "Texas A&M Aggies"),
        _t("TXST", "Texas State", "Texas State Bobcats"),
        _t("UCF", "UCF", "UCF Knights"),
        _t("UK", "Kentucky", "Kentucky Wildcats"),
        _t("WAKE", "Wake Forest", "Wake Forest Demon Deacons"),
        _t("WASH", "Washington", "Washington Huskies"),
        _t("WIS", "Wisconsin", "Wisconsin Badgers"),
        _t("WMU", "Western Michigan", "Western Michigan Broncos"),
        _t("WSU", "Washington State", "Washington State Cougars"),
    )
}


def _index_cfbd() -> dict[str, TeamRef]:
    out: dict[str, TeamRef] = {}
    for ref in TEAMS.values():
        key = ref.cfbd.casefold()
        if key in out and out[key].fd != ref.fd:
            raise RuntimeError(f"duplicate CFBD name {ref.cfbd!r}")
        out[key] = ref
    return out


def _index_odds() -> dict[str, TeamRef]:
    out: dict[str, TeamRef] = {}
    for ref in TEAMS.values():
        for name in (ref.cfbd, *ref.odds):
            key = name.casefold()
            if key in out and out[key].fd != ref.fd:
                raise RuntimeError(f"duplicate odds name {name!r}")
            out[key] = ref
    return out


BY_CFBD = _index_cfbd()
BY_ODDS = _index_odds()


class UnmappedTeam(Exception):
    """FanDuel abbrev or provider name is not in TEAMS."""


def require_fd(abbrev: str) -> TeamRef:
    key = (abbrev or "").strip().upper()
    ref = TEAMS.get(key)
    if ref is None:
        raise UnmappedTeam(
            f"unmapped FanDuel team {key!r}; add it to ncaaf/teams.py "
            f"(CFBD school name + Odds API name)"
        )
    return ref


def lookup_cfbd(name: str) -> TeamRef | None:
    return BY_CFBD.get((name or "").strip().casefold())


def lookup_odds(name: str) -> TeamRef | None:
    return BY_ODDS.get((name or "").strip().casefold())


def require_mapped(abbrevs: set[str]) -> dict[str, TeamRef]:
    missing = sorted(a for a in abbrevs if a not in TEAMS)
    if missing:
        raise UnmappedTeam(
            "unmapped FanDuel team(s): "
            + ", ".join(missing)
            + " — add each to ncaaf/teams.py (do not guess Texas vs Texas State vs Texas A&M)"
        )
    return {a: TEAMS[a] for a in sorted(abbrevs)}
