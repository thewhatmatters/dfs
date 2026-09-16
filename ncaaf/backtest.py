"""Score a solved lineup against a known perfect card (hindsight overlap).

Not an ILP objective. Use after a finished slate to see if script/floor
refinements pick more of the perfect 7. Do not fit the solver to this file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ncaaf.ourlads import match_key

PERFECT_DIR = Path(__file__).resolve().parent / "data" / "perfect"


def _key(name: str, team: str) -> tuple[str, str]:
    return (match_key(name), (team or "").strip().upper())


def player_keys(rows: list[dict]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for r in rows:
        name = r.get("name") or ""
        team = r.get("team") or ""
        if name and team:
            out.add(_key(name, team))
    return out


def picker_rows(lineup: dict) -> list[dict]:
    if lineup.get("picker"):
        return list(lineup["picker"])
    slots = lineup.get("slots") or {}
    return list(slots.values())


def compare(
    solved: dict,
    perfect: dict,
    *,
    baseline: dict | None = None,
) -> dict:
    ours = player_keys(picker_rows(solved.get("lineup") or solved))
    gold = player_keys(perfect.get("players") or [])
    hit = ours & gold
    miss = gold - ours
    extra = ours - gold
    base_hit: set[tuple[str, str]] = set()
    if baseline and baseline.get("players"):
        base_hit = player_keys(baseline["players"]) & gold
    elif perfect.get("baseline_lock"):
        base_hit = player_keys(perfect["baseline_lock"].get("players") or []) & gold

    def names(keys: set[tuple[str, str]]) -> list[str]:
        gold_rows = perfect.get("players") or []
        by = {_key(r["name"], r["team"]): r["name"] for r in gold_rows if r.get("name")}
        our_rows = picker_rows(solved.get("lineup") or solved)
        by_ours = {_key(r.get("name") or "", r.get("team") or ""): r.get("name") or "" for r in our_rows}
        return sorted(by.get(k) or by_ours.get(k) or f"{k[0]} {k[1]}" for k in keys)

    return {
        "contest": perfect.get("contest"),
        "overlap": len(hit),
        "perfect_size": len(gold),
        "baseline_overlap": len(base_hit),
        "improved": len(hit) > len(base_hit),
        "hits": names(hit),
        "misses": names(miss),
        "extras": names(extra),
        "solved_salary": (solved.get("lineup") or solved).get("salary"),
        "perfect_salary": perfect.get("salary"),
        "perfect_fd": perfect.get("fd_points"),
        "note": (
            "Hindsight overlap only — do not train the ILP on this card. "
            "Improvement vs the entered lock is extra hits on the perfect 7."
        ),
    }


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--perfect",
        default=str(PERFECT_DIR / "133865.json"),
        help="LineStar/user perfect-card JSON",
    )
    ap.add_argument(
        "--lineup",
        required=True,
        help="optimizer JSON (--out from ncaaf.optimize)",
    )
    args = ap.parse_args(argv)
    perfect = load_json(Path(args.perfect).expanduser())
    solved = load_json(Path(args.lineup).expanduser())
    report = compare(solved, perfect)
    print(json.dumps(report, indent=2))
    print(
        f"overlap {report['overlap']}/{report['perfect_size']}  "
        f"baseline {report['baseline_overlap']}  "
        f"hits {report['hits']}",
        file=sys.stderr,
    )
    if report["misses"]:
        print(f"miss  {report['misses']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
