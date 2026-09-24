"""Gangstash player-props client (BettingPros consensus via Data Aggregator).

GET https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/props
Header: x-api-key from GANGSTASH_API_KEY. Never log the key or put it in the URL.

Unfiltered board is cached at nfl/data/gangstash-props/YYYY-MM-DD/props.json.
A same-day cache is used without the network. If live fetch fails (or the key
is unset) and an older cache exists, that cache is used and marked stale.
No cache and no key is an error. truncated=true on an unfiltered board is an
error and is not cached.

Do not scrape sportsbooks. This module only calls the gangstash HTTP API.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date
from pathlib import Path

from nfl import env as envmod
from nfl.http import HttpError, http_json

ENDPOINT = "https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/props"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "gangstash-props"


class GangstashError(Exception):
    """Fatal gangstash props ingest."""


class GangstashKeyMissing(GangstashError):
    """No GANGSTASH_API_KEY and no usable cache."""

    gate = "PROPS_GANGSTASH_KEY"


class GangstashTruncated(GangstashError):
    """Unfiltered board was cut off. Do not score a partial dump."""


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


def _reject_truncated(payload: dict, *, filtered: bool) -> None:
    if payload.get("truncated") and not filtered:
        n = len(payload.get("data") or [])
        raise GangstashTruncated(
            "gangstash props truncated=true "
            f"({n} rows) with no player_name/prop filter; refusing a partial board"
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
) -> dict:
    params: dict[str, str] = {}
    if player_name:
        params["player_name"] = player_name
    if prop:
        params["prop"] = prop
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
) -> tuple[list[dict], dict]:
    """Return (rows, meta). Meta includes cache path, stale flag, truncated."""
    day = cache_day or date.today()
    filtered = bool((player_name or "").strip() or (prop or "").strip())
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
        payload = _live(player_name=player_name, prop=prop)
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
