"""Slate source-coverage summary for NFL FanDuel classic.

Reuse ingest already attached on the pool. No scrapes. No ILP changes.

Coverage **leads with Randy’s cash-relevant salary bands** (strictly greater
than the floors below). Full-pool counts stay on a secondary ``all pool``
line and in JSON ``*.all``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

# Relevant iff salary > floor for that FanDuel position. v1 constants
# (no CLI overrides — keep ``--slate-status`` un-noisy).
RELEVANT_SALARY_FLOOR: dict[str, int] = {
    "QB": 6500,
    "WR": 5000,
    "RB": 5000,
    "TE": 4500,
    "D": 3500,
}

SKILL_POS = frozenset({"QB", "RB", "WR", "TE"})
LINEUPS_POS = frozenset({"RB", "WR", "TE"})
POS_ORDER = ("QB", "RB", "WR", "TE", "D")
MISSING_STDERR_CAP = 6

_PROP_FIELDS = (
    "prop_fd",
    "prop_pass_yds",
    "prop_pass_tds",
    "prop_rush_yds",
    "prop_rec_yds",
    "prop_receptions",
)


def _attr(obj: object, key: str):
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def canon_pos(pos: object) -> str:
    p = str(pos or "").strip().upper()
    if p in {"D", "DEF", "DST"}:
        return "D"
    return p


def relevant_salary_floor(pos: object) -> int | None:
    return RELEVANT_SALARY_FLOOR.get(canon_pos(pos))


def is_relevant(player: object) -> bool:
    """True when salary is strictly greater than the position floor."""
    floor = relevant_salary_floor(_attr(player, "position"))
    if floor is None:
        return False
    raw = _attr(player, "salary")
    try:
        salary = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return False
    return salary > floor


def player_ref(player: object) -> dict:
    raw = _attr(player, "salary")
    try:
        salary = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        salary = 0
    return {
        "name": str(_attr(player, "name") or ""),
        "pos": canon_pos(_attr(player, "position")),
        "team": str(_attr(player, "team") or ""),
        "salary": salary,
    }


def _sort_refs(rows: Sequence[Mapping]) -> list[dict]:
    return sorted(
        (dict(r) for r in rows),
        key=lambda r: (-int(r.get("salary") or 0), str(r.get("name") or "")),
    )


def count_by_pos(
    pool: Sequence[object] | None, *, relevant_only: bool = False
) -> dict[str, int]:
    counts = {k: 0 for k in POS_ORDER}
    for p in pool or []:
        if relevant_only and not is_relevant(p):
            continue
        pos = canon_pos(_attr(p, "position"))
        if pos in counts:
            counts[pos] += 1
    return counts


def pool_pos_counts(pool: Sequence[object] | None) -> dict[str, int]:
    skill = rb_wr_te = 0
    for p in pool or []:
        pos = canon_pos(_attr(p, "position"))
        if pos in SKILL_POS:
            skill += 1
        if pos in LINEUPS_POS:
            rb_wr_te += 1
    return {"skill": skill, "rb_wr_te": rb_wr_te}


def _has_depth(player: object) -> bool:
    return _attr(player, "depth_rank") is not None


def _has_targets(player: object) -> bool:
    return (
        _attr(player, "target_share") is not None
        or _attr(player, "targets") is not None
    )


def _has_snaps(player: object) -> bool:
    return (
        _attr(player, "snap_share") is not None or _attr(player, "snaps") is not None
    )


def _has_props(player: object) -> bool:
    if _attr(player, "prop_status") == "props":
        return True
    return any(_attr(player, key) is not None for key in _PROP_FIELDS)


def _has_implied_opp(player: object) -> bool:
    return _attr(player, "implied_opp") is not None


def _select(
    pool: Sequence[object],
    *,
    relevant_only: bool,
    positions: frozenset[str],
) -> list[object]:
    out: list[object] = []
    for p in pool:
        if canon_pos(_attr(p, "position")) not in positions:
            continue
        if relevant_only and not is_relevant(p):
            continue
        out.append(p)
    return out


def _coverage(
    players: Sequence[object], has
) -> tuple[int, int, list[dict]]:
    matched = 0
    missing: list[dict] = []
    for p in players:
        if has(p):
            matched += 1
        else:
            missing.append(player_ref(p))
    return matched, len(players), _sort_refs(missing)


def _feed_blob(
    *,
    source: str,
    skipped: bool,
    matched: int,
    eligible: int,
    label: str,
    missing: Sequence[Mapping],
    extra: Mapping | None = None,
) -> dict:
    blob = {
        "source": source,
        "skipped": skipped,
        "matched": 0 if skipped else matched,
        "eligible": eligible,
        "label": label,
        "missing": [] if skipped else list(missing),
    }
    if extra:
        blob.update(dict(extra))
    return blob


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
    pool = list(pool or [])
    full_counts = count_by_pos(pool)
    rel_counts = count_by_pos(pool, relevant_only=True)
    full_n = sum(full_counts.values())
    rel_n = sum(rel_counts.values())

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

    rel_skill = _select(pool, relevant_only=True, positions=SKILL_POS)
    rel_lineups = _select(pool, relevant_only=True, positions=LINEUPS_POS)
    rel_rb = _select(pool, relevant_only=True, positions=frozenset({"RB"}))
    rel_wr_te = _select(pool, relevant_only=True, positions=frozenset({"WR", "TE"}))
    rel_def = _select(pool, relevant_only=True, positions=frozenset({"D"}))

    all_skill = _select(pool, relevant_only=False, positions=SKILL_POS)
    all_lineups = _select(pool, relevant_only=False, positions=LINEUPS_POS)
    all_rb = _select(pool, relevant_only=False, positions=frozenset({"RB"}))

    d_m, d_e, d_miss = _coverage(rel_skill, _has_depth)
    t_m, t_e, t_miss = _coverage(rel_lineups, _has_targets)
    s_rb_m, s_rb_e, s_rb_miss = _coverage(rel_rb, _has_snaps)
    s_wr_m, s_wr_e, s_wr_miss = _coverage(rel_wr_te, _has_snaps)
    p_m, p_e, p_miss = _coverage(rel_skill, _has_props)
    dst_m, dst_e, dst_miss = _coverage(rel_def, _has_implied_opp)
    def_prop_m, def_prop_e, def_prop_miss = _coverage(rel_def, _has_props)

    ad_m, ad_e, _ = _coverage(all_skill, _has_depth)
    at_m, at_e, _ = _coverage(all_lineups, _has_targets)
    as_m, as_e, _ = _coverage(all_rb, _has_snaps)
    ap_m, ap_e, _ = _coverage(all_skill, _has_props)

    gaps: list[str] = []
    if depth_skipped:
        gaps.append("depth skipped")
    elif d_e and d_m == 0:
        gaps.append("depth joined 0")
    if targets_skipped:
        if targets.get("choke"):
            gaps.append(f"targets {targets['choke']}")
        else:
            gaps.append("targets skipped")
    elif t_e and t_m == 0:
        gaps.append("targets joined 0")
    if snaps_skipped:
        if snaps.get("choke"):
            gaps.append(f"snaps {snaps['choke']}")
        else:
            gaps.append("snaps skipped")
    elif s_rb_e and s_rb_m == 0:
        gaps.append("snaps joined 0")
    if props_skipped:
        reason = str(props.get("reason") or "")
        if reason == "PROPS_GANGSTASH_KEY":
            gaps.append("props skipped (no Gangstash key)")
        else:
            gaps.append("props skipped")
    elif p_e and p_m == 0:
        gaps.append("props joined 0")
    if inj_skipped:
        gaps.append("injuries skipped")
    for label, blob in (("targets", targets), ("snaps", snaps)):
        if blob.get("cache_only") or blob.get("seed_only"):
            gaps.append(f"{label} cache/seed only")

    return {
        "floors": dict(RELEVANT_SALARY_FLOOR),
        "relevant": {"total": rel_n, "by_pos": rel_counts},
        "pool": {
            "csv": int(raw_n),
            "after_ir_na": int(after_ir_n),
            "after_injuries": int(after_inj_n),
            "full": full_n,
            "relevant": rel_n,
            "by_pos": rel_counts,
            "full_by_pos": full_counts,
        },
        "depth": _feed_blob(
            source=src,
            skipped=depth_skipped,
            matched=d_m,
            eligible=d_e,
            label="relevant skill",
            missing=d_miss,
            extra={
                "all": {
                    "matched": 0 if depth_skipped else ad_m,
                    "eligible": ad_e,
                    "label": "skill",
                }
            },
        ),
        "targets": _feed_blob(
            source="lineups",
            skipped=targets_skipped,
            matched=t_m,
            eligible=t_e,
            label="relevant RB+WR+TE",
            missing=t_miss,
            extra={
                "week": targets.get("week"),
                "choke": targets.get("choke"),
                "joined": 0 if targets_skipped else t_m,
                "all": {
                    "matched": 0 if targets_skipped else at_m,
                    "eligible": at_e,
                    "label": "RB+WR+TE",
                },
            },
        ),
        "snaps": _feed_blob(
            source="lineups",
            skipped=snaps_skipped,
            matched=s_rb_m,
            eligible=s_rb_e,
            label="relevant RB",
            missing=s_rb_miss,
            extra={
                "week": snaps.get("week"),
                "choke": snaps.get("choke"),
                "joined": 0 if snaps_skipped else s_rb_m,
                "wr_te": {
                    "matched": 0 if snaps_skipped else s_wr_m,
                    "eligible": s_wr_e,
                    "missing": [] if snaps_skipped else s_wr_miss,
                },
                "all": {
                    "matched": 0 if snaps_skipped else as_m,
                    "eligible": as_e,
                    "label": "RB",
                },
            },
        ),
        "props": _feed_blob(
            source="gangstash",
            skipped=props_skipped,
            matched=p_m,
            eligible=p_e,
            label="relevant skill",
            missing=p_miss,
            extra={
                "joined": 0 if props_skipped else p_m,
                "reason": props.get("reason"),
                "def": {
                    "matched": 0 if props_skipped else def_prop_m,
                    "eligible": def_prop_e,
                    "missing": [] if props_skipped else def_prop_miss,
                    "label": "relevant DEF",
                },
                "all": {
                    "matched": 0 if props_skipped else ap_m,
                    "eligible": ap_e,
                    "label": "skill",
                },
            },
        ),
        "dst": {
            "source": "vegas",
            "matched": dst_m,
            "eligible": dst_e,
            "label": "relevant DEF implied opp",
            "missing": dst_miss,
        },
        "injuries": {
            "source": "espn",
            "skipped": inj_skipped,
            "applied": not inj_skipped,
            "dropped": 0 if inj_skipped else int(injuries.get("dropped") or 0),
            "unmatched": 0 if inj_skipped else int(injuries.get("unmatched") or 0),
        },
        "gaps": gaps,
    }


def format_slate_status(status: Mapping) -> str:
    """Plain-language stderr block for “what’s the slate status?”."""
    pool = status.get("pool") or {}
    relevant = status.get("relevant") or {}
    depth = status.get("depth") or {}
    targets = status.get("targets") or {}
    snaps = status.get("snaps") or {}
    props = status.get("props") or {}
    dst = status.get("dst") or {}
    inj = status.get("injuries") or {}
    gaps = list(status.get("gaps") or [])
    by_pos = relevant.get("by_pos") or pool.get("by_pos") or {}
    pos_bits = "  ".join(f"{pos} {int(by_pos.get(pos) or 0)}" for pos in POS_ORDER)
    rel_n = int(relevant.get("total") or pool.get("relevant") or 0)
    full_n = int(pool.get("full") or 0)

    lines = [
        "slate status",
        f"relevant  {rel_n} / {full_n}  {pos_bits}",
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
            f"{depth.get('matched', 0)} / {depth.get('eligible', 0)} relevant skill"
        )
    lines.append(_join_line("targets", targets, "relevant RB+WR+TE"))
    lines.append(_snaps_line(snaps))
    if props.get("skipped"):
        why = str(props.get("reason") or "")
        extra = " (no Gangstash key)" if why == "PROPS_GANGSTASH_KEY" else ""
        lines.append(f"props  skipped{extra}")
    else:
        lines.append(
            f"props  {props.get('matched', props.get('joined', 0))} / "
            f"{props.get('eligible', 0)} relevant skill"
        )
    lines.append(
        f"dst  {dst.get('matched', 0)} / {dst.get('eligible', 0)} "
        f"{dst.get('label') or 'relevant DEF implied opp'}"
    )
    if inj.get("skipped"):
        lines.append("injuries  skipped")
    else:
        lines.append(
            f"injuries  applied  dropped {inj.get('dropped', 0)}  "
            f"unmatched {inj.get('unmatched', 0)}"
        )
    lines.append("gaps  " + (", ".join(gaps) if gaps else "none"))
    if not depth.get("skipped"):
        lines.extend(_missing_block("depth", depth.get("missing") or []))
    if not targets.get("skipped"):
        lines.extend(_missing_block("targets", targets.get("missing") or []))
    if not snaps.get("skipped"):
        lines.extend(_missing_block("snaps", snaps.get("missing") or []))
    if not props.get("skipped"):
        lines.extend(_missing_block("props", props.get("missing") or []))
    lines.extend(_missing_block("dst", dst.get("missing") or []))
    lines.append(_all_pool_line(status))
    return "\n".join(lines)


def _join_line(name: str, blob: Mapping, default_label: str) -> str:
    label = blob.get("label") or default_label
    if blob.get("skipped"):
        extra = f" ({blob['choke']})" if blob.get("choke") else ""
        return f"{name}  skipped{extra}"
    week = blob.get("week")
    week_s = f"  week {week}" if week is not None else ""
    n = blob.get("matched", blob.get("joined", 0))
    return f"{name}  {n} / {blob.get('eligible', 0)} {label}{week_s}"


def _snaps_line(snaps: Mapping) -> str:
    if snaps.get("skipped"):
        extra = f" ({snaps['choke']})" if snaps.get("choke") else ""
        return f"snaps  skipped{extra}"
    week = snaps.get("week")
    week_s = f"  week {week}" if week is not None else ""
    wr_te = snaps.get("wr_te") or {}
    return (
        f"snaps  {snaps.get('matched', 0)} / {snaps.get('eligible', 0)} "
        f"relevant RB  (WR/TE {wr_te.get('matched', 0)} / "
        f"{wr_te.get('eligible', 0)}){week_s}"
    )


def _fmt_player(row: Mapping) -> str:
    sal = row.get("salary")
    try:
        sal_s = f"${int(sal):,}"
    except (TypeError, ValueError):
        sal_s = ""
    bits = [
        str(row.get("name") or "").strip(),
        str(row.get("pos") or "").strip(),
        str(row.get("team") or "").strip(),
        sal_s,
    ]
    return "  ".join(b for b in bits if b)


def _missing_block(feed: str, rows: Sequence[Mapping]) -> list[str]:
    if not rows:
        return []
    n = len(rows)
    shown = list(rows[:MISSING_STDERR_CAP])
    extra = n - len(shown)
    lines = [f"missing {feed} ({n}):"]
    for row in shown:
        lines.append(f"  {_fmt_player(row)}")
    if extra:
        lines.append(f"  +{extra} more")
    return lines


def _ratio(blob: Mapping | None, matched_key: str = "matched") -> str:
    blob = blob or {}
    return f"{blob.get(matched_key, 0)}/{blob.get('eligible', 0)}"


def _all_pool_line(status: Mapping) -> str:
    depth = (status.get("depth") or {}).get("all") or {}
    targets = (status.get("targets") or {}).get("all") or {}
    snaps = (status.get("snaps") or {}).get("all") or {}
    props = (status.get("props") or {}).get("all") or {}
    return (
        f"all pool  depth {_ratio(depth)} skill  "
        f"targets {_ratio(targets)} RB+WR+TE  "
        f"snaps {_ratio(snaps)} RB  "
        f"props {_ratio(props)} skill"
    )
