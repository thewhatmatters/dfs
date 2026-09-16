#!/usr/bin/env python3
"""Thin CLI seam: run the repo FanDuel NFL optimizer (JSON stdout).

I/O: same flags as nfl.optimize · stdout JSON · stderr diagnostics.
"""
import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))
sys.argv[0] = str(REPO_ROOT / "nfl" / "optimize.py")
runpy.run_module("nfl.optimize", run_name="__main__")
