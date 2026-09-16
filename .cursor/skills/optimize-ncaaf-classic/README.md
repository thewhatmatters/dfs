# optimize-ncaaf-classic

**What it is:** Build the highest-projection legal FanDuel NCAA football classic lineup from a downloaded players-list CSV. Not NFL. Bare “Let’s optimize” stays NCAAF even though `optimize-nfl-classic` exists.

## What you get

- One JSON lineup (slots, salaries, projection, teams) on stdout / `--out`
- A stderr table you can paste into chat
- An exact ILP when PuLP is installed; an approximate greedy fill otherwise (labeled)

## How to run

Ask the agent to optimize a CFB/NCAAF slate, or from repo root:

```bash
python3 scripts/preflight.py --csv ncaaf/data/YOUR.csv
python3 scripts/optimize.py --csv ncaaf/data/YOUR.csv --out results/lineup.json
```

Alias (same files): `.cursor/skills/optimize-ncaaf-classic/scripts/{preflight,optimize}.py`.

Same solver as `python3 -m ncaaf.optimize`.

## What it needs

Python 3, a FanDuel NCAAF classic CSV, `pulp` for the exact solve, and **`CFBD_API_KEY`** (or `ODDS_API_KEY`). See repo `.env.example`. `--use-fppg` skips the key and is prior-season, not this slate.

## How it works (high level)

1. Confirm FanDuel CFB rules (7 slots, $60k, 3 teams min, 4 max per team).
2. Drop Outs (`O`). Empty FPPG is fine on the default path.
3. Fetch CFBD `/lines` (or Odds API); implied totals from spread + total.
4. Maximize implied team total (+ leftover/value) under the roster/cap/team rules.

## Where to look next

- `SKILL.md` — operating instructions the agent follows.
- `WHY.md` — design decisions and the "why".
- `references/fanduel-ncaaf.md` — pointer to repo rules (loaded only when checking scoring).
- `references/sources.md` — choke ids when a scrape/API/key fails (catalog in `ncaaf/docs/data/sources.md`).
- Repo `ncaaf/docs/data/game-script.md` — weekly game-script knobs (grind vs blowout).
