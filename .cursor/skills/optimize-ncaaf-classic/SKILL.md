---
name: optimize-ncaaf-classic
description: >-
  Optimize a FanDuel NCAA football (NCAAF) classic DFS lineup from a players-list
  CSV: salary cap, QB/2 RB/3 WR-or-TE/SuperFLEX, min 3 teams, max 4 per team.
  FanDuel NCAA football classic only — not NFL. Use when the user asks to
  optimize a CFB/NCAAF slate, "Let's optimize", "Let's optimize NCAA football",
  "optimize NCAAF", "initialize the optimal lineup builder", "optimize today's
  slate", "optimize this FanDuel CFB CSV", "best NCAAF lineup", or
  "/optimize-ncaaf-classic". "Let's optimize" is NCAAF even though
  optimize-nfl-classic exists — do not ask NCAAF or NFL.
---

# optimize-ncaaf-classic

Solve the highest-projection legal FanDuel NCAAF classic lineup for a slate CSV.

## What it does

Loads contest rules from this repo, filters the FanDuel export, joins Vegas
lines, and runs the PuLP ILP (greedy if PuLP is missing). Default objective is
**implied team total** (spread + total), not CSV FPPG.

## How to run

Triggers (any of these start the builder): “Let’s optimize”, “Let’s optimize NCAA football”, “optimize NCAAF”, “initialize the optimal lineup builder”, “optimize today’s slate”, `/optimize-ncaaf-classic` (optional `--csv=PATH`). **“Let’s optimize” = NCAAF** even though `optimize-nfl-classic` exists. Do not ask “NCAAF or NFL?”. NFL is an explicit trigger (“optimize NFL”, `/optimize-nfl-classic`).

**No question card** (NCAAF and NFL). Agents: `--agent --interview-defaults`. Defaults: all games / keep sit / SuperFLEX qb / no locks. Optional: one line “using interview defaults.”

Crunch first (preflight, lines, OurLads, props — cached; no `--refresh-props` / `--refresh-depth` unless asked). Solve immediately with `python3 -m ncaaf.optimize --csv … --agent --interview-defaults --board --sim` (**mean** ILP + leverage seats, print p10/p99). Newest FanDuel CSV in `ncaaf/data/`. Unless the user already gave `--games` / `--cut` / `--sit` / `--superflex` / `--lock` / `--fade` / `--objective`. Cash / floor → `--objective=floor`. GPP / ceiling → `--objective=ceiling`. Exclusive one game on a 3-gamer is still infeasible (min 3 teams). User-facing report is the **stderr picker table** (paste verbatim in a ` ``` ` fence) — not a paraphrased 7, not JSON. Then backtest if `ncaaf/data/perfect/<contest>.json` exists.

CLI stdin interview (`ncaaf/interview.py`) is for `python3 -m ncaaf.optimize` without `--agent` (real TTY) — not a question card. Do not add a Python curses/inquirer TUI. `--interview-defaults` applies the five recommends even on a TTY. Non-TTY without `--agent` prints the questions + recommends and applies them.

## Flags

| Flag | Meaning |
|------|---------|
| `--agent` | non-interactive; no prompts/pauses (spec A7b); skips the grill |
| `--interview-defaults` | apply Q1–Q5 recommends even on a TTY (all / keep sit / qb / none) |
| `--games=all\|top\|top:N\|GAME,GAME` | when the user already named games; with `--agent` |
| `--cut=exclusive\|lean` | exclusive = drop other games; lean = ×1.08, keep pool |
| `--sit=keep\|ignore` | ignore = `script_mult` 1.0 this run only |
| `--lock=NAME` / `--fade=NAME` | repeatable; smallest lock/exclude on the pool |
| `--out=PATH` | write JSON artifact |
| `--csv=PATH` | FanDuel players-list CSV (required) |
| `--use-fppg` | opt-in: CSV FPPG (**prior season, not this slate**) |
| `--include-unprojected` | FPPG mode only: keep empty-FPPG rows (0 pts) |
| `--lines-json=PATH` | replay CFBD / Odds / simple-games JSON |
| `--lines-source=auto\|cfbd\|odds` | live source (needs `CFBD_API_KEY` or `ODDS_API_KEY`) |
| `--skip-depth` | skip OurLads depth join (unlisted prior) |
| `--refresh-depth` | refetch OurLads HTML (slate teams only) |
| `--skip-props` | skip Odds API player props |
| `--refresh-props` | refetch props (burns ~70 credits) |
| `--exclude-questionable` | drop Q/D; `O` is dropped by default |
| `--greedy` | skip PuLP even if installed |
| `--superflex=qb\|any` | SuperFLEX as second QB (default) or any skill |
| `--board` | print pool projections on stderr (depth 1–2 + anyone with `prop_fd`); JSON `board`. Point estimate (implied×depth×share×script, ±20% prop tilt) — not a sim |
| `--board=all` | same board, full pool |
| `--sim` | Game Monte Carlo (default 10000; `--sim=0` off). One Vegas total+spread world per game; teammates share it. Not a PBP copula. Mean ILP stays `week1_score`. Lineup Fl/Cl are the joint 7. `floor`/`ceiling` run this even if `--sim` is omitted |
| `--sim-seed=1` | RNG seed for `--sim` (default 1; use in tests) |
| `--objective=mean\|floor\|ceiling` | ILP score. **Default `mean`** = `week1_score` (not sim p50). `floor` = sim **p10**. `ceiling` = sim **p99**. Not historical FPPG max. `--use-fppg` only with mean |
| `--leverage=on\|off` | Default **on** (mean/ceiling). ≥1 +14 WR1/TE1 and ≥1 +21 dog QB when those sets exist in the pool. `off` = unconstrained implied mean. Floor is always off. |

## Step 0 — Mode probe

Probe `python3` and `scripts/`. **SCRIPTS** if both exist; else **NATIVE** (read `ncaaf/docs/sites/fanduel-ncaaf.md` and reason a lineup by hand — disclose it is not ILP).

## Steps

Commands from repo root: `python3 scripts/preflight.py` and `python3 scripts/optimize.py`. Implementations live under `.cursor/skills/optimize-ncaaf-classic/scripts/`.

1. **Rules** — read `ncaaf/docs/sites/fanduel-ncaaf.md` if anything looks off vs the CSV (`Roster Position`, salaries).
2. **Preflight** — `python3 scripts/preflight.py --csv=<path>`. `down` stops. If CSV Team/Opponent abbrevs are unmapped vs `ncaaf.teams.TEAMS`, **stop** (`LINES_JOIN`) and name them — do not run optimize. `gated` `SOLVER_PULP`: interactive A7c (*Fix it for me* `python3 -m pip install pulp` / *I'll do it myself* / *Skip* → greedy). `--agent`: Skip → greedy, say so. If overall is not `ready`, name the **`choke` id** and open [`references/sources.md`](references/sources.md) (catalog: `ncaaf/docs/data/sources.md`). Do not dump the catalog on a green run.
3. **Solve** — Grok TUI: `python3 -m ncaaf.optimize --csv=<path> --agent --interview-defaults --board --sim [--out=PATH]` unless the user already gave `--games` / `--cut` / `--sit` / `--superflex` / `--lock` / `--fade` / `--objective`. Cash/floor → `--objective=floor`. GPP/ceiling → `--objective=ceiling`. Same flags via `python3 scripts/optimize.py`. JSON stdout. Default needs `CFBD_API_KEY` or `ODDS_API_KEY`. Gate `LINES_KEY` is a **hard stop** (print install instructions from stderr; do **not** pass `--use-fppg` unless the user asked for prior-season FPPG). Ingest failures print `choke <ID>:` on stderr and set JSON `"choke"`.
4. **Report** — paste the stderr picker table **verbatim** inside a markdown ` ``` ` fence (preserves box-drawing in the TUI). Do **not** rewrite as a bullet/list, do **not** put a second `props:` line under each player, do **not** dump JSON in the chat on a TTY. Stack / sit / cash-line notes stay **under** the table (already in `format_picker_table`). Games table + `--board` stay stderr tables if printed — not a paraphrased 7. Method (`pulp-cbc` vs `greedy`) and objective (`mean` = week1_score vs `floor` = sim p10 vs `ceiling` = sim p99 vs `FPPG (prior season, not this slate)`) are the stderr header. Picker `projection` is the ILP score; `fl` / `cl` when sim ran. Partial/greedy runs stay labeled. If the run stopped, quote the choke id first. **`--board` is the point estimate; `--sim` is game Monte Carlo** (`mean` / `p10` / `p50` / `p90` / `p99`) — one world per game, not a PBP copula. Lineup Fl/Cl are the joint 7. If the run omitted `--board`, say `--board` (default: starters + d2 + props; `--board=all` is the full pool). If it omitted `--sim` on a **mean** run, say `--sim` for a 10k-run distribution.
5. **Backtest (when a perfect card exists)** — confirm `results/lineup.json` is that contest (csv / player ids). Then `python3 -m ncaaf.backtest --lineup results/lineup.json --perfect ncaaf/data/perfect/<contest>.json`. Report overlap vs the hindsight 7 and vs `baseline_lock`. **Do not** change the ILP to maximize that overlap. Improvement = more hits than the entered lock — not 7/7 by construction. Wrong-slate `lineup.json`: skip the compare; still write the perfect file.

After a **finished** slate: review `ncaaf/docs/data/game-script.md`, tweak knobs in `ncaaf/script.py` if two findings agree, append the log, re-run optimize + backtest. Do not retune off a live leaderboard. Do not retune off n=1.

## Slate shape (3-gamer)

- Min 3 **teams**, not min 3 **games**. Skipping a mid game is legal. Exclusive **one** game is still infeasible.
- High total / close spread: SuperFLEX second QB often means **both QBs in that game**.
- Huge favorite (mention or sit band): **RB committee** (two RBs same team) + **fade that WR1** is still a GPP note, not an ILP constraint.
- Cheap dog **WR1** filler and **+21 dog QB** are ILP seats (`--leverage`, default on). d2 darts stay out. `--leverage=off` to disable.
- After a finished slate: if Randy pastes LineStar, confirm FanDuel (**7** slots, **$60k**, Super FLEX — not DK $50k / 8). Write `ncaaf/data/perfect/<contest>.json` (players + `fd` + `baseline_lock`). Run `python3 -m ncaaf.backtest`. **Do not** fit the ILP to overlap. DK cards: say so and skip the file.

## Conventions this skill follows

- Spec is `~/.cursor/skills/skill-architecture.md`.
- Scripts: JSON stdout / diagnostics stderr / graceful failure (spec A4).
- **`WHY.md`:** read before changing this skill's design. After a run that locks a non-obvious choice, append a dated line. Skip routine runs. Cross-project lessons go to `/curate-vault`.
