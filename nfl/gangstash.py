"""Gangstash HTTP client (BettingPros consensus via Data Aggregator).

Player props:
  GET https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/props
  Header: x-api-key from GANGSTASH_API_KEY. Never log the key or put it in the URL.
  Cache: nfl/data/gangstash-props/YYYY-MM-DD/props.json

Other datasets (targets, game lines, depth, team stats):
  GET https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/data?dataset=<name>
  Same header and `{data, truncated}` body. Path override: GANGSTASH_DATA_PATH.
  Cache: nfl/data/gangstash-data/YYYY-MM-DD/<dataset>/<query>.json

A same-day cache is used without the network. If live fetch fails (or the key
is unset) and an older cache exists, that cache is used and marked stale.
No cache and no key is an error. truncated=true is an error and is not cached.

Do not scrape sportsbooks. This module only calls the gangstash HTTP API.
Never send a Supabase service-role key.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import date
from pathlib import Path

from nfl import env as envmod
from nfl.http import HttpError, http_json

ENDPOINT = "https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/props"
FUNCTIONS_BASE = "https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "gangstash-props"
DATA_CACHE_DIR = Path(__file__).resolve().parent / "data" / "gangstash-data"
# /data pages are 1,000 rows. The server caps a result at 20,000.
PAGE_SIZE = 1000
MAX_ROWS = 20_000

# Logical dataset → env override. Defaults are the live `dataset=` values.
DATASET_ENV = {
    "targets": "GANGSTASH_TARGETS_DATASET",
    "game_lines": "GANGSTASH_GAME_LINES_DATASET",
    "depth_charts": "GANGSTASH_DEPTH_DATASET",
    "team_stats": "GANGSTASH_TEAM_STATS_DATASET",
    "team_stats_weekly": "GANGSTASH_TEAM_STATS_WEEKLY_DATASET",
    "snaps": "GANGSTASH_SNAPS_DATASET",
    "player_stats_weekly": "GANGSTASH_PLAYER_STATS_WEEKLY_DATASET",
    "closing_lines": "GANGSTASH_CLOSING_LINES_DATASET",
    "injuries": "GANGSTASH_INJURIES_DATASET",
}


class GangstashError(Exception):
    """Fatal gangstash props ingest."""


class GangstashKeyMissing(GangstashError):
    """No GANGSTASH_API_KEY and no usable cache."""

    gate = "PROPS_GANGSTASH_KEY"


class GangstashTruncated(GangstashError):
    """Board was cut off. Do not score a partial dump."""


class GangstashDataError(GangstashError):
    """Fatal /data dataset ingest (HTTP, shape, or field mapping)."""


class GangstashDataKeyMissing(GangstashDataError):
    """No GANGSTASH_API_KEY and no usable /data cache."""


# Old CLI sources. Printed when the key is missing and no cache exists.
FALLBACK_FLAGS = (
    "--lines-source=oddsapi --targets-source=lineups "
    "--snaps-source=lineups --depth-source=ourlads"
)


def missing_key_message(detail: str) -> str:
    """One line: what failed, then the flags that restore the previous sources."""
    text = (detail or "GANGSTASH_API_KEY is not set and no cache exists").strip()
    return f"{text}; pass {FALLBACK_FLAGS}"


def _key() -> str:
    k = envmod.get("GANGSTASH_API_KEY")
    if not k:
        raise GangstashKeyMissing("GANGSTASH_API_KEY is not set")
    return k


def _cache_file(day: date) -> Path:
    return CACHE_DIR / day.isoformat() / "props.json"


def _read_payload(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise GangstashError(f"gangstash cache {path}: {e}") from e
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise GangstashError(f"gangstash cache {path} is not {{data: [...]}}")
    return payload


def _newest_cache() -> tuple[dict, Path] | None:
    if not CACHE_DIR.is_dir():
        return None
    days = sorted(
        (p for p in CACHE_DIR.iterdir() if p.is_dir() and (p / "props.json").is_file()),
        key=lambda p: p.name,
        reverse=True,
    )
    for day_dir in days:
        path = day_dir / "props.json"
        if path.stat().st_size <= 2:
            continue
        return _read_payload(path), path
    return None


def _reject_truncated(payload: dict, *, filtered: bool, label: str = "props") -> None:
    if payload.get("truncated") and not filtered:
        n = len(payload.get("data") or [])
        if label == "props":
            raise GangstashTruncated(
                "gangstash props truncated=true "
                f"({n} rows) with no player_name/prop filter; refusing a partial board"
            )
        raise GangstashTruncated(
            f"gangstash {label} truncated=true ({n} rows); refusing a partial board"
        )


def _write_cache(day: date, payload: dict) -> Path:
    path = _cache_file(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _live(
    *,
    player_name: str | None,
    prop: str | None,
    season: int | None = None,
    week: int | None = None,
) -> dict:
    params: dict[str, str] = {}
    if player_name:
        params["player_name"] = player_name
    if prop:
        params["prop"] = prop
    if season is not None:
        params["season"] = str(int(season))
    if week is not None:
        params["week"] = str(int(week))
    url = ENDPOINT
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        payload, _hdrs = http_json(url, headers={"x-api-key": _key()})
    except GangstashKeyMissing:
        raise
    except HttpError as e:
        raise GangstashError(f"gangstash props {e}") from e
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise GangstashError("gangstash props response is not {data: [...]}")
    return payload


def fetch_props(
    *,
    refresh: bool = False,
    cache_day: date | None = None,
    player_name: str | None = None,
    prop: str | None = None,
    season: int | None = None,
    week: int | None = None,
) -> tuple[list[dict], dict]:
    """Return (rows, meta). Meta includes cache path, stale flag, truncated.

    ``season`` and ``week`` request one week's snapshots. That call does not
    read the unfiltered day cache (a later week's board must not fill an
    earlier week).
    """
    day = cache_day or date.today()
    filtered = bool(
        (player_name or "").strip()
        or (prop or "").strip()
        or season is not None
        or week is not None
    )
    today_path = _cache_file(day)

    if not refresh and not filtered and today_path.is_file() and today_path.stat().st_size > 2:
        payload = _read_payload(today_path)
        _reject_truncated(payload, filtered=False)
        return list(payload["data"]), {
            "cache": str(today_path),
            "cache_stale": False,
            "truncated": bool(payload.get("truncated")),
            "live": False,
        }

    try:
        payload = _live(
            player_name=player_name,
            prop=prop,
            season=season,
            week=week,
        )
    except GangstashKeyMissing:
        if refresh or filtered:
            raise
        cached = _newest_cache()
        if cached is None:
            raise GangstashKeyMissing(
                "GANGSTASH_API_KEY is not set and no gangstash props cache exists"
            ) from None
        payload, path = cached
        _reject_truncated(payload, filtered=False)
        return list(payload["data"]), {
            "cache": str(path),
            "cache_stale": path != today_path,
            "truncated": bool(payload.get("truncated")),
            "live": False,
        }
    except GangstashError:
        if refresh or filtered:
            raise
        cached = _newest_cache()
        if cached is None:
            raise
        payload, path = cached
        _reject_truncated(payload, filtered=False)
        return list(payload["data"]), {
            "cache": str(path),
            "cache_stale": True,
            "truncated": bool(payload.get("truncated")),
            "live": False,
        }

    _reject_truncated(payload, filtered=filtered)
    meta = {
        "cache": None,
        "cache_stale": False,
        "truncated": bool(payload.get("truncated")),
        "live": True,
    }
    if not filtered:
        meta["cache"] = str(_write_cache(day, payload))
    return list(payload["data"]), meta


def dataset_id(logical: str) -> str:
    """Resolved `dataset=` value. Env override, else the logical name."""
    env_name = DATASET_ENV.get(logical)
    if env_name:
        override = envmod.get(env_name)
        if override:
            return override.strip()
    return logical


def data_endpoint() -> str:
    """Edge function URL. GANGSTASH_DATA_PATH replaces the `data` segment."""
    raw = (envmod.get("GANGSTASH_DATA_PATH") or "data").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")
    return FUNCTIONS_BASE + "/" + raw.lstrip("/")


def _safe_dataset(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    if not cleaned or cleaned in {".", ".."}:
        raise GangstashDataError(f"bad gangstash dataset name {name!r}")
    return cleaned


def _query_slug(params: dict[str, str]) -> str:
    if not params:
        return "_all"
    parts = [f"{k}-{v}" for k, v in sorted(params.items())]
    raw = "__".join(parts)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    return slug[:160] or "_all"


def dataset_cache_file(
    cache_root: Path,
    day: date,
    dataset: str,
    params: dict[str, str],
) -> Path:
    return cache_root / day.isoformat() / _safe_dataset(dataset) / f"{_query_slug(params)}.json"


def _data_key() -> str:
    k = envmod.get("GANGSTASH_API_KEY")
    if not k:
        raise GangstashDataKeyMissing("GANGSTASH_API_KEY is not set")
    return k


def _newest_dataset_cache(
    root: Path,
    dataset: str,
    params: dict[str, str],
) -> tuple[dict, Path] | None:
    if not root.is_dir():
        return None
    name = _safe_dataset(dataset)
    slug = _query_slug(params)
    days = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    for day_dir in days:
        path = day_dir / name / f"{slug}.json"
        if path.is_file() and path.stat().st_size > 2:
            return _read_payload(path), path
    return None


def _page_url(dataset: str, params: dict[str, str], offset: int) -> str:
    query = {"dataset": dataset, **params}
    if offset:
        query["offset"] = str(offset)
    return data_endpoint() + "?" + urllib.parse.urlencode(query, safe=",")


def _live_dataset_page(dataset: str, params: dict[str, str], offset: int) -> dict:
    """One page. First page omits offset. Later pages send offset=1000, 2000, …"""
    url = _page_url(dataset, params, offset)
    try:
        payload, _hdrs = http_json(url, headers={"x-api-key": _data_key()})
    except GangstashDataKeyMissing:
        raise
    except HttpError as e:
        raise GangstashDataError(f"gangstash {dataset} {e}") from e
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise GangstashDataError(f"gangstash {dataset} response is not {{data: [...]}}")
    return payload


def _live_dataset(dataset: str, params: dict[str, str]) -> dict:
    """Follow truncated pages of PAGE_SIZE up to MAX_ROWS. Cache the full board."""
    rows: list[dict] = []
    offset = 0
    max_pages = max(1, MAX_ROWS // PAGE_SIZE)
    for page in range(max_pages):
        payload = _live_dataset_page(dataset, params, offset)
        chunk = list(payload["data"])
        rows.extend(chunk)
        if not payload.get("truncated"):
            return {"data": rows, "truncated": False}
        if page + 1 >= max_pages or len(rows) >= MAX_ROWS:
            raise GangstashTruncated(
                f"gangstash {dataset} truncated=true after {len(rows)} rows "
                f"(page size {PAGE_SIZE}, cap {MAX_ROWS}); refusing a partial board"
            )
        offset += PAGE_SIZE
    raise GangstashTruncated(
        f"gangstash {dataset} truncated=true after {len(rows)} rows"
    )


def fetch_dataset(
    dataset: str,
    params: dict[str, str] | None = None,
    *,
    refresh: bool = False,
    cache_day: date | None = None,
    cache_root: Path | None = None,
) -> tuple[list[dict], dict]:
    """GET /data?dataset=… . Return (rows, meta). truncated=true is not cached.

    `params` is the query besides `dataset` (season, week, date, …).
    Same-day cache skips the network. A failed live call falls back to an
    older file for the same dataset and query unless `refresh` is set.
    """
    day = cache_day or date.today()
    root = cache_root or DATA_CACHE_DIR
    query = {k: str(v) for k, v in (params or {}).items() if v is not None and str(v) != ""}
    name = _safe_dataset(dataset)
    today_path = dataset_cache_file(root, day, name, query)

    if not refresh and today_path.is_file() and today_path.stat().st_size > 2:
        payload = _read_payload(today_path)
        _reject_truncated(payload, filtered=False, label=name)
        return list(payload["data"]), {
            "cache": str(today_path),
            "cache_stale": False,
            "truncated": bool(payload.get("truncated")),
            "live": False,
            "dataset": name,
        }

    try:
        payload = _live_dataset(name, query)
    except GangstashDataKeyMissing:
        if refresh:
            raise
        cached = _newest_dataset_cache(root, name, query)
        if cached is None:
            raise GangstashDataKeyMissing(
                f"GANGSTASH_API_KEY is not set and no gangstash {name} cache exists"
            ) from None
        payload, path = cached
        _reject_truncated(payload, filtered=False, label=name)
        return list(payload["data"]), {
            "cache": str(path),
            "cache_stale": path != today_path,
            "truncated": bool(payload.get("truncated")),
            "live": False,
            "dataset": name,
        }
    except GangstashDataError:
        if refresh:
            raise
        cached = _newest_dataset_cache(root, name, query)
        if cached is None:
            raise
        payload, path = cached
        _reject_truncated(payload, filtered=False, label=name)
        return list(payload["data"]), {
            "cache": str(path),
            "cache_stale": True,
            "truncated": bool(payload.get("truncated")),
            "live": False,
            "dataset": name,
        }

    _reject_truncated(payload, filtered=False, label=name)
    today_path.parent.mkdir(parents=True, exist_ok=True)
    today_path.write_text(json.dumps(payload), encoding="utf-8")
    return list(payload["data"]), {
        "cache": str(today_path),
        "cache_stale": False,
        "truncated": bool(payload.get("truncated")),
        "live": True,
        "dataset": name,
    }
