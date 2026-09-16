#!/usr/bin/env python3
"""Repo-root shim: run skill preflight.py (A4 seam stays under the skill)."""
import runpy
from pathlib import Path

_TARGET = (
    Path(__file__).resolve().parent.parent
    / ".cursor"
    / "skills"
    / "optimize-ncaaf-classic"
    / "scripts"
    / "preflight.py"
)
runpy.run_path(str(_TARGET), run_name="__main__")
