# optimize-nfl-classic

**What it is:** Build the highest-projection legal FanDuel NFL classic lineup from a downloaded players-list CSV. Not NCAAF. Bare “Let’s optimize” stays NCAAF.

## What you get

- One JSON lineup (slots, salaries, projection, teams) on stdout / `--out`
- A stderr table you can paste into chat (QB, RB, RB, WR, WR, WR, TE, FLEX, DEF)
- An exact ILP when PuLP is installed; an approximate greedy fill otherwise (labeled)
- FanDuel upload CSV in `nfl/export/` when `--n-lineups>1`

## How to run

Ask the agent to optimize NFL / FanDuel NFL classic, or from repo root:

```bash
python3 .cursor/skills/optimize-nfl-classic/scripts/preflight.py --csv nfl/data/YOUR.csv
python3 -m nfl.optimize --csv "nfl/data/YOUR.csv" --sim
```

Repo-root `scripts/optimize.py` is NCAAF — do not use it for NFL.

## What it needs

Python 3, a FanDuel NFL classic CSV, `pulp` for the exact solve, and
**`ODDS_API_KEY`**. See repo `.env.example`. Cached ESPN injuries/depth and
Odds props; do not `--refresh-props` unless asked.

## How it works (high level)

1. Confirm FanDuel NFL rules (9 slots, $60k, 3 teams min, house max 3 per team / FanDuel 4, DST).
2. Drop IR/NA. Join Odds lines, ESPN injuries/depth, player props (cached).
3. Maximize week1_score + house QB+WR/TE stack premium under roster/cap/team/house DST / stack-qb rules. Printed Proj stays week1_score.
4. `--bring-back=N` (default 0) optionally requires opposing WR/TE vs a QB pass stack. `--stack-qb` on. `--max-per-team=4` restores lobby.

## Where to look next

- `SKILL.md` — operating instructions the agent follows.
- `WHY.md` — design decisions and the "why".
- `references/fanduel-nfl.md` — pointer to repo rules (loaded only when checking scoring).
- `references/sources.md` — choke ids when an API/key fails (catalog in `nfl/docs/data/sources.md`).
