"""GPP leverage seats on the mean card.

Mean ILP on implied totals never selects a +14 dog WR1 or a +21 dog QB —
their implied is 13–18 vs a favorite's 35–39. Sim p90 also misses those
upsets: moneyline ~+1000 is P(win)≈8%, which sits past p90.

Two slates agreed this is construction, not a name list: 133970 Coleman
(cheap dog WR filler) and 134050 Harris + Mestemaker. Constraints fire
only when the pool actually has those players. `--leverage=off` disables.
Do not fit the ILP to a perfect-card roster.
"""

from __future__ import annotations

from ncaaf.players import Player
from ncaaf.script import BLOWOUT, SIT_RUN

PASS_CATCH = frozenset({"WR", "TE"})


def dog_wr1(player: Player) -> bool:
    """Starter WR/TE on a +BLOWOUT dog. Depth 1 only — not a d2 dart."""
    if (player.position or "").upper() not in PASS_CATCH:
        return False
    if player.depth_rank != 1:
        return False
    if player.spread is None:
        return False
    return float(player.spread) >= BLOWOUT


def huge_dog_qb(player: Player) -> bool:
    """Starter QB on a +SIT_RUN dog (the favorite is in the sit band)."""
    if (player.position or "").upper() != "QB":
        return False
    if player.depth_rank != 1:
        return False
    if player.spread is None:
        return False
    return float(player.spread) >= SIT_RUN


def add_ilp_constraints(prob, x: dict, pool: list[Player]) -> list[str]:
    """Require ≥1 dog WR1 and ≥1 huge-dog QB when those sets are non-empty.

    `x` is the solver's (index, slot) → binary map. No-ops when a set has
    no eligible (player, slot) pair. Notes are for Lineup.notes.
    """
    import pulp

    notes: list[str] = []
    wr_idx = {i for i, p in enumerate(pool) if dog_wr1(p)}
    wr_keys = [k for k in x if k[0] in wr_idx]
    if wr_keys:
        prob += pulp.lpSum(x[k] for k in wr_keys) >= 1, "leverage_dog_wr1"
        notes.append(
            f"leverage: ≥1 +{BLOWOUT:g} WR1/TE1 ({len(wr_idx)} in pool)"
        )
    qb_idx = {i for i, p in enumerate(pool) if huge_dog_qb(p)}
    qb_keys = [k for k in x if k[0] in qb_idx]
    if qb_keys:
        prob += pulp.lpSum(x[k] for k in qb_keys) >= 1, "leverage_dog_qb"
        notes.append(
            f"leverage: ≥1 +{SIT_RUN:g} dog QB ({len(qb_idx)} in pool)"
        )
    return notes


def smash_board(
    pool: list[Player],
    sim_by_pid: dict,
    *,
    n: int = 8,
) -> list[dict]:
    """Top p99 players (sim tail). Identification board — not the ILP."""
    rows: list[dict] = []
    for p in pool:
        st = sim_by_pid.get(p.pid)
        if st is None:
            continue
        p99 = float(getattr(st, "p99", 0.0) or 0.0)
        if p99 <= 0:
            continue
        sal = max(int(p.salary), 1)
        rows.append(
            {
                "player": p.name,
                "pos": p.position,
                "team": p.team,
                "salary": p.salary,
                "spread": p.spread,
                "proj": round(p.projection, 2),
                "p99": round(p99, 2),
                "p99_per_k": round(p99 / (sal / 1000.0), 2),
                "dog_wr1": dog_wr1(p),
                "huge_dog_qb": huge_dog_qb(p),
            }
        )
    rows.sort(key=lambda r: (-r["p99"], -r["p99_per_k"]))
    return rows[:n]


def print_smash_board(rows: list[dict]) -> None:
    import sys

    print(
        "smash (sim p99 — GPP tail; not the mean ILP)",
        file=sys.stderr,
    )
    if not rows:
        print("  (no sim p99)", file=sys.stderr)
        return
    print(
        f"  {'player':<28} pos team   sal   spread  proj    p99   p99/$k  tag",
        file=sys.stderr,
    )
    for r in rows:
        tags = []
        if r.get("huge_dog_qb"):
            tags.append("dogQB")
        if r.get("dog_wr1"):
            tags.append("dogWR1")
        sp = r.get("spread")
        sp_s = "—" if sp is None else f"{sp:+g}"
        print(
            f"  {str(r.get('player') or ''):<28} {str(r.get('pos') or ''):<3} "
            f"{str(r.get('team') or ''):<5} ${int(r.get('salary') or 0):<5} "
            f"{sp_s:>7} {float(r.get('proj') or 0):6.2f} {float(r['p99']):6.2f} "
            f"{float(r['p99_per_k']):7.2f}  {','.join(tags) or '—'}",
            file=sys.stderr,
        )
