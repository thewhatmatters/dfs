---
name: optimize-nfl-classic
description: >-
  Optimize a FanDuel NFL classic DFS lineup from a players-list CSV: salary
  cap, QB/2 RB/3 WR/TE/FLEX/DEF, min 3 teams, house max 3 per team. FanDuel NFL
  classic only — not NCAAF. Use when the user asks to optimize an NFL slate,
  "optimize NFL", "Let's optimize NFL", "FanDuel NFL classic", "best NFL
  lineup", or "/optimize-nfl-classic". Do not fire on bare "Let's optimize"
  — that stays NCAAF (optimize-ncaaf-classic).
---

# optimize-nfl-classic

Solve the highest-projection legal FanDuel NFL classic lineup for a slate CSV.

## What it does

Loads contest rules from this repo, filters the FanDuel export, joins Vegas
lines + OurLads depth + ESPN injuries + Odds props (cached), and runs the PuLP ILP
(greedy if PuLP is missing). Default objective is **week1_score** (implied×depth×share, volume props a
±20% tilt — not an override). No FPPG. No stdin interview. No question card.

## How to run

Triggers: “optimize NFL”, “Let’s optimize NFL”, “FanDuel NFL classic”,
`/optimize-nfl-classic` (optional `--csv=PATH`). **Not** bare “Let’s optimize”
(that is NCAAF). Do not ask “NCAAF or NFL?” on the bare phrase.

Crunch first (preflight, cached lines/OurLads depth/injuries/props). **Do not**
`--refresh-props` unless asked. **Do not** call `AskUserQuestion` /
`GrokBuild:ask_user_question` / `ask_user_question`. Do not search MCP.
Do not scrape FanDuel. Do not import `ncaaf`. Do not pass `-m`.

Solve immediately with `python3 -m nfl.optimize --csv … --sim --out results/nfl-lineup.json` (mean ILP, print p10/p90). **Do not** pass `--agent` on a TTY (stdout would be a JSON blob). Newest `FanDuel-NFL-*-players-list.csv` in `nfl/data/`.
GPP / ceiling → `--objective=ceiling`. Cash / floor → `--objective=floor`.
`--n-lineups=N` **only** if the user named a count (do not run 150).
`--bring-back=N` **only** if the user asked; default 0. House max 3/team
(FanDuel 4; `--max-per-team=4` restores lobby). `--stack-qb` on unless the
user asked `--stack-qb=off`. n>1 writes
`nfl/export/`. Do not rewrite existing 150 CSVs.

Repo-root `scripts/optimize.py` is the **NCAAF** shim — do not use it here.

## Flags

| Flag | Meaning |
|------|---------|
| `--agent` | JSON on stdout; no prompts |
| `--csv=PATH` | FanDuel players-list CSV (required) |
| `--out=PATH` | write JSON artifact |
| `--lines-json=PATH` | replay Odds / simple-games JSON |
| `--skip-depth` | skip OurLads depth join (unlisted prior) |
| `--refresh-depth` | refetch OurLads HTML (slate teams only) |
| `--depth-source=ourlads\|espn` | default **ourlads**; `espn` is optional fallback (often 403) |
| `--skip-injuries` | skip ESPN injury join |
| `--refresh-injuries` | refetch ESPN injuries |
| `--skip-props` | skip Odds API player props |
| `--refresh-props` | refetch props (burns credits) — do not unless asked |
| `--exclude-questionable` | drop CSV Q; IR/NA already dropped |
| `--greedy` | skip PuLP even if installed |
| `--min-salary=N` | house spend floor (default 58000; 0 disables) |
| `--board` | print pool projections on stderr; JSON `board` |
| `--board=all` | same board, full pool |
| `--sim` | Game Monte Carlo (Vegas total+spread; teammates share the world; default 10000; `--sim=0` off). Lineup Fl/Cl = joint 9 p10/p90. Not a PBP copula / not SaberSim. Mean ILP stays implied×depth×share with ±20% prop tilt |
| `--sim-seed=1` | RNG seed for `--sim` |
| `--objective=mean\|floor\|ceiling` | ILP score. **Default `mean`** = week1_score. `floor` = sim p10. `ceiling` = sim p90 |
| `--n-lineups=N` | unique 9s (default 1; max 150). Only if the user named a count |
| `--min-unique=N` | min different players vs the previous 9 (default 2) |
| `--bring-back=N` | require N opposing WR/TE/QB vs a QB pass stack (default **0** = off) |
| `--max-per-team=N` | max from one team (house default **3**; FanDuel lobby 4). `--max-per-team=4` restores lobby. Validate 1..4 |
| `--stack-qb=on\|off` | 2+ WR/TE from a team requires that team's QB (default **on**) |
| `--upload=PATH` | extra FanDuel upload CSV path |
| `--cash-line=N` | house cash line for ceiling gap (default 150) |

## Step 0 — Mode probe

Probe `python3`. **SCRIPTS** if `python3` works; else **NATIVE** (read
`nfl/docs/sites/fanduel-nfl.md` and reason a lineup by hand — disclose it
is not ILP).

## Steps

1. **Rules** — read `nfl/docs/sites/fanduel-nfl.md` if anything looks off vs the CSV (`Roster Position`, salaries, DST).
2. **Preflight** — `python3 .cursor/skills/optimize-nfl-classic/scripts/preflight.py --csv=<path>`. `down` stops. `gated` `SOLVER_PULP`: `--agent` Skip → greedy, say so. If overall is not `ready`, name the **`choke` id** and open [`references/sources.md`](references/sources.md) (catalog: `nfl/docs/data/sources.md`). Do not dump the catalog on a green run.
3. **Solve** — Interactive TTY: `python3 -m nfl.optimize --csv=<path> --sim --out results/nfl-lineup.json` (**no** `--agent`). Same flags via `python3 .cursor/skills/optimize-nfl-classic/scripts/optimize.py`. Newest CSV: `nfl/data/FanDuel-NFL-*-players-list.csv`. GPP/ceiling → `--objective=ceiling`. `n-lineups` only if named. Cached props. JSON stdout only with `--agent` / `--json` / non-TTY. Gate `LINES_KEY` is a **hard stop**. Ingest failures print `choke <ID>:` on stderr and set JSON `"choke"`.
4. **Report** — paste the stderr picker table **verbatim** inside a markdown ` ``` ` fence (preserves box-drawing in the TUI). Do **not** rewrite as a bullet/list, do **not** put a second `props:` line under each player, do **not** dump JSON in the chat on a TTY. Stack / bring-back / cash-line notes stay **under** the table (already in `format_picker_table`). Method (`pulp-cbc` vs `greedy`) and objective (`mean` / `floor` / `ceiling`) are the stderr header line above the table. Picker `projection` is **week1_score** (not the ILP objective with stack premium); `fl` / `cl` when sim ran. Confirm **no team has 4**. If the run stopped, quote the choke id first. n>1: upload path under `nfl/export/`.

## Conventions this skill follows

- Spec is `~/.cursor/skills/skill-architecture.md`.
- Scripts: JSON stdout / diagnostics stderr / graceful failure (spec A4).
- **`WHY.md`:** read before changing this skill's design. After a run that locks a non-obvious choice, append a dated line. Skip routine runs. Cross-project lessons go to `/curate-vault`.
