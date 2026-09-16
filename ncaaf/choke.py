"""Named ingest/solver chokes. Catalog: ncaaf/docs/data/sources.md."""

from __future__ import annotations

import sys
from typing import Any

from ncaaf.lines import LinesAuthError, LinesError, LinesKeyMissing
from ncaaf.mix import MixError
from ncaaf.ourlads import DepthError
from ncaaf.props import PropsError, PropsKeyMissing
from ncaaf.teams import UnmappedTeam


def line(choke_id: str, message: str) -> str:
    return f"choke {choke_id}: {message}"


def emit(choke_id: str, message: str) -> None:
    print(line(choke_id, message), file=sys.stderr)


def stamp(payload: dict[str, Any], choke_id: str, message: str) -> dict[str, Any]:
    payload["choke"] = choke_id
    payload["error"] = message
    return payload


def lines_id(exc: BaseException) -> str:
    if isinstance(exc, LinesKeyMissing):
        return "LINES_KEY"
    if isinstance(exc, UnmappedTeam):
        return "LINES_JOIN"
    if isinstance(exc, LinesAuthError):
        return "LINES_AUTH"
    text = str(exc).casefold()
    if "cfbd" in text:
        return "LINES_CFBD"
    if "odds" in text:
        return "LINES_ODDS"
    if "json" in text:
        return "LINES_JSON"
    return "LINES_INGEST"


def depth_id(exc: BaseException) -> str:
    if isinstance(exc, UnmappedTeam):
        return "DEPTH_JOIN"
    if isinstance(exc, DepthError):
        return "DEPTH_OURLADS"
    return "DEPTH_OURLADS"


def mix_id(exc: BaseException) -> str:
    text = str(exc).casefold()
    if "401" in text or "403" in text or "key" in text:
        return "MIX_AUTH"
    return "MIX_CFBD"


def props_id(exc: BaseException) -> str:
    if isinstance(exc, PropsKeyMissing):
        return "PROPS_ODDS_KEY"
    if isinstance(exc, PropsError):
        return "PROPS_ODDS"
    return "PROPS_ODDS"
