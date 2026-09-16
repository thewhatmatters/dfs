#!/usr/bin/env python3
"""Readiness check for optimize-ncaaf-classic (spec A6).

I/O: stdout JSON {overall, checks, summary} · stderr human board · exit 1 only on `down`.
States per check: ready | degraded | gated | down  (with a gate id).
"""
import argparse
import csv
import json
import sys
from pathlib import Path

MARK = {"ready": "✅", "degraded": "⚠ ", "gated": "🔒", "down": "⛔"}
RANK = {"ready": 0, "degraded": 1, "gated": 2, "down": 3}

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_DIR.parents[2]  # dfs/.cursor/skills/<name> → dfs


def check_repo():
    rules = REPO_ROOT / "ncaaf" / "docs" / "sites" / "fanduel-ncaaf.md"
    pkg = REPO_ROOT / "ncaaf" / "optimize.py"
    if rules.is_file() and pkg.is_file():
        return ("ready", None, f"repo {REPO_ROOT.name}")
    return ("down", "REPO", f"missing ncaaf package or rules under {REPO_ROOT}")


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


def check_join(path: str | None):
    """CSV Team/Opponent vs ncaaf.teams.TEAMS. Skip when there is no file yet."""
    if not path:
        return ("ready", None, "no --csv yet (join skipped)")
    p = Path(path).expanduser()
    if not p.is_file():
        return ("ready", None, "csv missing (join skipped)")
    sys.path.insert(0, str(REPO_ROOT))
    from ncaaf.teams import TEAMS  # noqa: WPS433

    abbrevs: set[str] = set()
    with p.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            for key in ("Team", "Opponent"):
                val = (row.get(key) or "").strip().upper()
                if val:
                    abbrevs.add(val)
    missing = sorted(a for a in abbrevs if a not in TEAMS)
    if missing:
        return (
            "down",
            "LINES_JOIN",
            f"{', '.join(missing)} — add each to ncaaf/teams.py",
        )
    return ("ready", None, f"{len(abbrevs)} teams mapped")


def check_lines(use_fppg: bool, lines_json: str | None):
    if use_fppg:
        return ("ready", None, "FPPG opt-in (prior season, not this slate)")
    if lines_json:
        p = Path(lines_json).expanduser()
        if not p.is_file():
            return ("down", "LINES_JSON", f"missing {p}")
        return ("ready", None, f"lines-json {p.name}")
    sys.path.insert(0, str(REPO_ROOT))
    from ncaaf.lines import available_source  # noqa: WPS433

    src = available_source()
    if src:
        return ("ready", None, f"lines source {src}")
    return (
        "gated",
        "LINES_KEY",
        "set CFBD_API_KEY (https://collegefootballdata.com/key) or ODDS_API_KEY — will not fall back to FPPG",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--csv")
    ap.add_argument("--use-fppg", action="store_true")
    ap.add_argument("--lines-json")
    args = ap.parse_args()
    checks = {
        "repo": check_repo(),
        "pulp": check_pulp(),
        "csv": check_csv(args.csv),
        "join": check_join(args.csv),
        "lines": check_lines(args.use_fppg, args.lines_json),
    }
    overall = "ready"
    for s, _g, _d in checks.values():
        if RANK[s] > RANK[overall]:
            overall = s
    print("optimize-ncaaf-classic readiness", file=sys.stderr)
    for n, (s, g, d) in checks.items():
        suffix = f"  [{g}]" if g else ""
        print(f"  {MARK[s]} {n:<8} {d}{suffix}", file=sys.stderr)
    print(f"  → overall: {overall}", file=sys.stderr)
    down_chokes = [g for s, g, _d in checks.values() if g and s == "down"]
    gated_chokes = [g for s, g, _d in checks.values() if g and s == "gated"]
    chokes = down_chokes or gated_chokes
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
