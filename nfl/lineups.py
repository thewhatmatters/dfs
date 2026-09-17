"""Shared Lineups.com NFL metrics SSR helpers (targets + snaps).

Public pages only. Identified UA; cache JSON first. Cloudflare 403 on some
egress IPs — prefer a cached `.json` SSR payload. Grant:
`nfl/docs/data/lineups-authorization.md`. Do not scrape FanDuel.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from nfl.http import HttpAuthError, HttpError, http_text

SSR_CLASS = "sc-sports-nfl-metrics"

# Browser-like UA — bare dfs UA / some residential IPs get Cloudflare 403.
LINEUPS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Inventory only — not ingested this pass.
LINEUPS_PROPS_CANDIDATE = "https://www.lineups.com/nfl/player-prop-bets/"


class LineupsError(Exception):
    """Fatal Lineups metrics ingest."""

    def __init__(self, choke: str, message: str) -> None:
        self.choke = choke
        super().__init__(message)


class _SsrJsonExtractor(HTMLParser):
    """Pull text from <script type=application/json class=sc-sports-nfl-metrics>."""

    def __init__(self) -> None:
        super().__init__()
        self._capture = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        ad = {k: (v or "") for k, v in attrs}
        classes = ad.get("class", "").split()
        if ad.get("type", "").lower() == "application/json" and SSR_CLASS in classes:
            self._capture = True
            self.chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._capture = False

    def handle_data(self, data: str) -> None:
        if self._capture:
            self.chunks.append(data)


def extract_ssr_payload(
    html: str,
    *,
    metric: str,
    choke: str,
) -> dict[str, Any]:
    """Parse embedded Lineups SSR JSON and require `metric`."""
    parser = _SsrJsonExtractor()
    parser.feed(html)
    raw = "".join(parser.chunks).strip()
    if not raw:
        m = re.search(
            r'<script[^>]*class="[^"]*%s[^"]*"[^>]*>(\{.*?\})</script>'
            % re.escape(SSR_CLASS),
            html,
            re.S,
        )
        if not m:
            raise LineupsError(choke, f"missing SSR JSON script ({SSR_CLASS})")
        raw = m.group(1)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LineupsError(choke, f"SSR JSON decode: {e}") from e
    if not isinstance(payload, dict):
        raise LineupsError(choke, "SSR payload is not an object")
    if payload.get("metric") != metric:
        raise LineupsError(
            choke,
            f"unexpected metric {payload.get('metric')!r} (want {metric!r})",
        )
    return payload


def day_cache_dir(cache_dir: Path, asof: date | None = None) -> Path:
    d = cache_dir / (asof or date.today()).isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_metric_payload(
    position: str,
    url: str,
    *,
    cache_dir: Path,
    metric: str,
    choke: str,
    refresh: bool,
) -> dict[str, Any]:
    """Return SSR JSON for one Lineups metrics page.

    Prefer cached `.json`. Else parse cached `.html`. Else live GET (writes
    both). Cloudflare 403 falls back to existing `.json` when present.
    """
    day = day_cache_dir(cache_dir)
    json_path = day / f"{position.lower()}.json"
    html_path = day / f"{position.lower()}.html"

    if not refresh and json_path.is_file() and json_path.stat().st_size > 50:
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise LineupsError(choke, f"{position} cache JSON: {e}") from e
        if payload.get("metric") != metric:
            raise LineupsError(choke, f"{position} cache bad metric")
        return payload

    if not refresh and html_path.is_file() and html_path.stat().st_size > 1000:
        payload = extract_ssr_payload(
            html_path.read_text(encoding="utf-8"),
            metric=metric,
            choke=choke,
        )
        json_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    try:
        body, _hdrs = http_text(url, headers=LINEUPS_HEADERS)
    except (HttpAuthError, HttpError) as e:
        if json_path.is_file() and json_path.stat().st_size > 50:
            print(
                f"lineups: {position} live fetch blocked ({e}); "
                f"using cache {json_path}",
                file=sys.stderr,
            )
            return json.loads(json_path.read_text(encoding="utf-8"))
        raise LineupsError(
            choke,
            f"{position} fetch failed ({e}). Seed {json_path} with Lineups "
            f"SSR JSON (metric={metric}) or retry from a non-blocked network.",
        ) from e
    if len(body) < 1000:
        raise LineupsError(
            choke,
            f"{position} response too small ({len(body)} bytes)",
        )
    html_path.write_text(body, encoding="utf-8")
    payload = extract_ssr_payload(body, metric=metric, choke=choke)
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload
