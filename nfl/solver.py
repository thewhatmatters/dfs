"""Solve a FanDuel NFL classic lineup.

Preferred: PuLP + CBC (exact ILP, 9 slots). Fallback: greedy.
Applies house `opp_dst_illegal` (QB opp DST + stud RB ≥ $7000 vs opp DST),
house max 3/team (FanDuel lobby 4), `require_qb_with_two_pass_catchers`
(2+ WR/TE from a team ⇒ that team's QB), optional `--bring-back`, and an
ILP stack premium (STACK_COEF × catcher.projection for QB+WR/TE).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field, replace

from nfl.explain import explain_player, lineup_notes
from nfl.players import Player
from nfl.rules import (
    BRING_BACK_POS,
    DIVERSITY_CHALK,
    DIVERSITY_COVERAGE,
    FANDUEL_NFL,
    FANDUEL_PICKER_ORDER,
    FanDuelNflClassic,
    MIN_UNIQUE_DEFAULT,
    PASS_STACK_POS,
    STACK_COEF,
    SkillSide,
    bring_back_illegal,
    bring_back_players,
    max_per_team_illegal,
    max_player_appearances,
    opp_dst_illegal,
    pass_catchers_by_team,
    pass_stack_players,
    stack_qb_illegal,
)

# Soft coverage (n>1, diversity=coverage): after lineup #1, subtract
# COUNT_PENALTY * prior_count from the objective per selected player.
# Unmatched Lineups RB/WR/TE get an extra hit so OurLads-only names are
# not cheap unique swaps. Mean remains the only objective for lineup #1.
COVERAGE_COUNT_PENALTY = 0.75
UNMATCHED_FILLER_PENALTY = 3.0


def is_unmatched_filler(player: Player) -> bool:
    """True for RB/WR/TE with no Lineups name join (usage stays 1.0)."""
    pos = player.position
    if pos in {"WR", "TE"}:
        return player.targets_status == "unmatched"
    if pos == "RB":
        tgt = player.targets_status == "unmatched"
        snap = player.snaps_status in {None, "unmatched"}
        return tgt and snap
    return False


@dataclass
class Lineup:
    slots: dict[str, Player]
    method: str
    notes: list[str] = field(default_factory=list)
    salary_cap: int = FANDUEL_NFL.salary_cap
    salary_floor: int = FANDUEL_NFL.salary_floor
    bring_back: int = 0
    max_per_team: int = FANDUEL_NFL.max_per_team
    require_qb_with_two_pass_catchers: bool = FANDUEL_NFL.require_qb_with_two_pass_catchers
    pool: list[Player] = field(default_factory=list, repr=False)

    @property
    def salary(self) -> int:
        return sum(p.salary for p in self.slots.values())

    @property
    def projection(self) -> float:
        return sum(p.projection for p in self.slots.values())

    @property
    def teams(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for p in self.slots.values():
            counts[p.team] += 1
        return dict(counts)

    def _player_dict(self, p: Player, *, slot_key: str | None = None) -> dict:
        row = {
            "id": p.pid,
            "name": p.name,
            "position": p.position,
            "team": p.team,
            "opponent": p.opponent,
            "game": p.game,
            "salary": p.salary,
            "projection": round(p.projection, 4),
            "fppg": p.fppg,
            "spread": p.spread,
            "total": p.total,
            "implied_total": p.implied_total,
            "implied_opp": p.implied_opp,
            "moneyline": p.moneyline,
            "depth_rank": p.depth_rank,
            "depth_source": p.depth_source,
            "target_share": p.target_share,
            "snap_share": p.snap_share,
            "targets_status": p.targets_status,
            "snaps_status": p.snaps_status,
            "prop_fd": None if p.prop_fd is None else round(p.prop_fd, 4),
            "prop_pass_yds": p.prop_pass_yds,
            "prop_pass_tds": p.prop_pass_tds,
            "prop_rush_yds": p.prop_rush_yds,
            "prop_rec_yds": p.prop_rec_yds,
            "prop_receptions": p.prop_receptions,
            "prop_book": p.prop_book,
            "injury": p.injury,
        }
        row.update(explain_player(p, slot_key=slot_key))
        return row

    def to_dict(self) -> dict:
        by_key = {
            slot: self._player_dict(p, slot_key=slot)
            for slot, p in self.slots.items()
        }
        ordered = []
        for key, label in FANDUEL_PICKER_ORDER:
            if key not in by_key:
                continue
            row = dict(by_key[key])
            row["slot"] = label
            row["slot_key"] = key
            ordered.append(row)
        notes = list(self.notes)
        notes.extend(
            lineup_notes(
                self.slots,
                bring_back=self.bring_back,
                max_per_team=self.max_per_team,
                require_qb_with_two_pass_catchers=self.require_qb_with_two_pass_catchers,
            )
        )
        return {
            "method": self.method,
            "salary": self.salary,
            "salary_floor": self.salary_floor,
            "salary_remaining": self.salary_cap - self.salary,
            "projection": round(self.projection, 4),
            "teams": self.teams,
            "slots": by_key,
            "picker": ordered,
            "notes": notes,
        }


class Infeasible(Exception):
    """No legal lineup in the filtered pool."""


def lineup_pids(lineup: Lineup) -> frozenset[str]:
    return frozenset(p.pid for p in lineup.slots.values())


def player_exposure_counts(lineups: list[Lineup]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for lu in lineups:
        for p in lu.slots.values():
            counts[p.pid] += 1
    return dict(counts)


def _coverage_penalty(player: Player, prior_count: int) -> float:
    pen = COVERAGE_COUNT_PENALTY * prior_count
    if is_unmatched_filler(player):
        pen += UNMATCHED_FILLER_PENALTY
    return pen


def _coverage_adjusted_pool(
    pool: list[Player], counts: dict[str, int]
) -> list[Player]:
    out: list[Player] = []
    for p in pool:
        pen = _coverage_penalty(p, counts.get(p.pid, 0))
        if pen:
            out.append(replace(p, objective=p.projection - pen))
        else:
            out.append(p)
    return out


def _slot_families(rules: FanDuelNflClassic) -> list[tuple[str, str, frozenset[str]]]:
    out: list[tuple[str, str, frozenset[str]]] = []
    for family, count, elig in rules.roster:
        if count == 1:
            out.append((family, family, elig))
        else:
            for i in range(1, count + 1):
                out.append((f"{family}{i}", family, elig))
    return out


def _opp_dst_chosen_illegal(
    chosen: dict[str, Player], rules: FanDuelNflClassic
) -> bool:
    dst = chosen.get("DEF")
    if dst is None:
        return False
    qb = chosen.get("QB")
    qb_opp = qb.opponent if qb is not None else None
    skill = [
        SkillSide(p.position, p.opponent, p.salary)
        for k, p in chosen.items()
        if k != "DEF"
    ]
    if rules.forbid_qb_opp_dst or rules.forbid_stud_rb_opp_dst:
        return opp_dst_illegal(
            def_team=dst.team,
            qb_opp=qb_opp if rules.forbid_qb_opp_dst else None,
            skill=skill if rules.forbid_stud_rb_opp_dst else (),
            stud_rb_min_salary=rules.stud_rb_min_salary,
        )
    return False


def _chosen_illegal(chosen: dict[str, Player], rules: FanDuelNflClassic) -> bool:
    if max_per_team_illegal(chosen, rules.max_per_team):
        return True
    if stack_qb_illegal(chosen, rules.require_qb_with_two_pass_catchers):
        return True
    if _opp_dst_chosen_illegal(chosen, rules):
        return True
    return bring_back_illegal(chosen, rules.bring_back)


def _illegal_reason(chosen: dict[str, Player], rules: FanDuelNflClassic) -> str | None:
    if max_per_team_illegal(chosen, rules.max_per_team):
        return "max_per_team_illegal"
    if stack_qb_illegal(chosen, rules.require_qb_with_two_pass_catchers):
        return "stack_qb_illegal"
    if bring_back_illegal(chosen, rules.bring_back):
        return "bring_back_illegal"
    if _opp_dst_chosen_illegal(chosen, rules):
        return "opp_dst_illegal"
    return None


def _bring_back_reachable(
    partial: dict[str, Player],
    rest: list[tuple[str, str, frozenset[str]]],
    rules: FanDuelNflClassic,
) -> bool:
    """True if a complete 9 from `partial` can still meet `--bring-back`.

    Incomplete lineups are not illegal yet: a pass stack with 0 bring-backs
    is fine while WR/TE/FLEX slots remain.
    """
    n = rules.bring_back
    if n <= 0:
        return True
    if partial.get("QB") is None:
        return True
    if not pass_stack_players(partial):
        return True
    need = n - len(bring_back_players(partial))
    if need <= 0:
        return True
    cap = sum(1 for _s, _fam, elig in rest if elig & BRING_BACK_POS)
    return cap >= need


def _stack_qb_reachable(
    partial: dict[str, Player],
    rest: list[tuple[str, str, frozenset[str]]],
    rules: FanDuelNflClassic,
) -> bool:
    """True if a complete 9 from `partial` can still meet stack-qb.

    Incomplete: 2+ WR/TE from a team with QB unfilled is fine while the QB
    slot remains. Once QB is set, 2+ catchers from another team is dead.
    """
    if not rules.require_qb_with_two_pass_catchers:
        return True
    if stack_qb_illegal(partial, True):
        return False
    qb = partial.get("QB")
    if qb is not None:
        return True
    qb_open = any(s == "QB" for s, _fam, _elig in rest)
    for _team, catchers in pass_catchers_by_team(partial).items():
        if len(catchers) >= 2 and not qb_open:
            return False
    return True


def _build_ilp(pool: list[Player], rules: FanDuelNflClassic):
    try:
        import pulp
    except ImportError as e:
        raise RuntimeError("pulp_missing") from e

    slots = _slot_families(rules)
    prob = pulp.LpProblem("fanduel_nfl", pulp.LpMaximize)
    x = {
        (i, s): pulp.LpVariable(f"x_{i}_{s}", cat="Binary")
        for i, _p in enumerate(pool)
        for s, _fam, elig in slots
        if pool[i].position in elig
    }
    if not x:
        raise Infeasible("no eligible player/slot pairs")

    def selected(i: int):
        keys = [(i, s) for (j, s) in x if j == i]
        return pulp.lpSum(x[k] for k in keys) if keys else 0

    obj = pulp.lpSum(x[i, s] * pool[i].projection for (i, s) in x)
    if STACK_COEF:
        for ji, wr in enumerate(pool):
            if wr.position not in PASS_STACK_POS:
                continue
            qb_idxs = [
                i
                for i, p in enumerate(pool)
                if p.position == "QB" and p.team == wr.team
            ]
            if not qb_idxs:
                continue
            b = pulp.LpVariable(f"stack_prem_{ji}", cat="Binary")
            prob += b <= selected(ji), f"prem_wr_{ji}"
            prob += b <= pulp.lpSum(selected(i) for i in qb_idxs), f"prem_qb_{ji}"
            obj += STACK_COEF * wr.projection * b
    prob += obj

    for s, _fam, elig in slots:
        keys = [(i, s) for i, p in enumerate(pool) if p.position in elig]
        prob += pulp.lpSum(x[k] for k in keys) == 1, f"fill_{s}"

    for i, _p in enumerate(pool):
        keys = [(i, s) for (j, s) in x if j == i]
        if keys:
            prob += pulp.lpSum(x[k] for k in keys) <= 1, f"once_{i}"

    salary_expr = pulp.lpSum(x[i, s] * pool[i].salary for (i, s) in x)
    prob += salary_expr <= rules.salary_cap, "cap"
    if rules.salary_floor > 0:
        prob += salary_expr >= rules.salary_floor, "min_salary"

    teams = sorted({p.team for p in pool})
    y = {t: pulp.LpVariable(f"team_{t}", cat="Binary") for t in teams}
    for t in teams:
        idxs = [i for i, p in enumerate(pool) if p.team == t]
        keys_t = [k for k in x if k[0] in idxs]
        if not keys_t:
            continue
        team_sum = pulp.lpSum(x[k] for k in keys_t)
        prob += team_sum <= rules.max_per_team, f"max_{t}"
        prob += team_sum >= y[t], f"team_used_{t}"
        for k in keys_t:
            prob += y[t] >= x[k], f"link_{t}_{k[0]}_{k[1]}"
    prob += pulp.lpSum(y[t] for t in teams) >= rules.min_teams, "min_teams"

    def_idxs = [i for i, p in enumerate(pool) if p.position == "D"]
    if rules.forbid_qb_opp_dst:
        for qi, qb in enumerate(pool):
            if qb.position != "QB":
                continue
            for di in def_idxs:
                if pool[di].team == qb.opponent:
                    prob += selected(qi) + selected(di) <= 1, f"qb_opp_dst_{qi}_{di}"
    if rules.forbid_stud_rb_opp_dst:
        for ri, rb in enumerate(pool):
            if rb.position != "RB" or rb.salary < rules.stud_rb_min_salary:
                continue
            for di in def_idxs:
                if pool[di].team == rb.opponent:
                    prob += selected(ri) + selected(di) <= 1, f"stud_rb_dst_{ri}_{di}"

    if rules.bring_back > 0:
        n_bb = rules.bring_back
        for qi, qb in enumerate(pool):
            if qb.position != "QB":
                continue
            mate_idxs = [
                i
                for i, p in enumerate(pool)
                if i != qi and p.position in {"WR", "TE"} and p.team == qb.team
            ]
            if not mate_idxs:
                continue
            bb_idxs = [
                i
                for i, p in enumerate(pool)
                if p.position in BRING_BACK_POS and p.team == qb.opponent
            ]
            has_mates = pulp.LpVariable(f"pass_stack_{qi}", cat="Binary")
            mate_sum = pulp.lpSum(selected(i) for i in mate_idxs)
            prob += has_mates <= mate_sum, f"stack_le_{qi}"
            prob += mate_sum <= len(mate_idxs) * has_mates, f"stack_ge_{qi}"
            need = pulp.LpVariable(f"need_bb_{qi}", cat="Binary")
            prob += need <= selected(qi), f"need_qb_{qi}"
            prob += need <= has_mates, f"need_mates_{qi}"
            prob += need >= selected(qi) + has_mates - 1, f"need_and_{qi}"
            if bb_idxs:
                prob += (
                    pulp.lpSum(selected(i) for i in bb_idxs) >= n_bb * need,
                    f"bb_{qi}",
                )
            else:
                prob += need == 0, f"bb_none_{qi}"

    if rules.require_qb_with_two_pass_catchers:
        for t in teams:
            catcher_idxs = [
                i
                for i, p in enumerate(pool)
                if p.team == t and p.position in PASS_STACK_POS
            ]
            if not catcher_idxs:
                continue
            qb_idxs = [
                i for i, p in enumerate(pool) if p.team == t and p.position == "QB"
            ]
            catcher_sum = pulp.lpSum(selected(i) for i in catcher_idxs)
            qb_sel = (
                pulp.lpSum(selected(i) for i in qb_idxs) if qb_idxs else 0
            )
            # 0 QB ⇒ at most 1 WR/TE; 1 QB ⇒ up to all catchers.
            prob += (
                catcher_sum <= 1 + (len(catcher_idxs) - 1) * qb_sel,
                f"stack_qb_{t}",
            )

    return pulp, prob, x, slots, selected


def _lineup_from_ilp(pool, rules, x, slots) -> Lineup:
    chosen: dict[str, Player] = {}
    for (i, s), var in x.items():
        if var.value() and var.value() > 0.5:
            chosen[s] = pool[i]
    if len(chosen) != len(slots):
        raise Infeasible("incomplete assignment after solve")
    why = _illegal_reason(chosen, rules)
    if why:
        raise Infeasible(f"{why} after solve")
    ordered = {s: chosen[s] for s, _fam, _e in slots}
    return Lineup(
        slots=ordered,
        method="pulp-cbc",
        salary_cap=rules.salary_cap,
        salary_floor=rules.salary_floor,
        bring_back=rules.bring_back,
        max_per_team=rules.max_per_team,
        require_qb_with_two_pass_catchers=rules.require_qb_with_two_pass_catchers,
        pool=list(pool),
    )


def solve_ilp(
    pool: list[Player],
    rules: FanDuelNflClassic = FANDUEL_NFL,
) -> Lineup:
    return solve_ilp_many(pool, rules, n_lineups=1)[0]


def solve_ilp_many(
    pool: list[Player],
    rules: FanDuelNflClassic = FANDUEL_NFL,
    *,
    n_lineups: int = 1,
    min_unique: int = MIN_UNIQUE_DEFAULT,
    max_exposure: float = 1.0,
    diversity: str = DIVERSITY_CHALK,
) -> list[Lineup]:
    """Sequential CBC. Reuses one model.

    Unique player-sets vs **each** locked 9. `--min-unique` is stacked vs
    **every** locked 9 (overlap ≤ 9 − min_unique), not only the previous
    one — cores cannot cycle forever by swapping the same two cheap seats.
    `--max-exposure` is a running count cap (all positions, including DST);
    1.0 disables. `diversity=coverage` keeps lineup #1 on the mean
    objective, then soft-penalizes high-exposure / unmatched-Lineups
    fillers. n=1 is exact CBC. n>1 uses a time/gap limit. Duplicate
    incumbents get a unique cut and retry. If a later CBC is infeasible,
    return the lineups we already have — do not raise.
    """
    if n_lineups < 1:
        raise ValueError("n_lineups must be >= 1")
    roster_n = len(_slot_families(rules))
    if min_unique < 1 or min_unique > roster_n:
        raise ValueError(f"min_unique must be 1..{roster_n}")
    max_count = max_player_appearances(max_exposure, n_lineups)
    use_coverage = diversity == DIVERSITY_COVERAGE and n_lineups > 1

    pulp, prob, x, slots, selected = _build_ilp(pool, rules)
    base_obj = prob.objective
    # n=1 stays exact. n>1: ceiling has many near-ties; do not prove 0.5%.
    if n_lineups == 1:
        solver = pulp.PULP_CBC_CMD(msg=False)
    else:
        solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=4, gapRel=0.02)
    lineups: list[Lineup] = []
    used: set[frozenset[str]] = set()
    pid_index = {p.pid: i for i, p in enumerate(pool)}
    cut_sets: set[frozenset[str]] = set()
    dup_cuts = 0
    counts: dict[str, int] = defaultdict(int)
    capped: set[int] = set()

    def _idxs(pids: frozenset[str]) -> list[int]:
        return [pid_index[pid] for pid in pids if pid in pid_index]

    def _add_unique_cut(pids: frozenset[str], name: str) -> bool:
        idxs = _idxs(pids)
        if not idxs:
            return False
        cut = pulp.lpSum(selected(i) for i in idxs) <= roster_n - 1
        prob.addConstraint(cut, name)
        cut_sets.add(pids)
        return True

    def _set_coverage_obj(*, apply: bool) -> None:
        if not apply:
            prob.setObjective(base_obj)
            return
        extra = []
        for i, p in enumerate(pool):
            pen = _coverage_penalty(p, counts.get(p.pid, 0))
            if pen:
                extra.append(pen * selected(i))
        if extra:
            prob.setObjective(base_obj - pulp.lpSum(extra))
        else:
            prob.setObjective(base_obj)

    for k in range(n_lineups):
        if use_coverage:
            _set_coverage_obj(apply=k > 0)
        while True:
            for var in x.values():
                var.varValue = None
            status = prob.solve(solver)
            label = pulp.LpStatus[status]
            try:
                lu = _lineup_from_ilp(pool, rules, x, slots)
            except Infeasible:
                if not lineups:
                    raise Infeasible(label)
                return lineups
            pids = lineup_pids(lu)
            if pids not in used:
                break
            # Stale/duplicate incumbent — unique-cut and retry this k.
            if pids in cut_sets:
                return lineups
            dup_cuts += 1
            if not _add_unique_cut(pids, f"unique_dup_{dup_cuts}"):
                return lineups
        lineups.append(lu)
        used.add(pids)
        for p in lu.slots.values():
            counts[p.pid] += 1
            if max_count is not None and counts[p.pid] >= max_count:
                i = pid_index.get(p.pid)
                if i is not None and i not in capped:
                    capped.add(i)
                    prob += selected(i) == 0, f"exposure_{i}"
        if n_lineups >= 10 and ((k + 1) % 10 == 0 or k + 1 == n_lineups):
            print(f"  solved {k + 1}/{n_lineups}", file=sys.stderr)
        if k + 1 >= n_lineups:
            break
        idxs = _idxs(pids)
        if not idxs:
            break
        # min_unique vs **this** locked 9 — keep every cut (do not replace).
        if min_unique > 1:
            cap = roster_n - min_unique
            prob += (
                pulp.lpSum(selected(i) for i in idxs) <= cap,
                f"min_unique_{k}",
            )
        else:
            _add_unique_cut(pids, f"unique_{k}")
    return lineups


def _def_ok(pl: Player, chosen: dict[str, Player], rules: FanDuelNflClassic) -> bool:
    trial = dict(chosen)
    trial["DEF"] = pl
    return not _chosen_illegal(trial, rules)


def _greedy_score(
    pl: Player,
    chosen: dict[str, Player],
    remaining: list[Player],
    slot: str,
) -> float:
    """week1_score plus house stack premium for QB+WR/TE (greedy ranking only)."""
    score = pl.projection
    if not STACK_COEF:
        return score
    qb = pl if slot == "QB" or pl.position == "QB" else chosen.get("QB")
    if pl.position in PASS_STACK_POS and qb is not None and qb.team == pl.team:
        score += STACK_COEF * pl.projection
    if slot == "QB" or pl.position == "QB":
        for p in chosen.values():
            if p.position in PASS_STACK_POS and p.team == pl.team:
                score += STACK_COEF * p.projection
        mates = sorted(
            (
                p
                for p in remaining
                if p.position in PASS_STACK_POS
                and p.team == pl.team
                and p.pid != pl.pid
            ),
            key=lambda p: p.projection,
            reverse=True,
        )[:2]
        score += STACK_COEF * sum(p.projection for p in mates)
    return score


def solve_greedy(
    pool: list[Player],
    rules: FanDuelNflClassic = FANDUEL_NFL,
) -> Lineup:
    notes = [
        "greedy fallback — not guaranteed optimal; install/use PuLP for exact"
    ]
    remaining = list(pool)
    chosen: dict[str, Player] = {}
    used_ids: set[str] = set()
    team_counts: dict[str, int] = defaultdict(int)
    slots = _slot_families(rules)

    def legal(pl: Player, slot: str) -> bool:
        if pl.pid in used_ids:
            return False
        if team_counts[pl.team] >= rules.max_per_team:
            return False
        if slot == "DEF" and not _def_ok(pl, chosen, rules):
            return False
        trial = dict(chosen)
        trial[slot] = pl
        rest = [(s, fam, elig) for s, fam, elig in slots if s not in trial]
        if rules.bring_back > 0 and not _bring_back_reachable(trial, rest, rules):
            return False
        if not _stack_qb_reachable(trial, rest, rules):
            return False
        return True

    for slot, _fam, elig in slots:
        cands = [p for p in remaining if p.position in elig and legal(p, slot)]
        if not cands:
            raise Infeasible(f"no player for {slot}")
        pick = max(
            cands,
            key=lambda p: (
                _greedy_score(p, chosen, remaining, slot),
                -p.salary,
            ),
        )
        chosen[slot] = pick
        used_ids.add(pick.pid)
        team_counts[pick.team] += 1
        remaining = [p for p in remaining if p.pid != pick.pid]

    salary = sum(p.salary for p in chosen.values())

    def swap_ok(slot: str, alt: Player, current: Player) -> bool:
        if alt.pid in used_ids:
            return False
        if team_counts[alt.team] >= rules.max_per_team and alt.team != current.team:
            return False
        trial = dict(chosen)
        trial[slot] = alt
        return not _chosen_illegal(trial, rules)

    if salary > rules.salary_cap:
        notes.append("greedy over cap; swapping down in salary")
        for slot, pl in list(chosen.items()):
            family = slot.rstrip("0123456789")
            elig = rules.eligible_positions(family)
            cheaper = [
                p
                for p in pool
                if p.position in elig and p.salary < pl.salary and swap_ok(slot, p, pl)
            ]
            cheaper.sort(key=lambda p: (-p.projection, p.salary))
            for alt in cheaper:
                delta = alt.salary - pl.salary
                if salary + delta <= rules.salary_cap:
                    used_ids.remove(pl.pid)
                    used_ids.add(alt.pid)
                    team_counts[pl.team] -= 1
                    team_counts[alt.team] += 1
                    chosen[slot] = alt
                    salary += delta
                    break
            if salary <= rules.salary_cap:
                break
    if salary > rules.salary_cap:
        raise Infeasible("greedy could not meet salary cap")

    if rules.salary_floor and salary < rules.salary_floor:
        notes.append("greedy under salary floor; swapping up")
        for slot, pl in list(chosen.items()):
            family = slot.rstrip("0123456789")
            elig = rules.eligible_positions(family)
            pricier = [
                p
                for p in pool
                if p.position in elig and p.salary > pl.salary and swap_ok(slot, p, pl)
            ]
            pricier.sort(key=lambda p: (-p.projection, -p.salary))
            for alt in pricier:
                delta = alt.salary - pl.salary
                if salary + delta <= rules.salary_cap:
                    used_ids.remove(pl.pid)
                    used_ids.add(alt.pid)
                    team_counts[pl.team] -= 1
                    team_counts[alt.team] += 1
                    chosen[slot] = alt
                    salary += delta
                    break
            if salary >= rules.salary_floor:
                break
    if rules.salary_floor and salary < rules.salary_floor:
        raise Infeasible("greedy could not meet salary floor")

    teams_used = {p.team for p in chosen.values()}
    if len(teams_used) < rules.min_teams:
        raise Infeasible("greedy min-teams")
    why = _illegal_reason(chosen, rules)
    if why:
        raise Infeasible(f"greedy {why}")

    return Lineup(
        slots=chosen,
        method="greedy",
        notes=notes,
        salary_cap=rules.salary_cap,
        salary_floor=rules.salary_floor,
        bring_back=rules.bring_back,
        max_per_team=rules.max_per_team,
        require_qb_with_two_pass_catchers=rules.require_qb_with_two_pass_catchers,
        pool=list(pool),
    )


def solve_greedy_many(
    pool: list[Player],
    rules: FanDuelNflClassic = FANDUEL_NFL,
    *,
    n_lineups: int = 1,
    min_unique: int = MIN_UNIQUE_DEFAULT,
    max_exposure: float = 1.0,
    diversity: str = DIVERSITY_CHALK,
) -> list[Lineup]:
    """Greedy 9s. Next lineup cannot use `min_unique` lowest-proj players
    from the previous 9. Exposure cap blocks players at the running
    max. Coverage downweights high-count / unmatched fillers after #1.
    Stops on infeasible / duplicate set."""
    if n_lineups < 1:
        raise ValueError("n_lineups must be >= 1")
    max_count = max_player_appearances(max_exposure, n_lineups)
    use_coverage = diversity == DIVERSITY_COVERAGE and n_lineups > 1
    lineups: list[Lineup] = []
    used: set[frozenset[str]] = set()
    blocked: set[str] = set()
    blocked_exposure: set[str] = set()
    counts: dict[str, int] = defaultdict(int)
    for k in range(n_lineups):
        skip = blocked | blocked_exposure
        trial = [p for p in pool if p.pid not in skip] if skip else list(pool)
        if use_coverage and k > 0:
            trial = _coverage_adjusted_pool(trial, counts)
        try:
            lu = solve_greedy(trial, rules)
        except Infeasible:
            if not lineups:
                raise
            break
        pids = lineup_pids(lu)
        if pids in used:
            break
        if lineups:
            prev = lineup_pids(lineups[-1])
            if len(pids - prev) < min_unique:
                break
        lineups.append(lu)
        used.add(pids)
        for p in lu.slots.values():
            counts[p.pid] += 1
            if max_count is not None and counts[p.pid] >= max_count:
                blocked_exposure.add(p.pid)
        cheapest = sorted(lu.slots.values(), key=lambda p: (p.projection, -p.salary))
        blocked = {p.pid for p in cheapest[:min_unique]}
    return lineups


def solve_many(
    pool: list[Player],
    rules: FanDuelNflClassic = FANDUEL_NFL,
    *,
    prefer_ilp: bool = True,
    n_lineups: int = 1,
    min_unique: int = MIN_UNIQUE_DEFAULT,
    max_exposure: float = 1.0,
    diversity: str = DIVERSITY_CHALK,
) -> list[Lineup]:
    if prefer_ilp:
        try:
            return solve_ilp_many(
                pool,
                rules,
                n_lineups=n_lineups,
                min_unique=min_unique,
                max_exposure=max_exposure,
                diversity=diversity,
            )
        except RuntimeError as e:
            if "pulp_missing" not in str(e):
                raise
        except Infeasible:
            raise
    return solve_greedy_many(
        pool,
        rules,
        n_lineups=n_lineups,
        min_unique=min_unique,
        max_exposure=max_exposure,
        diversity=diversity,
    )


def solve(
    pool: list[Player],
    rules: FanDuelNflClassic = FANDUEL_NFL,
    *,
    prefer_ilp: bool = True,
) -> Lineup:
    return solve_many(pool, rules, prefer_ilp=prefer_ilp, n_lineups=1)[0]
