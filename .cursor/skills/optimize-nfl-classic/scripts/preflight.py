#!/usr/bin/env python3
"""Readiness check for optimize-nfl-classic (spec A6).

I/O: stdout JSON {overall, checks, summary} · stderr human board · exit 1 only on `down`.
States per check: ready | degraded | gated | down  (with a gate id).
"""
import argparse
import json
import sys
from pathlib import Path

MARK = {"ready": "✅", "degraded": "⚠ ", "gated": "🔒", "down": "⛔"}
RANK = {"ready": 0, "degraded": 1, "gated": 2, "down": 3}

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_DIR.parents[2]  # dfs/.cursor/skills/<name> → dfs


def check_repo():
    rules = REPO_ROOT / "nfl" / "docs" / "sites" / "fanduel-nfl.md"
    pkg = REPO_ROOT / "nfl" / "optimize.py"
    if rules.is_file() and pkg.is_file():
        return ("ready", None, f"repo {REPO_ROOT.name}")
    return ("down", "REPO", f"missing nfl package or rules under {REPO_ROOT}")


def check_pulp():
    try:
        import pulp  # noqa: F401
    except ImportError:
        return (
            "gated",
            "SOLVER_PULP",
            "pulp not importable — exact ILP unavailable (greedy degrade)",
        )
    return ("ready", None, "pulp importable")


def check_csv(path: str | None):
    if not path:
        return ("degraded", None, "no --csv yet (pass when solving)")
    p = Path(path).expanduser()
    if not p.is_file():
        return ("down", "CSV_FANDUEL", f"missing {p}")
    return ("ready", None, p.name)


def check_lines(lines_json: str | None):
    if lines_json:
        p = Path(lines_json).expanduser()
        if not p.is_file():
            return ("down", "LINES_JSON", f"missing {p}")
        return ("ready", None, f"lines-json {p.name}")
    sys.path.insert(0, str(REPO_ROOT))
    from nfl.env import get  # noqa: WPS433

    if get("ODDS_API_KEY") or get("THE_ODDS_API_KEY"):
        return ("ready", None, "lines source odds")
    return (
        "gated",
        "LINES_KEY",
        "set ODDS_API_KEY — will not fall back to FPPG",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--csv")
    ap.add_argument("--lines-json")
    args = ap.parse_args()
    checks = {
        "repo": check_repo(),
        "pulp": check_pulp(),
        "csv": check_csv(args.csv),
        "lines": check_lines(args.lines_json),
    }
    overall = "ready"
    for s, _g, _d in checks.values():
        if RANK[s] > RANK[overall]:
            overall = s
    print("optimize-nfl-classic readiness", file=sys.stderr)
    for n, (s, g, d) in checks.items():
        suffix = f"  [{g}]" if g else ""
        print(f"  {MARK[s]} {n:<8} {d}{suffix}", file=sys.stderr)
    print(f"  → overall: {overall}", file=sys.stderr)
    chokes = [g for s, g, _d in checks.values() if g and s in {"gated", "down"}]
    if chokes:
        print(f"choke {chokes[0]}", file=sys.stderr)
    payload = {
        "overall": overall,
        "checks": {
            n: {"status": s, "gate": g, "detail": d} for n, (s, g, d) in checks.items()
        },
        "summary": f"{overall}: " + ", ".join(f"{n}={s}" for n, (s, _, _) in checks.items()),
        "choke": chokes[0] if chokes else None,
    }
    print(json.dumps(payload, indent=2))
    sys.exit(1 if overall == "down" else 0)


if __name__ == "__main__":
    main()
