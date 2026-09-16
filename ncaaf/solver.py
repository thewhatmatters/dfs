"""Solve a FanDuel NCAAF classic lineup.

Preferred adapter: PuLP + CBC (exact ILP). Fallback: greedy fill by
points-per-dollar then a local swap — approximate; callers must disclose.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ncaaf.explain import explain_player, lineup_script_notes
from ncaaf.leverage import add_ilp_constraints
from ncaaf.players import Player
from ncaaf.rules import FANDUEL_NCAAF, FANDUEL_PICKER_ORDER, FanDuelNcaafClassic


@dataclass
class Lineup:
    slots: dict[str, Player]
    method: str
    notes: list[str] = field(default_factory=list)
    salary_cap: int = FANDUEL_NCAAF.salary_cap
    salary_floor: int = FANDUEL_NCAAF.salary_floor
    # Full slate pool (for team-level prop comparisons in notes). Not serialized.
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
        lineup_ps = list(self.slots.values())
        pool = self.pool or lineup_ps
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
            "prop_fd": None if p.prop_fd is None else round(p.prop_fd, 4),
            "prop_pass_yds": p.prop_pass_yds,
            "prop_pass_tds": p.prop_pass_tds,
            "prop_rush_yds": p.prop_rush_yds,
            "prop_rec_yds": p.prop_rec_yds,
            "prop_receptions": p.prop_receptions,
            "prop_book": p.prop_book,
            "pass_rate": p.pass_rate,
            "opp_pass_rate": p.opp_pass_rate,
            "opp_rush_ppa": p.opp_rush_ppa,
            "opp_pass_ppa": p.opp_pass_ppa,
            "injury": p.injury,
            "ownership": p.ownership,
            "weather": p.weather,
            "rush_share": p.rush_share,
            "target_share": p.target_share,
        }
        row.update(
            explain_player(p, pool=pool, lineup=lineup_ps, slot_key=slot_key)
        )
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
            lineup_script_notes(
                list(self.slots.values()),
                pool=self.pool or list(self.slots.values()),
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


def _slot_families(rules: FanDuelNcaafClassic) -> list[tuple[str, str, frozenset[str]]]:
    """Return (slot_name, family, eligible positions) for each concrete slot."""
    out: list[tuple[str, str, frozenset[str]]] = []
    for family, count, elig in rules.roster:
        if family == "SUPERFLEX":
            elig = rules.superflex_eligible
        if count == 1:
            out.append((family, family, elig))
        else:
            for i in range(1, count + 1):
                out.append((f"{family}{i}", family, elig))
    return out


def solve_ilp(
    pool: list[Player],
    rules: FanDuelNcaafClassic = FANDUEL_NCAAF,
    *,
    lock_ids: frozenset[str] = frozenset(),
    leverage: bool = False,
) -> Lineup:
    try:
        import pulp
    except ImportError as e:
        raise RuntimeError("pulp_missing") from e

    slots = _slot_families(rules)
    prob = pulp.LpProblem("fanduel_ncaaf", pulp.LpMaximize)
    x = {
        (i, s): pulp.LpVariable(f"x_{i}_{s}", cat="Binary")
        for i, _p in enumerate(pool)
        for s, _fam, elig in slots
        if pool[i].position in elig
    }
    if not x:
        raise Infeasible("no eligible player/slot pairs")

    prob += pulp.lpSum(x[i, s] * pool[i].projection for (i, s) in x)

    for s, _fam, elig in slots:
        keys = [(i, s) for i, p in enumerate(pool) if p.position in elig]
        prob += pulp.lpSum(x[k] for k in keys) == 1, f"fill_{s}"

    for i, _p in enumerate(pool):
        keys = [(i, s) for (j, s) in x if j == i]
        if keys:
            prob += pulp.lpSum(x[k] for k in keys) <= 1, f"once_{i}"

    if lock_ids:
        found: set[str] = set()
        for i, p in enumerate(pool):
            if p.pid not in lock_ids:
                continue
            found.add(p.pid)
            keys = [(i, s) for (j, s) in x if j == i]
            if not keys:
                raise Infeasible(f"locked {p.name} has no eligible slot")
            prob += pulp.lpSum(x[k] for k in keys) == 1, f"lock_{i}"
        missing = lock_ids - found
        if missing:
            raise Infeasible(f"locked ids not in pool: {sorted(missing)}")

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
        # y=1 iff at least one player from t (both directions).
        prob += team_sum >= y[t], f"team_used_{t}"
        for k in keys_t:
            prob += y[t] >= x[k], f"link_{t}_{k[0]}_{k[1]}"
    prob += pulp.lpSum(y[t] for t in teams) >= rules.min_teams, "min_teams"

    lev_notes: list[str] = []
    if leverage:
        lev_notes = add_ilp_constraints(prob, x, pool)

    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        raise Infeasible(pulp.LpStatus[status])

    chosen: dict[str, Player] = {}
    for (i, s), var in x.items():
        if var.value() and var.value() > 0.5:
            chosen[s] = pool[i]
    if len(chosen) != len(slots):
        raise Infeasible("incomplete assignment after solve")
    ordered = {s: chosen[s] for s, _fam, _e in slots}
    return Lineup(
        slots=ordered,
        method="pulp-cbc",
        notes=list(lev_notes),
        salary_cap=rules.salary_cap,
        salary_floor=rules.salary_floor,
        pool=list(pool),
    )


def solve_greedy(
    pool: list[Player],
    rules: FanDuelNcaafClassic = FANDUEL_NCAAF,
    *,
    lock_ids: frozenset[str] = frozenset(),
) -> Lineup:
    """Approximate fill: highest projection per remaining slot, then salary repair."""
    notes = [
        "greedy fallback — not guaranteed optimal; install/use PuLP for exact"
    ]
    remaining = list(pool)
    chosen: dict[str, Player] = {}
    used_ids: set[str] = set()
    team_counts: dict[str, int] = defaultdict(int)
    slots = _slot_families(rules)

    if lock_ids:
        found = {p.pid for p in pool if p.pid in lock_ids}
        missing = lock_ids - found
        if missing:
            raise Infeasible(f"locked ids not in pool: {sorted(missing)}")
        for pl in [p for p in pool if p.pid in lock_ids]:
            placed = False
            for slot, _fam, elig in slots:
                if slot in chosen or pl.position not in elig:
                    continue
                if team_counts[pl.team] >= rules.max_per_team:
                    raise Infeasible(f"locked {pl.name} exceeds max per team")
                chosen[slot] = pl
                used_ids.add(pl.pid)
                team_counts[pl.team] += 1
                remaining = [p for p in remaining if p.pid != pl.pid]
                placed = True
                break
            if not placed:
                raise Infeasible(f"no slot for locked {pl.name}")

    def legal(pl: Player) -> bool:
        if pl.pid in used_ids:
            return False
        if team_counts[pl.team] >= rules.max_per_team:
            return False
        return True

    for slot, _fam, elig in slots:
        if slot in chosen:
            continue
        cands = [p for p in remaining if p.position in elig and legal(p)]
        if not cands:
            raise Infeasible(f"no player for {slot}")
        pick = max(cands, key=lambda p: (p.projection, -p.salary))
        chosen[slot] = pick
        used_ids.add(pick.pid)
        team_counts[pick.team] += 1
        remaining = [p for p in remaining if p.pid != pick.pid]

    salary = sum(p.salary for p in chosen.values())
    # Cheap swaps if over cap.
    if salary > rules.salary_cap:
        notes.append("greedy over cap; swapping down in salary")
        for slot, pl in list(chosen.items()):
            if pl.pid in lock_ids:
                continue
            family = slot.rstrip("0123456789")
            elig = rules.eligible_positions(
                "SUPERFLEX" if family == "SUPERFLEX" else family
            )
            cheaper = [
                p
                for p in pool
                if p.position in elig
                and p.pid not in used_ids
                and p.salary < pl.salary
                and (team_counts[p.team] < rules.max_per_team or p.team == pl.team)
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
            if pl.pid in lock_ids:
                continue
            family = slot.rstrip("0123456789")
            elig = rules.eligible_positions(
                "SUPERFLEX" if family == "SUPERFLEX" else family
            )
            pricier = [
                p
                for p in pool
                if p.position in elig
                and p.pid not in used_ids
                and p.salary > pl.salary
                and (team_counts[p.team] < rules.max_per_team or p.team == pl.team)
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
        notes.append("greedy under min unique teams; not repaired")
        raise Infeasible("greedy min-teams")

    return Lineup(
        slots=chosen,
        method="greedy",
        notes=notes,
        salary_cap=rules.salary_cap,
        salary_floor=rules.salary_floor,
        pool=list(pool),
    )


def solve(
    pool: list[Player],
    rules: FanDuelNcaafClassic = FANDUEL_NCAAF,
    *,
    prefer_ilp: bool = True,
    lock_ids: frozenset[str] = frozenset(),
    leverage: bool = False,
) -> Lineup:
    if prefer_ilp:
        try:
            return solve_ilp(
                pool, rules, lock_ids=lock_ids, leverage=leverage
            )
        except RuntimeError as e:
            if "pulp_missing" not in str(e):
                raise
        except Infeasible:
            raise
    greedy = solve_greedy(pool, rules, lock_ids=lock_ids)
    if leverage:
        greedy.notes.append("leverage skipped on greedy — PuLP path only")
    return greedy
