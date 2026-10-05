"""Upsert sim game rows to ``public.nfl_game_projections``.

Player rows stay on ``POST /functions/v1/projections``. This table is
PostgREST only, with the service-role key, conflict target
``season,week,season_type,run_at,model,game_id``. ``model`` is ``sim``.
``game_id`` is ``nfl_games.id`` (uuid).

Scores are the sim's ``game_draws`` (away/home points per draw). Means and
percentiles use those raw points. Wins, covers, and totals use each draw
rounded to the nearest integer, so an integer consensus line can push.
``consensus_home_line`` is the FanDuel home spread (negative when home is
favored). Home covers when ``(home - away) + home_line > 0``.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Optional, Sequence

from nfl import env as envmod
from nfl.gangstash import REST_BASE
from nfl.gangstash_data import parse_game_line
from nfl.http import HttpError, http_json_post

GAME_PROJECTIONS_URL = REST_BASE + "/nfl_game_projections"
ON_CONFLICT = "season,week,season_type,run_at,model,game_id"
MODEL = "sim"

# Live columns except id and created_at (database defaults).
GAME_ROW_FIELDS = (
    "season",
    "week",
    "season_type",
    "game_id",
    "run_at",
    "model",
    "model_version",
    "n_draws",
    "home_team",
    "away_team",
    "home_mean",
    "home_p10",
    "home_p50",
    "home_p90",
    "away_mean",
    "away_p10",
    "away_p50",
    "away_p90",
    "home_wins",
    "away_wins",
    "ties",
    "home_win_prob",
    "away_win_prob",
    "consensus_home_line",
    "consensus_total",
    "home_cover_prob",
    "away_cover_prob",
    "push_cover_prob",
    "over_prob",
    "under_prob",
    "push_total_prob",
)

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
_PUSH_EPS = 1e-6


class GameProjectionsError(Exception):
    """Game-projection upsert failed."""


class GameProjectionsKeyMissing(GameProjectionsError):
    """No GANGSTASH_SERVICE_ROLE_KEY."""


def service_role_key() -> Optional[str]:
    """Supabase service-role key for the game-projection upsert only."""
    return envmod.get("GANGSTASH_SERVICE_ROLE_KEY")


def _uuid(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if _UUID.match(text):
        return text
    return None


def _uuid_from_row(row: dict) -> Optional[str]:
    """``nfl_games.id``. Prefer an explicit uuid, then ``game_id`` when it is one."""
    for key in ("nfl_game_id", "game_uuid"):
        found = _uuid(row.get(key))
        if found:
            return found
    found = _uuid(row.get("game_id"))
    if found:
        return found
    return _uuid(row.get("id"))


def _book(row: dict) -> str:
    return str(row.get("book") or row.get("sportsbook") or "").strip().casefold()


def _consensus_rows(rows: Sequence[dict]) -> list:
    """Rows whose book is consensus. A feed with no book column is already that board."""
    dicts = [row for row in rows if isinstance(row, dict)]
    if not any(_book(row) for row in dicts):
        return dicts
    return [row for row in dicts if _book(row) == "consensus"]


def _split_game(game: str) -> tuple:
    if "@" not in (game or ""):
        return None, None
    away, home = str(game).split("@", 1)
    away_fd = away.strip().upper()
    home_fd = home.strip().upper()
    if not away_fd or not home_fd:
        return None, None
    return away_fd, home_fd


def _percentile(sorted_xs: list, p: float) -> float:
    n = len(sorted_xs)
    if n == 1:
        return float(sorted_xs[0])
    idx = p * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    weight = idx - lo
    return sorted_xs[lo] * (1.0 - weight) + sorted_xs[hi] * weight


def _band(values: list) -> tuple:
    ordered = sorted(float(v) for v in values)
    return (
        _percentile(ordered, 0.10),
        _percentile(ordered, 0.50),
        _percentile(ordered, 0.90),
    )


def _round_score(value: float) -> int:
    """Nearest integer. Halfway cases go away from zero (scores are non-negative)."""
    return int(math.floor(float(value) + 0.5))


def _round(value: Optional[float], places: int) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), places)


def _rate(count: int, n: int) -> float:
    return round(float(count) / float(n), 6)


def consensus_index(line_rows: Optional[Sequence[dict]]) -> dict:
    """``(away, home) → {game_id, home_line, total}`` from book=consensus."""
    out: dict = {}
    for row in _consensus_rows(list(line_rows or [])):
        parsed = parse_game_line(row)
        if parsed is None:
            continue
        slot = out.setdefault((parsed.away_fd, parsed.home_fd), {})
        slot["home_line"] = float(parsed.spread)
        slot["total"] = float(parsed.total)
        uid = _uuid_from_row(row)
        if uid:
            slot["game_id"] = uid
    return out


def _entry_lines(entries: Optional[Sequence]) -> dict:
    """Home spread and total already on the published pool, plus a uuid game id."""
    out: dict = {}
    for entry in entries or []:
        player = getattr(entry, "player", None)
        if player is None:
            continue
        away, home = _split_game(getattr(player, "game", "") or "")
        if away is None:
            continue
        slot = out.setdefault((away, home), {})
        uid = _uuid(getattr(entry, "game_id", None))
        if uid and not slot.get("game_id"):
            slot["game_id"] = uid
        team = (getattr(player, "team", "") or "").upper()
        spread = getattr(player, "spread", None)
        total = getattr(player, "total", None)
        if spread is not None:
            if team == home and slot.get("home_line") is None:
                slot["home_line"] = float(spread)
            elif team == away and slot.get("home_line") is None:
                slot["home_line"] = -float(spread)
        if total is not None and slot.get("total") is None:
            slot["total"] = float(total)
    return out


def _line_context(
    line_rows: Optional[Sequence[dict]],
    entries: Optional[Sequence],
) -> dict:
    """Consensus numbers win. Entry lines fill gaps only when the feed has no book."""
    rows = [row for row in (line_rows or []) if isinstance(row, dict)]
    has_book = any(_book(row) for row in rows)
    index = consensus_index(rows)
    fallback = _entry_lines(entries)
    for key, extra in fallback.items():
        slot = index.setdefault(key, {})
        if extra.get("game_id") and not slot.get("game_id"):
            slot["game_id"] = extra["game_id"]
        if has_book:
            continue
        if slot.get("home_line") is None and extra.get("home_line") is not None:
            slot["home_line"] = extra["home_line"]
        if slot.get("total") is None and extra.get("total") is not None:
            slot["total"] = extra["total"]
    return index


def _bucket_draws(game_draws) -> list:
    """``[(away, home, away_pts, home_pts), ...]`` in first-seen order."""
    buckets: dict = {}
    order: list = []
    for row in game_draws or ():
        if not isinstance(row, (tuple, list)) or len(row) != 5:
            continue
        _game, away, home, away_pts, home_pts = row
        if not away or not home:
            continue
        try:
            away_val = float(away_pts)
            home_val = float(home_pts)
        except (TypeError, ValueError):
            continue
        key = (str(away).upper(), str(home).upper())
        slot = buckets.get(key)
        if slot is None:
            slot = ([], [])
            buckets[key] = slot
            order.append(key)
        slot[0].append(away_val)
        slot[1].append(home_val)
    return [(key[0], key[1], buckets[key][0], buckets[key][1]) for key in order]


def _outcomes(
    away_pts: Sequence[float],
    home_pts: Sequence[float],
    home_line: Optional[float],
    total: Optional[float],
) -> dict:
    n = len(home_pts)
    home_wins = away_wins = ties = 0
    home_covers = away_covers = push_covers = 0
    overs = unders = push_totals = 0
    for away_raw, home_raw in zip(away_pts, home_pts):
        away_i = _round_score(away_raw)
        home_i = _round_score(home_raw)
        if home_i > away_i:
            home_wins += 1
        elif away_i > home_i:
            away_wins += 1
        else:
            ties += 1
        if home_line is not None:
            adj = (home_i - away_i) + float(home_line)
            if abs(adj) <= _PUSH_EPS:
                push_covers += 1
            elif adj > 0:
                home_covers += 1
            else:
                away_covers += 1
        if total is not None:
            got = float(home_i + away_i)
            if abs(got - float(total)) <= _PUSH_EPS:
                push_totals += 1
            elif got > float(total):
                overs += 1
            else:
                unders += 1
    out = {
        "home_wins": home_wins,
        "away_wins": away_wins,
        "ties": ties,
        "home_win_prob": _rate(home_wins, n),
        "away_win_prob": _rate(away_wins, n),
    }
    if home_line is None:
        out["home_cover_prob"] = None
        out["away_cover_prob"] = None
        out["push_cover_prob"] = None
    else:
        out["home_cover_prob"] = _rate(home_covers, n)
        out["away_cover_prob"] = _rate(away_covers, n)
        out["push_cover_prob"] = _rate(push_covers, n)
    if total is None:
        out["over_prob"] = None
        out["under_prob"] = None
        out["push_total_prob"] = None
    else:
        out["over_prob"] = _rate(overs, n)
        out["under_prob"] = _rate(unders, n)
        out["push_total_prob"] = _rate(push_totals, n)
    return out


def build_game_projection_rows(
    game_draws,
    *,
    season: int,
    week: int,
    season_type: str,
    run_at: str,
    model_version: str,
    entries: Optional[Sequence] = None,
    line_rows: Optional[Sequence[dict]] = None,
) -> list:
    """One ``nfl_game_projections`` row per game that has a uuid ``game_id``."""
    lines = _line_context(line_rows, entries)
    rows: list = []
    for away, home, away_pts, home_pts in _bucket_draws(game_draws):
        meta = lines.get((away, home)) or {}
        game_id = meta.get("game_id")
        if not game_id:
            print(
                "game projections: skip %s@%s (no nfl_games uuid)" % (away, home),
                file=sys.stderr,
            )
            continue
        n = len(home_pts)
        home_p10, home_p50, home_p90 = _band(home_pts)
        away_p10, away_p50, away_p90 = _band(away_pts)
        home_line = meta.get("home_line")
        total = meta.get("total")
        row = {
            "season": int(season),
            "week": int(week),
            "season_type": season_type or "REG",
            "game_id": game_id,
            "run_at": run_at,
            "model": MODEL,
            "model_version": model_version,
            "n_draws": n,
            "home_team": home,
            "away_team": away,
            "home_mean": round(sum(home_pts) / float(n), 4),
            "home_p10": round(home_p10, 4),
            "home_p50": round(home_p50, 4),
            "home_p90": round(home_p90, 4),
            "away_mean": round(sum(away_pts) / float(n), 4),
            "away_p10": round(away_p10, 4),
            "away_p50": round(away_p50, 4),
            "away_p90": round(away_p90, 4),
            "consensus_home_line": _round(home_line, 4),
            "consensus_total": _round(total, 4),
        }
        row.update(_outcomes(away_pts, home_pts, home_line, total))
        rows.append({key: row.get(key) for key in GAME_ROW_FIELDS})
    return rows


def _redact(text: str, key: str) -> str:
    if key and key in text:
        return text.replace(key, "***")
    return text


def _post_games(url: str, rows: list, key: str):
    if "?" in url and (
        key in url or "api_key=" in url or "apikey=" in url.lower()
    ):
        raise GameProjectionsError("refusing to put the service-role key in the URL")
    if "/functions/v1/projections" in url:
        raise GameProjectionsError(
            "refusing to post game rows to /functions/v1/projections"
        )
    payload, _hdrs = http_json_post(
        url,
        rows,
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Prefer": "resolution=merge-duplicates,return=representation",
        },
    )
    return payload


def write_game_projection_file(rows: list, dest_dir: Path, stem: str) -> Path:
    """Dry-run JSON. Not a player-projection payload."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{stem}-game-projections.json"
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return path


def post_game_projection_rows(
    rows: list,
    *,
    key: str,
    poster=None,
) -> int:
    """Upsert game rows. The key stays in ``apikey`` and ``Authorization``."""
    if not rows:
        return 0
    if not key:
        raise GameProjectionsKeyMissing("GANGSTASH_SERVICE_ROLE_KEY is not set")
    url = GAME_PROJECTIONS_URL + "?on_conflict=" + ON_CONFLICT
    send = poster or _post_games
    try:
        payload = send(url, rows, key)
    except GameProjectionsError:
        raise
    except HttpError as exc:
        raise GameProjectionsError(_redact(str(exc), key)) from exc
    except Exception as exc:
        raise GameProjectionsError(_redact(str(exc), key)) from exc
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict) and payload.get("message"):
        raise GameProjectionsError(_redact(str(payload.get("message")), key))
    return len(rows)
