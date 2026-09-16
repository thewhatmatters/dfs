"""Load API keys without overriding a real process environment.

Precedence: process env → repo `.env` → `~/.cursor/skills/.env`.
Empty values are skipped so a placeholder never shadows a real key.
Never log values.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILES = (
    _REPO_ROOT / ".env",
    Path.home() / ".cursor" / "skills" / ".env",
    Path.home() / ".claude" / ".env",
)


def _parse(path: Path) -> dict[str, str]:
    pairs: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return pairs
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and val:
            pairs[key] = val
    return pairs


def load() -> None:
    for path in _ENV_FILES:
        for key, val in _parse(path).items():
            if key not in os.environ:
                os.environ[key] = val


def get(name: str) -> str | None:
    load()
    val = os.environ.get(name, "").strip()
    return val or None
