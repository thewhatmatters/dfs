"""Authorized OurLads NCAA depth-chart ingest (HTTP Fetcher only).

Grant: ncaaf/docs/data/ourlads-authorization.md. Identified User-Agent, no stealth,
slate teams only, cache + sleep. Do not import/call DynamicFetcher or
StealthyFetcher.
"""

from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from ncaaf import env as envmod
from ncaaf.teams import UnmappedTeam

INDEX_URL = "https://www.ourlads.com/ncaa-football-depth-charts/"
ASPX_PATH = "depth-chart.aspx"
PRETTY_PATH = "/ncaa-football-depth-charts/depth-chart/{slug}/{id}"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "ourlads"
DEPTH_CSV = Path(__file__).resolve().parent / "data" / "depth.csv"
SLEEP_S = 1.5
SKILL_POS = frozenset({"QB", "RB", "WR", "TE"})

# FanDuel abbrev → OurLads `s=` slug. Ids come from the index (they move).
OURLADS_SLUG: dict[str, str] = {
    "ARIZ": "arizona",
    "ARKST": "arkansas-state",
    "ASU": "arizona-state",
    "AUB": "auburn",
    "BALL": "ball-state",
    "BAMA": "alabama",
    "BAY": "baylor",
    "BC": "boston-college",
    "BOIS": "boise-state",
    "BYU": "brigham-young",
    "CAL": "california",
    "CIN": "cincinnati",
    "CLEM": "clemson",
    "DUKE": "duke",
    "ECU": "east-carolina",
    "GT": "georgia-tech",
    "HOU": "houston",
    "ILL": "illinois",
    "IND": "indiana",
    "IOWA": "iowa",
    "ISU": "iowa-state",
    "LOU": "louisville",
    "LSU": "lsu",
    "MAR": "marshall",
    "MEM": "memphis",
    "MICH": "michigan",
    "MINN": "minnesota",
    "MISS": "ole-miss",
    "MOST": "missouri-state",
    "MSST": "mississippi-state",
    "ND": "notre-dame",
    "NTEX": "north-texas",
    "OKST": "oklahoma-state",
    "ORE": "oregon",
    "ORST": "oregon-state",
    "OSU": "ohio-state",
    "OU": "oklahoma",
    "PITT": "pittsburgh",
    "PSU": "penn-state",
    "PUR": "purdue",
    "SYR": "syracuse",
    "TENN": "tennessee",
    "TEX": "texas",
    "TTU": "texas-tech",
    "TULN": "tulane",
    "TXAM": "texas-am",
    "TXST": "texas-state",
    "UCF": "central-florida",
    "UK": "kentucky",
    "WAKE": "wake-forest",
    "WASH": "washington",
    "WIS": "wisconsin",
    "WMU": "western-michigan",
    "WSU": "washington-state",
}

_ASPX_RE = re.compile(
    r"depth-chart\.aspx\?s=([a-z0-9\-]+)&id=(\d+)", re.I
)
_PRETTY_RE = re.compile(
    r"/depth-chart/([a-z0-9\-]+)/(\d+)/?", re.I
)
_CLASS_TAIL = re.compile(
    r"(?:\s|/)+(?:RS|R-S|FR|SO|JR|SR|GR|TR|RFR|RS-FR|REDSHIRT)+\s*$",
    re.I,
)


class DepthError(Exception):
    """Fatal OurLads ingest."""


def contact_email() -> str:
    return envmod.get("OURLADS_CONTACT_EMAIL") or "tucker.maus@ourlads.com"


def user_agent() -> str:
    return (
        "dfs-ncaaf/0.1 (personal DFS research; authorized OurLads NCAA depth; "
        f"contact {contact_email()})"
    )


def _headers() -> dict[str, str]:
    email = contact_email()
    return {
        "User-Agent": user_agent(),
        "From": email,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _fetcher_get(url: str):
    """HTTP GET via Scrapling Fetcher (curl_cffi). No stealth impersonation."""
    try:
        from scrapling.fetchers import Fetcher
    except ImportError as e:
        raise DepthError(
            "scrapling Fetcher is not importable. Install:\n"
            "  python3 -m pip install 'scrapling[fetchers]>=0.4.15'\n"
            "(Fetcher only — do not use DynamicFetcher/StealthyFetcher)"
        ) from e
    page = Fetcher.get(
        url,
        headers=_headers(),
        stealthy_headers=False,
        impersonate=None,
        timeout=30,
        retries=2,
        retry_delay=2,
    )
    status = getattr(page, "status", None)
    if status != 200:
        raise DepthError(f"OurLads HTTP {status} for {url}")
    body = page.body
    if isinstance(body, bytes):
        html = body.decode("utf-8", errors="replace")
    else:
        html = body or ""
    if not html:
        raise DepthError(f"empty OurLads body for {url}")
    return html, getattr(page, "url", url) or url


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def fetch_html(url: str, cache_name: str, *, refresh: bool = False) -> tuple[str, str, bool]:
    """Return (html, source_url, from_cache). Sleeps only on a live request."""
    path = _cache_path(cache_name)
    if path.is_file() and not refresh:
        return path.read_text(encoding="utf-8", errors="replace"), url, True
    html, final_url = _fetcher_get(url)
    path.write_text(html, encoding="utf-8")
    time.sleep(SLEEP_S)
    return html, final_url, False


def parse_index_slugs(html: str) -> dict[str, str]:
    """slug → id from the NCAA index."""
    out: dict[str, str] = {}
    for slug, oid in _ASPX_RE.findall(html) + _PRETTY_RE.findall(html):
        out[slug.lower()] = oid
    if not out:
        raise DepthError("OurLads index has no depth-chart.aspx?s=&id= links")
    return out


def aspx_url(slug: str, oid: str) -> str:
    return urljoin(INDEX_URL, f"{ASPX_PATH}?s={slug}&id={oid}")


def pretty_url(slug: str, oid: str) -> str:
    return f"https://www.ourlads.com{PRETTY_PATH.format(slug=slug, id=oid)}"


def require_slugs(fd_teams: set[str]) -> dict[str, str]:
    missing = sorted(t for t in fd_teams if t not in OURLADS_SLUG)
    if missing:
        raise UnmappedTeam(
            "unmapped FanDuel→OurLads slug(s): "
            + ", ".join(missing)
            + " — add to ncaaf/ourlads.py OURLADS_SLUG"
        )
    return {t: OURLADS_SLUG[t] for t in sorted(fd_teams)}


def skill_pos(label: str) -> str | None:
    p = (label or "").strip().upper()
    if not p:
        return None
    if p == "QB" or p.startswith("QB-") or p.startswith("QB/"):
        return "QB"
    if p == "RB" or p.startswith("RB-") or p.startswith("RB/"):
        return "RB"
    if p.startswith("WR"):
        return "WR"
    if p.startswith("TE"):
        return "TE"
    return None


def strip_class_tokens(raw: str) -> str:
    s = (raw or "").replace("\xa0", " ").strip()
    prev = None
    while s != prev:
        prev = s
        s = _CLASS_TAIL.sub("", s).strip(" ,/")
    return s


def display_name(raw: str) -> str:
    """'Manning, Arch RS JR' → 'Arch Manning'; 'Bishop Jr., Jermaine FR' → 'Jermaine Bishop Jr.'"""
    s = strip_class_tokens(raw)
    if "," in s:
        last, first = s.split(",", 1)
        s = f"{first.strip()} {last.strip()}"
    return re.sub(r"\s+", " ", s).strip()


_GENERATIONAL = re.compile(r"\b(?:jr|sr|ii|iii|iv|v|vi)\s*$", re.I)


def norm_name(raw: str) -> str:
    s = display_name(raw)
    s = s.casefold().replace(".", "").replace("'", "").replace("’", "")
    s = s.replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def match_key(raw: str) -> str:
    """norm_name with Jr/Sr/II/III/IV/V stripped for FanDuel join."""
    return _GENERATIONAL.sub("", norm_name(raw)).strip()


class _DepthTableParser(HTMLParser):
    """First depth table: Pos / No. / Player 1 / No / Player 2 / No / Player 3 …"""

    def __init__(self) -> None:
        super().__init__()
        self._in_table = False
        self._seen_header = False
        self._in_cell = False
        self._cell_bits: list[str] = []
        self._row: list[str] = []
        self.rows: list[list[str]] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table" and not self.rows and not self._in_table:
            self._in_table = True
            return
        if not self._in_table:
            return
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._in_cell = True
            self._cell_bits = []
        elif tag == "br" and self._in_cell:
            self._cell_bits.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if not self._in_table:
            return
        if tag in {"td", "th"} and self._in_cell:
            text = re.sub(r"\s+", " ", "".join(self._cell_bits)).strip()
            self._row.append(text)
            self._in_cell = False
        elif tag == "tr" and self._row:
            joined = " ".join(self._row).lower()
            if "player 1" in joined and "pos" in joined:
                self._seen_header = True
            elif self._seen_header:
                self.rows.append(self._row)
            self._row = []
        elif tag == "table":
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_table and self._in_cell and not self._skip_depth:
            self._cell_bits.append(data)


def parse_offense_depth(html: str) -> list[tuple[str, int, str]]:
    """Return (pos, rank, display_name) for QB/RB/WR/TE ranks 1–3.

    WR-X/WR-Z/WR-H Player 1 are all WR rank 1 (starters at each alignment).
    Same player on two rows keeps the best (lowest) rank later at write time.
    """
    if "Player 1" not in html:
        raise DepthError(
            "depth table missing from raw HTML (Player 1 header). "
            "Grant says HTTP first; DynamicFetcher is not auto-enabled."
        )
    parser = _DepthTableParser()
    parser.feed(html)
    if not parser.rows:
        raise DepthError("parsed zero depth-chart rows")
    out: list[tuple[str, int, str]] = []
    for cells in parser.rows:
        if not cells:
            continue
        pos = skill_pos(cells[0])
        if pos is None:
            continue
        # Player 1,2,3 sit at odd indices after Pos, No. → 2, 4, 6
        for rank, idx in ((1, 2), (2, 4), (3, 6)):
            if idx >= len(cells):
                continue
            name = display_name(cells[idx])
            if not name:
                continue
            out.append((pos, rank, name))
    if not out:
        raise DepthError("no QB/RB/WR/TE names in depth table")
    return out


@dataclass(frozen=True)
class DepthRow:
    team: str
    pos: str
    rank: int
    name: str
    source_url: str
    fetched_at: str


def _dedupe(rows: list[DepthRow]) -> list[DepthRow]:
    """Keep the best rank for (team, pos, norm_name)."""
    best: dict[tuple[str, str, str], DepthRow] = {}
    for r in rows:
        key = (r.team, r.pos, norm_name(r.name))
        prev = best.get(key)
        if prev is None or r.rank < prev.rank:
            best[key] = r
    return sorted(best.values(), key=lambda r: (r.team, r.pos, r.rank, r.name))


def ingest_slate_depth(
    fd_teams: set[str],
    *,
    refresh: bool = False,
    out_csv: Path | None = None,
) -> list[DepthRow]:
    """Fetch (or cache) slate-team charts and write depth.csv."""
    slugs = require_slugs(fd_teams)
    index_html, _, _ = fetch_html(INDEX_URL, "index.html", refresh=refresh)
    id_by_slug = parse_index_slugs(index_html)
    missing = [f"{fd}:{slug}" for fd, slug in slugs.items() if slug not in id_by_slug]
    if missing:
        raise DepthError(
            "OurLads index missing slug(s): " + ", ".join(missing)
        )

    fetched_at = datetime.now(timezone.utc).isoformat()
    rows: list[DepthRow] = []
    for fd, slug in slugs.items():
        oid = id_by_slug[slug]
        url = aspx_url(slug, oid)
        cache_name = f"{slug}-{oid}.html"
        html, final_url, _cached = fetch_html(url, cache_name, refresh=refresh)
        src = pretty_url(slug, oid)
        if final_url and "depth-chart" in final_url:
            src = final_url.split("?")[0]
        try:
            entries = parse_offense_depth(html)
        except DepthError as e:
            raise DepthError(f"{fd} ({slug}): {e}") from e
        for pos, rank, name in entries:
            rows.append(
                DepthRow(
                    team=fd,
                    pos=pos,
                    rank=rank,
                    name=name,
                    source_url=src,
                    fetched_at=fetched_at,
                )
            )
    rows = _dedupe(rows)
    dest = out_csv or DEPTH_CSV
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["team", "pos", "rank", "name", "source_url", "fetched_at"],
        )
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "team": r.team,
                    "pos": r.pos,
                    "rank": r.rank,
                    "name": r.name,
                    "source_url": r.source_url,
                    "fetched_at": r.fetched_at,
                }
            )
    return rows


def load_depth_csv(path: Path | None = None) -> list[DepthRow]:
    p = path or DEPTH_CSV
    if not p.is_file():
        return []
    out: list[DepthRow] = []
    with p.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append(
                DepthRow(
                    team=(row.get("team") or "").strip().upper(),
                    pos=(row.get("pos") or "").strip().upper(),
                    rank=int(row["rank"]),
                    name=(row.get("name") or "").strip(),
                    source_url=(row.get("source_url") or "").strip(),
                    fetched_at=(row.get("fetched_at") or "").strip(),
                )
            )
    return out
