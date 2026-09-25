# optimize-nfl-classic

**What it is:** Build the highest-projection legal FanDuel NFL classic lineup from a downloaded players-list CSV. Not NCAAF. Bare “Let’s optimize” stays NCAAF.

## What you get

- One JSON lineup (slots, salaries, projection, teams) on stdout / `--out`
- A stderr table you can paste into chat (QB, RB, RB, WR, WR, WR, TE, FLEX, DEF; last column Sources)
- Slate coverage via `--slate-status` / `python3 -m nfl.status` (JSON `slate_status`)
- An exact ILP when PuLP is installed; an approximate greedy fill otherwise (labeled)
- FanDuel upload CSV in `nfl/export/` when `--n-lineups>1`
- Multi-lineup **coverage** (default when n>1): `--max-exposure=0.60` so no player is in every 9; `--diversity=coverage` keeps lineup #1 on mean and tilts later 9s toward underused players. `--diversity=chalk --max-exposure=1 --min-unique=2` is the old “swap two seats” path

## How to run

Ask the agent to optimize NFL / FanDuel NFL classic, or from repo root:

```bash
python3 .cursor/skills/optimize-nfl-classic/scripts/preflight.py --csv nfl/data/YOUR.csv
python3 -m nfl.optimize --csv "nfl/data/YOUR.csv" --sim
```

Repo-root `scripts/optimize.py` is NCAAF — do not use it for NFL.

## What it needs

Python 3, a FanDuel NFL classic CSV, `pulp` for the exact solve, and
**`ODDS_API_KEY`** (game lines, default) and **`GANGSTASH_API_KEY`** (player props; optional `--lines-source=gangstash`, `--targets-source=gangstash`, `--snaps-source=gangstash`, `--depth-source=gangstash`). See repo `.env.example`. Cached OurLads depth, ESPN
injuries, and Gangstash props; do not `--refresh-props` unless asked.
`python3 -m nfl.depth --csv …` refreshes `nfl/data/depth.csv`.

## How it works (high level)

1. Confirm FanDuel NFL rules (9 slots, $60k, 3 teams min, house max 3 per team / FanDuel 4, DST).
2. Drop IR/NA. Join Odds lines, OurLads depth, Lineups targets + snaps, ESPN injuries, player props (cached).
3. Maximize week1_score + house QB+WR/TE stack premium under roster/cap/team/house DST / stack-qb rules. Printed Proj stays week1_score.
4. `--bring-back=N` (default 0) optionally requires opposing WR/TE vs a QB pass stack. `--stack-qb` on. `--max-per-team=4` restores lobby.
5. `--n-lineups>1` defaults to coverage, not chalk lock-in: exposure cap 0.60, min-unique 3 vs every locked 9, coverage penalty after the first mean 9. Salary / stack-qb / house DST rules stay intact.

## Where to look next

- `SKILL.md` — operating instructions the agent follows.
- `WHY.md` — design decisions and the "why".
- `references/fanduel-nfl.md` — pointer to repo rules (loaded only when checking scoring).
- `references/sources.md` — choke ids when an API/key fails (catalog in `nfl/docs/data/sources.md`).
