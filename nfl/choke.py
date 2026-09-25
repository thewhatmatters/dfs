"""Named ingest/solver chokes for NFL. Catalog: nfl/docs/data/sources.md."""

from __future__ import annotations

import sys
from typing import Any

from nfl.depth import (
    DepthError,
    EspnDepthError,
    GangstashDepthError,
    GangstashDepthKeyMissing,
)
from nfl.http import HttpAuthError
from nfl.injuries import InjuryError
from nfl.lines import (
    LinesAuthError,
    LinesError,
    LinesGangstashError,
    LinesGangstashKeyMissing,
    LinesKeyMissing,
)
from nfl.props import PropsError, PropsKeyMissing
from nfl.teams import UnmappedTeam


def line(choke_id: str, message: str) -> str:
    return f"choke {choke_id}: {message}"


def emit(choke_id: str, message: str) -> None:
    print(line(choke_id, message), file=sys.stderr)


def stamp(payload: dict[str, Any], choke_id: str, message: str) -> dict[str, Any]:
    payload["choke"] = choke_id
    payload["error"] = message
    return payload


def lines_id(exc: BaseException) -> str:
    if isinstance(exc, LinesGangstashKeyMissing):
        return "LINES_GANGSTASH_KEY"
    if isinstance(exc, LinesKeyMissing):
        return "LINES_KEY"
    if isinstance(exc, UnmappedTeam):
        return "LINES_JOIN"
    if isinstance(exc, LinesGangstashError):
        return "LINES_GANGSTASH"
    if isinstance(exc, (LinesAuthError, HttpAuthError)):
        return "LINES_AUTH"
    text = str(exc).casefold()
    if "json" in text:
        return "LINES_JSON"
    return "LINES_ODDS"


def depth_id(exc: BaseException) -> str:
    if isinstance(exc, UnmappedTeam):
        return "DEPTH_JOIN"
    if isinstance(exc, GangstashDepthKeyMissing):
        return "DEPTH_GANGSTASH_KEY"
    if isinstance(exc, GangstashDepthError):
        return "DEPTH_GANGSTASH"
    if isinstance(exc, EspnDepthError):
        return "DEPTH_ESPN"
    if isinstance(exc, DepthError):
        return "DEPTH_OURLADS"
    return "DEPTH_OURLADS"


def inj_id(exc: BaseException) -> str:
    if isinstance(exc, InjuryError):
        return "INJ_ESPN"
    return "INJ_ESPN"


def props_id(exc: BaseException) -> str:
    if isinstance(exc, PropsKeyMissing):
        return "PROPS_GANGSTASH_KEY"
    if isinstance(exc, PropsError):
        return "PROPS_GANGSTASH"
    return "PROPS_GANGSTASH"
