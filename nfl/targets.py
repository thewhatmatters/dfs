"""Lineups.com WR/TE target refresh → nfl/data/targets.csv.

Public pages only (no login). Low-rate identified User-Agent; cache under
nfl/data/lineups-targets/. Lineups ToS disallow automated aggregation — treat
as a known risk; prefer cache hits when not forcing --refresh.

Some egress IPs get Cloudflare 403 on live urllib. Prefer cached `.json`
(SSR payload) when present; seed those from a network that can fetch.

Usage:
  python3 -m nfl.targets --refresh
  python3 -m nfl.targets --cache-only
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from nfl.http import HttpAuthError, HttpError, http_text
from nfl.teams import UnmappedTeam, lookup_odds

WR_URL = "https://www.lineups.com/nfl/targets/wide-receiver/"
TE_URL = "https://www.lineups.com/nfl/targets/tight-end/"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "lineups-targets"
DEFAULT_OUT = Path(__file__).resolve().parent / "data" / "targets.csv"
SOURCE = "lineups"
SSR_CLASS = "sc-sports-nfl-metrics"
CSV_FIELDS = (
    "player",
    "team",
    "position",
    "week",
    "targets",
    "target_share",
    "targets_avg",
    "targets_total",
    "source",
    "asof",
)

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


class TargetsError(Exception):
    """Fatal Lineups targets ingest."""

    def __init__(self, choke: str, message: str) -> None:
        self.choke = choke
        super().__init__(message)


@dataclass(frozen=True)
class TargetWeekRow:
    player: str
    team: str
    position: str
    week: int
    targets: int
    target_share: float
    targets_avg: float
    targets_total: int
    source: str
    asof: str


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


def extract_ssr_payload(html: str) -> dict[str, Any]:
    """Parse embedded Lineups SSR JSON for metric=targets."""
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
            raise TargetsError(
                "TARGETS_LINEUPS",
                f"missing SSR JSON script ({SSR_CLASS})",
            )
        raw = m.group(1)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TargetsError("TARGETS_LINEUPS", f"SSR JSON decode: {e}") from e
    if not isinstance(payload, dict):
        raise TargetsError("TARGETS_LINEUPS", "SSR payload is not an object")
    if payload.get("metric") != "targets":
        raise TargetsError(
            "TARGETS_LINEUPS",
            f"unexpected metric {payload.get('metric')!r}",
        )
    return payload


def _day_dir(asof: date | None = None) -> Path:
    d = CACHE_DIR / (asof or date.today()).isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_position_payload(position: str, url: str, *, refresh: bool) -> dict[str, Any]:
    """Return SSR targets JSON for WR/TE.

    Prefer cached `.json`. Else parse cached `.html`. Else live GET (writes
    both). Cloudflare 403 falls back to existing `.json` when present.
    """
    day = _day_dir()
    json_path = day / f"{position.lower()}.json"
    html_path = day / f"{position.lower()}.html"

    if not refresh and json_path.is_file() and json_path.stat().st_size > 50:
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise TargetsError("TARGETS_LINEUPS", f"{position} cache JSON: {e}") from e
        if payload.get("metric") != "targets":
            raise TargetsError("TARGETS_LINEUPS", f"{position} cache bad metric")
        return payload

    if not refresh and html_path.is_file() and html_path.stat().st_size > 1000:
        payload = extract_ssr_payload(html_path.read_text(encoding="utf-8"))
        json_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    try:
        body, _hdrs = http_text(url, headers=LINEUPS_HEADERS)
    except (HttpAuthError, HttpError) as e:
        if json_path.is_file() and json_path.stat().st_size > 50:
            print(
                f"targets: {position} live fetch blocked ({e}); "
                f"using cache {json_path}",
                file=sys.stderr,
            )
            return json.loads(json_path.read_text(encoding="utf-8"))
        raise TargetsError(
            "TARGETS_LINEUPS",
            f"{position} fetch failed ({e}). Seed {json_path} with Lineups "
            "SSR JSON (metric=targets) or retry from a non-blocked network.",
        ) from e
    if len(body) < 1000:
        raise TargetsError(
            "TARGETS_LINEUPS",
            f"{position} response too small ({len(body)} bytes)",
        )
    html_path.write_text(body, encoding="utf-8")
    payload = extract_ssr_payload(body)
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def rows_from_payload(
    payload: dict[str, Any],
    *,
    asof: str,
    expect_pos: str | None = None,
) -> list[TargetWeekRow]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TargetsError("TARGETS_LINEUPS", "SSR data missing")
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise TargetsError("TARGETS_LINEUPS", "SSR rows empty")

    out: list[TargetWeekRow] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        team_name = str(item.get("team") or "").strip()
        pos = str(item.get("position") or expect_pos or "").strip().upper()
        if not name or not team_name or not pos:
            continue
        if expect_pos and pos != expect_pos:
            continue
        ref = lookup_odds(team_name)
        if ref is None:
            raise TargetsError(
                "TARGETS_JOIN",
                f"unmapped Lineups team {team_name!r} for {name!r}; "
                "add Odds full name to nfl/teams.py",
            )
        weeks = item.get("weeks") or []
        weeks_pct = item.get("weeksPct") or []
        if not isinstance(weeks, list):
            raise TargetsError("TARGETS_LINEUPS", f"bad weeks for {name!r}")
        if not isinstance(weeks_pct, list):
            weeks_pct = []
        try:
            total = int(item.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        try:
            average = float(item.get("average") or 0)
        except (TypeError, ValueError):
            average = 0.0

        for i, val in enumerate(weeks):
            if val is None:
                continue
            try:
                targets = int(val)
            except (TypeError, ValueError):
                continue
            share_raw = weeks_pct[i] if i < len(weeks_pct) else None
            if share_raw is None:
                share = 0.0
            else:
                try:
                    share = float(share_raw) / 100.0
                except (TypeError, ValueError):
                    share = 0.0
            out.append(
                TargetWeekRow(
                    player=name,
                    team=ref.fd,
                    position=pos,
                    week=i + 1,
                    targets=targets,
                    target_share=share,
                    targets_avg=average,
                    targets_total=total,
                    source=SOURCE,
                    asof=asof,
                )
            )
    return out


def write_targets_csv(rows: list[TargetWeekRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda r: (r.team, r.position, r.player, r.week))
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in ordered:
            share_s = (
                f"{r.target_share:.4f}".rstrip("0").rstrip(".")
                if r.target_share
                else "0"
            )
            w.writerow(
                {
                    "player": r.player,
                    "team": r.team,
                    "position": r.position,
                    "week": r.week,
                    "targets": r.targets,
                    "target_share": share_s,
                    "targets_avg": r.targets_avg,
                    "targets_total": r.targets_total,
                    "source": r.source,
                    "asof": r.asof,
                }
            )


def refresh_targets(
    *,
    refresh: bool = True,
    out: Path = DEFAULT_OUT,
) -> dict[str, Any]:
    asof = date.today().isoformat()
    wr_payload = fetch_position_payload("WR", WR_URL, refresh=refresh)
    te_payload = fetch_position_payload("TE", TE_URL, refresh=refresh)
    wr_rows = rows_from_payload(wr_payload, asof=asof, expect_pos="WR")
    te_rows = rows_from_payload(te_payload, asof=asof, expect_pos="TE")
    rows = wr_rows + te_rows
    if not rows:
        raise TargetsError("TARGETS_LINEUPS", "no player-week rows after parse")
    write_targets_csv(rows, out)
    return {
        "out": str(out),
        "asof": asof,
        "rows": len(rows),
        "wr_players": len({r.player for r in wr_rows}),
        "te_players": len({r.player for r in te_rows}),
        "wr_rows": len(wr_rows),
        "te_rows": len(te_rows),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="re-fetch Lineups WR/TE pages (writes cache + CSV)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"output CSV (default: {DEFAULT_OUT})",
    )
    ap.add_argument(
        "--cache-only",
        action="store_true",
        help="rebuild CSV from today's cache without HTTP",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.refresh and not args.cache_only:
        print(
            "choke TARGETS_LINEUPS: pass --refresh (or --cache-only)",
            file=sys.stderr,
        )
        return 2
    try:
        stats = refresh_targets(
            refresh=bool(args.refresh) and not args.cache_only,
            out=args.out,
        )
    except TargetsError as e:
        print(f"choke {e.choke}: {e}", file=sys.stderr)
        return 1
    except UnmappedTeam as e:
        print(f"choke TARGETS_JOIN: {e}", file=sys.stderr)
        return 1
    print(
        f"targets: {stats['rows']} rows "
        f"({stats['wr_players']} WR / {stats['te_players']} TE players) "
        f"→ {stats['out']} (asof {stats['asof']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
