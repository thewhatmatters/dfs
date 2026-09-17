"""Slate source-coverage summary for NFL FanDuel classic.

Reuse ingest stats already computed in ``nfl.optimize``. No scrapes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

SKILL_POS = frozenset({"QB", "RB", "WR", "TE"})
LINEUPS_POS = frozenset({"RB", "WR", "TE"})


def _attr(obj: object, key: str):
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def pool_pos_counts(pool: Sequence[object] | None) -> dict[str, int]:
    skill = rb_wr_te = 0
    for p in pool or []:
        pos = str(_attr(p, "position") or "").upper()
        if pos in SKILL_POS:
            skill += 1
        if pos in LINEUPS_POS:
            rb_wr_te += 1
    return {"skill": skill, "rb_wr_te": rb_wr_te}


def build_slate_status(
    *,
    raw_n: int,
    after_ir_n: int,
    after_inj_n: int,
    pool: Sequence[object] | None = None,
    depth_source: str = "ourlads",
    depth: Mapping | None = None,
    targets: Mapping | None = None,
    snaps: Mapping | None = None,
    props: Mapping | None = None,
    injuries: Mapping | None = None,
    flags: Mapping | None = None,
) -> dict:
    """Compact coverage object for stderr + JSON ``slate_status``."""
    flags = dict(flags or {})
    counts = pool_pos_counts(pool)
    skill_n = counts["skill"]
    rb_wr_te = counts["rb_wr_te"]
    depth = dict(depth or {"skipped": True})
    targets = dict(targets or {"skipped": True})
    snaps = dict(snaps or {"skipped": True})
    props = dict(props or {"skipped": True})
    injuries = dict(injuries or {"skipped": True})

    depth_skipped = bool(depth.get("skipped") or flags.get("skip_depth"))
    targets_skipped = bool(targets.get("skipped") or flags.get("skip_targets"))
    snaps_skipped = bool(snaps.get("skipped") or flags.get("skip_snaps"))
    props_skipped = bool(props.get("skipped") or flags.get("skip_props"))
    inj_skipped = bool(injuries.get("skipped") or flags.get("skip_injuries"))

    src = str(depth.get("source") or depth_source or "ourlads").strip().lower()
    if src not in {"ourlads", "espn"}:
        src = "ourlads"

    targets_eligible = int(
        targets.get("slate_rb_wr_te") or targets.get("slate_wr_te") or rb_wr_te
    )
    snaps_eligible = int(snaps.get("slate_rb_wr_te") or rb_wr_te)

    gaps: list[str] = []
    if depth_skipped:
        gaps.append("depth skipped")
    if targets_skipped:
        if targets.get("choke"):
            gaps.append(f"targets {targets['choke']}")
        else:
            gaps.append("targets skipped")
    elif int(targets.get("joined") or 0) == 0:
        gaps.append("targets joined 0")
    if snaps_skipped:
        if snaps.get("choke"):
            gaps.append(f"snaps {snaps['choke']}")
        else:
            gaps.append("snaps skipped")
    elif int(snaps.get("joined") or 0) == 0:
        gaps.append("snaps joined 0")
    if props_skipped:
        reason = str(props.get("reason") or "")
        if reason == "PROPS_ODDS_KEY":
            gaps.append("props skipped (no Odds key)")
        else:
            gaps.append("props skipped")
    if inj_skipped:
        gaps.append("injuries skipped")
    for label, blob in (("targets", targets), ("snaps", snaps)):
        if blob.get("cache_only") or blob.get("seed_only"):
            gaps.append(f"{label} cache/seed only")

    return {
        "pool": {
            "csv": int(raw_n),
            "after_ir_na": int(after_ir_n),
            "after_injuries": int(after_inj_n),
        },
        "depth": {
            "source": src,
            "skipped": depth_skipped,
            "matched": 0 if depth_skipped else int(depth.get("matched") or 0),
            "eligible": skill_n,
            "label": "skill",
        },
        "targets": {
            "source": "lineups",
            "skipped": targets_skipped,
            "joined": 0 if targets_skipped else int(targets.get("joined") or 0),
            "eligible": targets_eligible,
            "label": "RB+WR+TE",
            "week": targets.get("week"),
            "choke": targets.get("choke"),
        },
        "snaps": {
            "source": "lineups",
            "skipped": snaps_skipped,
            "joined": 0 if snaps_skipped else int(snaps.get("joined") or 0),
            "eligible": snaps_eligible,
            "label": "RB+WR+TE",
            "week": snaps.get("week"),
            "choke": snaps.get("choke"),
        },
        "props": {
            "source": "odds",
            "skipped": props_skipped,
            "joined": 0
            if props_skipped
            else int(props.get("players_with_props") or props.get("joined") or 0),
            "eligible": skill_n,
            "label": "skill",
            "credits_remaining": props.get("credits_remaining"),
            "reason": props.get("reason"),
        },
        "injuries": {
            "source": "espn",
            "skipped": inj_skipped,
            "dropped": 0 if inj_skipped else int(injuries.get("dropped") or 0),
            "unmatched": 0 if inj_skipped else int(injuries.get("unmatched") or 0),
        },
        "gaps": gaps,
    }


def format_slate_status(status: Mapping) -> str:
    """Plain-language stderr block for “what’s the slate status?”."""
    pool = status.get("pool") or {}
    depth = status.get("depth") or {}
    targets = status.get("targets") or {}
    snaps = status.get("snaps") or {}
    props = status.get("props") or {}
    inj = status.get("injuries") or {}
    gaps = list(status.get("gaps") or [])

    lines = [
        "slate status",
        (
            f"pool  {pool.get('csv', 0)} csv → "
            f"{pool.get('after_ir_na', 0)} after IR/NA → "
            f"{pool.get('after_injuries', 0)} after injuries"
        ),
    ]
    if depth.get("skipped"):
        lines.append(f"depth  {depth.get('source', 'ourlads')}  skipped")
    else:
        lines.append(
            f"depth  {depth.get('source', 'ourlads')}  "
            f"{depth.get('matched', 0)} / {depth.get('eligible', 0)} skill"
        )
    lines.append(_join_line("targets", targets, "RB+WR+TE"))
    lines.append(_join_line("snaps", snaps, "RB+WR+TE"))
    if props.get("skipped"):
        why = str(props.get("reason") or "")
        extra = " (no Odds key)" if why == "PROPS_ODDS_KEY" else ""
        lines.append(f"props  skipped{extra}")
    else:
        cred = props.get("credits_remaining")
        cred_s = f"  credits_left {cred}" if cred is not None else ""
        lines.append(
            f"props  {props.get('joined', 0)} / {props.get('eligible', 0)} "
            f"skill{cred_s}"
        )
    if inj.get("skipped"):
        lines.append("injuries  skipped")
    else:
        lines.append(
            f"injuries  dropped {inj.get('dropped', 0)}  "
            f"unmatched {inj.get('unmatched', 0)}"
        )
    lines.append("gaps  " + (", ".join(gaps) if gaps else "none"))
    return "\n".join(lines)


def _join_line(name: str, blob: Mapping, default_label: str) -> str:
    label = blob.get("label") or default_label
    if blob.get("skipped"):
        extra = f" ({blob['choke']})" if blob.get("choke") else ""
        return f"{name}  skipped{extra}"
    week = blob.get("week")
    week_s = f"  week {week}" if week is not None else ""
    return (
        f"{name}  {blob.get('joined', 0)} / {blob.get('eligible', 0)} "
        f"{label}{week_s}"
    )
