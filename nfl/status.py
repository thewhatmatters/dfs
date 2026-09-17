#!/usr/bin/env python3
"""Print NFL FanDuel classic slate source coverage. No solve.

Same ``--csv`` / ``--agent`` / ``--out`` flags as ``nfl.optimize``.
Exit 0 after the report (adds ``--slate-status`` if omitted).

Coverage is cash-relevant salary bands (see ``nfl.slate_status``), not the
full FanDuel pool.
"""

from __future__ import annotations

import sys

from nfl.optimize import main as optimize_main


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a == "--slate-status" or a.startswith("--slate-status=") for a in argv):
        argv.append("--slate-status")
    return optimize_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
