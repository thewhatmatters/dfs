"""Stdin grill before solve() on the interactive optimize path.

`--agent` skips this. Empty answers accept the printed recommend.
Non-TTY without `--agent` prints the questions + recommends and applies them.

Lean is a modest objective bump on players in selected games so the rest of
the slate stays in the pool. It is not a retune of ncaaf/script.py sit knobs.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, replace
from typing import Iterable, TextIO

from ncaaf.lines import TeamLine
from ncaaf.ourlads import match_key
from ncaaf.players import Player
from ncaaf.projections import score_player

# Modest overweight for Q2=lean. Keep this well below blowout sit-band moves
# (those live in ncaaf/script.py and ncaaf/docs/data/game-script.md). 1.08
# prefers the chosen games without emptying the slate or dominating props.
LEAN_MULT = 1.08
# A game is "top implied" when either side's implied total is >= this.
TOP_IMPLIED_CUTOFF = 30.0

_NONE = frozenset({"none", "n", "-", "no"})


class InterviewError(ValueError):
    """Bad interview answer or lock that cannot be applied."""


@dataclass(frozen=True)
class InterviewAnswers:
    games_mode: str  # all | top | pick
    selected_games: tuple[str, ...]
    exclusive: bool
    apply_sit: bool
    superflex: str  # qb | any
    lock_names: tuple[str, ...]
    fade_names: tuple[str, ...]

    @staticmethod
    def defaults() -> InterviewAnswers:
        return InterviewAnswers(
            games_mode="all",
            selected_games=(),
            exclusive=False,
            apply_sit=True,
            superflex="qb",
            lock_names=(),
            fade_names=(),
        )

    def to_dict(self) -> dict:
        cut = None
        if self.games_mode != "all":
            cut = "exclusive" if self.exclusive else "lean"
        return {
            "games": self.games_mode,
            "selected": list(self.selected_games),
            "cut": cut,
            "sit": "keep" if self.apply_sit else "ignore",
            "superflex": self.superflex,
            "locks": list(self.lock_names),
            "fades": list(self.fade_names),
            "lean_mult": LEAN_MULT,
            "top_cutoff": TOP_IMPLIED_CUTOFF,
        }


def numbered_games(games: Iterable[TeamLine]) -> list[TeamLine]:
    """Highest max-implied first so 'top N' and pick #1 are the shootouts."""
    return sorted(
        games,
        key=lambda g: (-max(g.implied_away, g.implied_home), g.game),
    )


def is_top_implied(game: TeamLine, cutoff: float = TOP_IMPLIED_CUTOFF) -> bool:
    return max(game.implied_away, game.implied_home) >= cutoff


def format_games_table(
    games: list[TeamLine],
    *,
    cutoff: float = TOP_IMPLIED_CUTOFF,
) -> str:
    if not games:
        return "no games on this run"
    header = "#  game            spread(home)     total  implied_away  implied_home"
    lines = [header]
    n_top = 0
    for i, g in enumerate(games, 1):
        top = is_top_implied(g, cutoff)
        n_top += int(top)
        home_s = f"{g.home_fd} {g.home_spread:+g}"
        mark = "  *" if top else ""
        lines.append(
            f"{i:>2} {g.game:<15} {home_s:<16} {g.total:<6g} "
            f"{g.away_fd} {g.implied_away:<8g} {g.home_fd} {g.implied_home:g}{mark}"
        )
    lines.append(
        f"top implied = either side ≥ {cutoff:g}  "
        f"({n_top}/{len(games)} games marked *)"
    )
    return "\n".join(lines)


def parse_games_choice(
    raw: str,
    numbered: list[TeamLine],
    *,
    cutoff: float = TOP_IMPLIED_CUTOFF,
) -> tuple[str, tuple[str, ...]]:
    """Return (mode, selected game ids). Empty → all."""
    s = (raw or "").strip()
    if not s or s.casefold() in {"all", "a", "all games"}:
        return "all", tuple(g.game for g in numbered)

    low = s.casefold()
    if low.startswith("top"):
        rest = low[3:].strip().lstrip("=:").strip()
        if rest in {"", "implied", "implied totals", "implied total"}:
            sel = [g for g in numbered if is_top_implied(g, cutoff)]
            return "top", tuple(g.game for g in sel)
        try:
            n = float(rest)
        except ValueError as e:
            raise InterviewError(f"could not parse games choice: {raw!r}") from e
        if n == int(n) and 1 <= int(n) <= len(numbered):
            return "top", tuple(g.game for g in numbered[: int(n)])
        sel = [g for g in numbered if is_top_implied(g, n)]
        return "top", tuple(g.game for g in sel)

    parts = [p for p in re.split(r"[,\s]+", s) if p]
    if parts and parts[0].casefold() in {"pick", "p"}:
        parts = parts[1:]
    selected: list[TeamLine] = []
    seen: set[str] = set()
    for part in parts:
        hit: TeamLine | None = None
        if part.isdigit():
            i = int(part)
            if i < 1 or i > len(numbered):
                raise InterviewError(f"game #{i} is not in 1..{len(numbered)}")
            hit = numbered[i - 1]
        else:
            up = part.upper()
            for g in numbered:
                if g.game.upper() == up or up in {g.home_fd, g.away_fd}:
                    hit = g
                    break
        if hit is None:
            raise InterviewError(f"unknown game {part!r}")
        if hit.game not in seen:
            selected.append(hit)
            seen.add(hit.game)
    if not selected:
        raise InterviewError(f"could not parse games choice: {raw!r}")
    return "pick", tuple(g.game for g in selected)


def parse_cut_choice(raw: str) -> bool:
    """True = exclusive (drop other games). Empty → lean (False)."""
    s = (raw or "").strip().casefold()
    if not s or s in {"lean", "l", "overweight", "keep pool", "keep"}:
        return False
    if s in {"exclusive", "e", "cut", "drop", "only"}:
        return True
    raise InterviewError(f"cut must be exclusive or lean, not {raw!r}")


def parse_sit_choice(raw: str) -> bool:
    """True = keep script_mult. Empty → keep."""
    s = (raw or "").strip().casefold()
    if not s or s in {"keep", "k", "yes", "on"}:
        return True
    if s in {"ignore", "i", "skip", "off", "no"}:
        return False
    raise InterviewError(f"sit must be keep or ignore, not {raw!r}")


def parse_superflex_choice(raw: str) -> str:
    s = (raw or "").strip().casefold()
    if not s or s in {"qb", "q", "second qb", "2qb"}:
        return "qb"
    if s in {"any", "a", "skill", "flex"}:
        return "any"
    raise InterviewError(f"superflex must be qb or any, not {raw!r}")


def parse_lock_fade(raw: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Locks / fades from a single answer. Empty or 'none' → both empty.

    Forms: 'none' | 'lock A, B fade C' | 'fade C' | 'A, B' (locks).
    """
    s = (raw or "").strip()
    if not s or s.casefold() in _NONE:
        return (), ()
    fade_m = re.search(r"\bfades?\b", s, re.I)
    lock_m = re.search(r"\blocks?\b", s, re.I)
    fade_raw = ""
    lock_raw = s
    if fade_m and lock_m:
        if fade_m.start() < lock_m.start():
            fade_raw = s[fade_m.end() : lock_m.start()]
            lock_raw = s[lock_m.end() :]
        else:
            lock_raw = s[lock_m.end() : fade_m.start()]
            fade_raw = s[fade_m.end() :]
    elif fade_m:
        fade_raw = s[fade_m.end() :]
        lock_raw = s[: fade_m.start()]
    elif lock_m:
        lock_raw = s[lock_m.end() :]
        fade_raw = ""
    locks = _split_names(lock_raw)
    fades = _split_names(fade_raw)
    return tuple(locks), tuple(fades)


def _split_names(raw: str) -> list[str]:
    s = (raw or "").strip().strip(":").strip()
    if not s or s.casefold() in _NONE:
        return []
    parts = re.split(r"\s*(?:,|;|\band\b)\s*", s, flags=re.I)
    return [p.strip() for p in parts if p.strip() and p.strip().casefold() not in _NONE]


def parse_answers(
    *,
    games_raw: str = "",
    cut_raw: str = "",
    sit_raw: str = "",
    superflex_raw: str = "",
    lock_fade_raw: str = "",
    numbered: list[TeamLine] | None = None,
) -> InterviewAnswers:
    """Build answers from raw strings; empty fields take recommends."""
    numbered = numbered or []
    mode, selected = parse_games_choice(games_raw, numbered)
    exclusive = False
    if mode != "all":
        exclusive = parse_cut_choice(cut_raw)
    locks, fades = parse_lock_fade(lock_fade_raw)
    return InterviewAnswers(
        games_mode=mode,
        selected_games=selected if mode != "all" else (),
        exclusive=exclusive,
        apply_sit=parse_sit_choice(sit_raw),
        superflex=parse_superflex_choice(superflex_raw),
        lock_names=locks,
        fade_names=fades,
    )


def _split_flag_names(items: Iterable[str] | None) -> tuple[str, ...]:
    out: list[str] = []
    for item in items or ():
        out.extend(_split_names(item))
    return tuple(out)


def answers_from_flags(args, games: list[TeamLine]) -> InterviewAnswers:
    numbered = numbered_games(games)
    raw_games = getattr(args, "games", None) or "all"
    mode, selected = parse_games_choice(raw_games, numbered)
    exclusive = False
    if mode != "all":
        exclusive = (getattr(args, "cut", "lean") or "lean") == "exclusive"
    sit = getattr(args, "sit", "keep") or "keep"
    return InterviewAnswers(
        games_mode=mode,
        selected_games=selected if mode != "all" else (),
        exclusive=exclusive,
        apply_sit=sit != "ignore",
        superflex=getattr(args, "superflex", "qb") or "qb",
        lock_names=_split_flag_names(getattr(args, "lock", None)),
        fade_names=_split_flag_names(getattr(args, "fade", None)),
    )


def _ask(prompt: str, default: str, infile: TextIO, outfile: TextIO) -> str:
    print(f"{prompt}  [{default}]: ", end="", file=outfile, flush=True)
    line = infile.readline()
    if line == "":
        print(file=outfile)
        return default
    s = line.strip()
    return s if s else default


def _print_defaults(outfile: TextIO, numbered: list[TeamLine]) -> None:
    n_top = sum(1 for g in numbered if is_top_implied(g))
    extra = f" (either side ≥ {TOP_IMPLIED_CUTOFF:g} → {n_top}/{len(numbered)})" if numbered else ""
    print("interview — empty input accepts [recommend]", file=outfile)
    print(f"  Q1 games: all  [recommend]{extra}", file=outfile)
    print("  Q2 skipped (all games)", file=outfile)
    print("  Q3 blowout sit ≤ −21: keep  [recommend]", file=outfile)
    print("  Q4 SuperFLEX: qb  [recommend]", file=outfile)
    print("  Q5 locks/fades: none  [recommend]", file=outfile)


def run_interview(
    games: list[TeamLine],
    *,
    use_defaults: bool = False,
    infile: TextIO | None = None,
    outfile: TextIO | None = None,
) -> InterviewAnswers:
    infile = infile if infile is not None else sys.stdin
    outfile = outfile if outfile is not None else sys.stderr
    numbered = numbered_games(games)
    print(format_games_table(numbered), file=outfile)
    if use_defaults:
        _print_defaults(outfile, numbered)
        return parse_answers(numbered=numbered)

    n_top = sum(1 for g in numbered if is_top_implied(g))
    q1 = (
        f"Q1  Games: all / top implied (either side ≥ {TOP_IMPLIED_CUTOFF:g}"
        f" → {n_top}/{len(numbered) or 0}) / pick #,#,#"
    )
    games_raw = _ask(q1, "all", infile, outfile)
    mode, _selected = parse_games_choice(games_raw, numbered)
    cut_raw = ""
    if mode != "all":
        cut_raw = _ask(
            f"Q2  Selected games: exclusive cut (drop the rest) / "
            f"lean (×{LEAN_MULT:g}, keep pool)",
            "lean",
            infile,
            outfile,
        )
    sit_raw = _ask("Q3  Blowout sit (favorite ≤ −21): keep / ignore", "keep", infile, outfile)
    sflx_raw = _ask("Q4  SuperFLEX: qb / any", "qb", infile, outfile)
    lf_raw = _ask("Q5  Locks / fades (names, or none)", "none", infile, outfile)
    return parse_answers(
        games_raw=games_raw,
        cut_raw=cut_raw,
        sit_raw=sit_raw,
        superflex_raw=sflx_raw,
        lock_fade_raw=lf_raw,
        numbered=numbered,
    )


def selected_teams(
    games: list[TeamLine],
    selected_ids: tuple[str, ...],
) -> set[str]:
    want = {g.upper() for g in selected_ids}
    teams: set[str] = set()
    for g in games:
        if g.game.upper() in want:
            teams.add(g.home_fd)
            teams.add(g.away_fd)
    return teams


def resolve_names(
    names: tuple[str, ...],
    pool: list[Player],
) -> tuple[list[Player], list[str]]:
    """Match lock/fade strings to pool players. Last token may be a team."""
    hits: list[Player] = []
    missing: list[str] = []
    seen: set[str] = set()

    def take(found: list[Player]) -> bool:
        if len(found) != 1:
            return False
        pl = found[0]
        if pl.pid not in seen:
            hits.append(pl)
            seen.add(pl.pid)
        return True

    for raw in names:
        toks = (raw or "").strip().split()
        team = None
        name_s = raw
        if toks and toks[-1].isupper() and 2 <= len(toks[-1]) <= 5:
            team = toks[-1].upper()
            name_s = " ".join(toks[:-1]).strip() or raw
        mk = match_key(name_s)
        if not mk:
            missing.append(raw)
            continue
        cands = pool if team is None else [p for p in pool if p.team == team]
        exact = [p for p in cands if match_key(p.name) == mk]
        if take(exact):
            continue
        last = mk.split()[-1]
        lasts = [p for p in cands if match_key(p.name).split()[-1:] == [last]]
        if take(lasts):
            continue
        missing.append(raw)
    return hits, missing


def apply_interview(
    pool: list[Player],
    games: list[TeamLine],
    answers: InterviewAnswers,
) -> tuple[list[Player], frozenset[str]]:
    """Filter / reweight the pool. Returns (pool, lock pids).

    exclusive: keep only players whose team is in a selected game.
    lean: multiply objective by LEAN_MULT for those players; do not drop.
    ignore-sit: recompute week-1 score with script_mult forced to 1.0.
    """
    teams: set[str] | None = None
    if answers.games_mode != "all" and answers.selected_games:
        teams = selected_teams(games, answers.selected_games)
        if not teams:
            raise InterviewError(
                f"selected games {list(answers.selected_games)} matched no slate teams"
            )

    out: list[Player] = []
    for pl in pool:
        in_sel = teams is None or pl.team in teams
        if teams is not None and answers.exclusive and not in_sel:
            continue
        lean = (
            LEAN_MULT
            if teams is not None and not answers.exclusive and in_sel
            else 1.0
        )
        obj = pl.objective
        script_applied = pl.script_applied
        if not answers.apply_sit:
            script_applied = False
            if pl.implied_total is not None:
                obj = score_player(pl, apply_script=False)
        if lean != 1.0 and obj is not None:
            obj = obj * lean
        if obj != pl.objective or script_applied != pl.script_applied:
            out.append(replace(pl, objective=obj, script_applied=script_applied))
        else:
            out.append(pl)

    fade_hits, fade_miss = resolve_names(answers.fade_names, out)
    if fade_miss:
        print(
            f"interview fade unmatched: {', '.join(fade_miss)}",
            file=sys.stderr,
        )
    fade_ids = {p.pid for p in fade_hits}
    if fade_ids:
        out = [p for p in out if p.pid not in fade_ids]

    lock_hits, lock_miss = resolve_names(answers.lock_names, out)
    if lock_miss:
        raise InterviewError(
            "lock not in pool: " + ", ".join(lock_miss)
        )
    if answers.exclusive and not out:
        raise InterviewError("exclusive cut left no players")
    return out, frozenset(p.pid for p in lock_hits)
