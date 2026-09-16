"""Name normalize + Jr. strip for FanDuel / ESPN / Odds joins."""

from __future__ import annotations

import re

_GENERATIONAL = re.compile(r"\b(?:jr|sr|ii|iii|iv|v|vi)\s*$", re.I)


def norm_name(raw: str) -> str:
    s = (raw or "").casefold().replace(".", "").replace("'", "").replace("’", "")
    s = s.replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def match_key(raw: str) -> str:
    """norm_name with Jr/Sr/II/III/IV/V stripped."""
    return _GENERATIONAL.sub("", norm_name(raw)).strip()
