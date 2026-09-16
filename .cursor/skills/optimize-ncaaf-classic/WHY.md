# optimize-ncaaf-classic — Why

Living record of what this skill is, the decisions behind it, and any
non-obvious constraints (spec A12).

Created: 2026-09-05  ·  Generator: generate-skill @ 2026-08-20

## 1. Purpose

Solve the highest-projection legal FanDuel NCAAF classic lineup for a slate CSV.

## 2. Reusable patterns (link to spec A1..A15)

Follows `~/.cursor/skills/skill-architecture.md` A1–A15. Solver lives in the
**repo** (`ncaaf/`), not vendored inside the skill — skill scripts are a thin
CLI seam (A4 one-concern). Scoring tables are in `ncaaf/docs/sites/` (A1 branch:
only loaded when rules need a check).

## 3. Decision log

- 2026-09-05: scaffolded by generate-skill; project location (this repo is the product).
- 2026-09-05: **FPPG as projection** until a model exists — disclose every run.
- 2026-09-05: **Week-1 objective is Vegas implied team totals**, not CSV FPPG
  (prior season / empty). Missing CFBD/Odds key is a hard gate — never silent
  FPPG. Player score = implied total + small leftover/value term; no fake
  usage. Join + spread sign in `ncaaf/docs/data/vegas-implied-totals.md`.
- 2026-09-05: **OurLads depth** is an authorized HTTP Fetcher ingest (no
  stealth). Rank 1 is a role prior on the week-1 score, not snap %. FPPG
  stays opt-in.
- 2026-09-05: ILP team indicator `y[t]` must be linked both ways (`sum x >= y`
  and `y >= x_i`). One-way linking let CBC set a dummy third team and return
  a 2-team lineup when implied totals were identical within a team.
- 2026-09-05: **PuLP ILP default, greedy degrade** — 7-slot exact solve is the
  product; stdlib-only exact ILP is not worth a custom solver.
- 2026-09-05: **CFB scoring ≠ NFL** — no yardage bonuses; TE → WR slot.
  Locked from fanduel.com/rules College Football table vs NFL table.
- 2026-09-05: model-invoked (A14) — lineup requests should auto-route; not
  side-effectful (no contest entry).
- 2026-09-05: **Per-player notes** — every picker row states Odds props
  (lines + book) vs “no player props” implied×depth×POS_FD_SHARE (join-miss
  vs no market when cheap), OurLads starter `(!)` as a role prior not snaps,
  and game script only from spread/implied/team props. `Lineup.notes` stays
  solver-level; blank notes were greedy diagnostics, not a missing API.
- 2026-09-05: **Closing-script from the closing spread** hits the ILP via
  `script_mult` even when 2026 pass rates are missing (week 1). Huge
  favorites (≤ −21) haircut WR/TE (stronger for starters) and modestly QB.
  2026-09-06: RB1 sits some; RB2/RB3 boost. Not live score, not 2025 mix.
- 2026-09-05: renamed `optimize-lineup` → `optimize-ncaaf-classic`; rules/data
  docs live under `ncaaf/docs/`. NFL gets its own adapter/skill later — do
  not import CFB scoring.
- 2026-09-06: **Game script playbook** `ncaaf/docs/data/game-script.md` —
  weekly knobs in `ncaaf/script.py`. Blowout (≤ −21): WR/TE/QB sit; **RB1
  sits some, RB2/RB3 boost**. Grind (−3 to −14): spread tilt only. Tweaks
  after 0 quarters left, not live.
- 2026-09-06: house spend floor **$58,000** (was $57,000). Perfect card 133865
  spent $59,600; leftover-cap cheap WR/TEs were the failure mode, not this
  lock ($59,900). `--min-salary=57500` if you want the half-step.
- 2026-09-06: **Interview then ILP** (interactive only). Crunch sources,
  grill game targets / sit / SuperFLEX one Q at a time. **Backtest** vs
  LineStar perfect (`ncaaf/data/perfect/133865.json`) is overlap scorecard
  only — never the objective. Baseline lock overlap is 1 (Smith).
- 2026-09-06: **Lean multiplier 1.08** on players in selected games when Q1
  is not all and Q2 is lean. Modest objective bump so the pool stays intact;
  not a `script.py` sit-band retune. Exclusive is a pool filter to those
  games’ teams. `--agent` still skips the grill. Empty input = recommend.
- 2026-09-06: **Q1 is the Grok question card, not typed indices.** Interactive
  Grok calls `ask_user_question` (`questions[].multi_select` on the game rows;
  Space toggles). Do not paste the numbered ASCII games table as the prompt.
  CLI stdin grill in `ncaaf/interview.py` is unchanged.
- 2026-09-06: **Q1 tool is `GrokBuild:ask_user_question` (not MCP).** Session
  `01a07728-…` 404'd the native name, skipped the card, printed 14 games and
  “Reply all, top, or pick” — failed grill. Retry aliases `ask_user_question`
  then `AskUserQuestion`; still missing → stop, list tools, never a typing
  prompt. `label` is the ↑/↓/Space surface. No Python curses TUI.
- 2026-09-06: Randy — TUI name is PascalCase `AskUserQuestion`. First call
  that; 404 ladder only if needed: `GrokBuild:ask_user_question` →
  `ask_user_question` → `x.ai/ask_user_question`. Snake_case first is why
  the card never opened.
- 2026-09-06: **“Let’s optimize” = NCAAF** until `optimize-nfl-classic`
  exists. Do not ask “NCAAF or NFL?”. Next week: stand up
  `optimize-nfl-classic` as its own adapter (FanDuel NFL classic rules,
  not CFB) — do not import `ncaaf/` into `nfl/`.
- 2026-09-06: **`optimize-nfl-classic` exists.** Bare “Let’s optimize”
  stays NCAAF — do not ask NCAAF vs NFL. NFL is explicit (“optimize NFL”,
  `/optimize-nfl-classic`).
- 2026-09-06: **AskUserQuestion 404 → `--interview-defaults`**, do not
  stop the solve. Card missing is not a typing-prompt fallback and is
  not a Grok Rust/`register_resource`/serde fix. Defaults: all / keep
  sit / qb / none.
- 2026-09-06: **Grok question card is not wired — skip it.** TUI
  advertises `ask_user_question` / `GrokBuild:ask_user_question` /
  `x.ai/ask_user_question` then `post_tool_use_failure`. Do not call
  `AskUserQuestion` or retry the 404 ladder. Interactive Grok path:
  crunch → one line `Grok question card not wired — using interview
  defaults.` → `--agent --interview-defaults --board --sim` (unless flags already
  given) → picker + backtest. CLI stdin grill in `ncaaf/interview.py`
  stays for `python3 -m ncaaf.optimize` without `--agent`.
- 2026-09-06: **Repo-root shims** `scripts/preflight.py` and `scripts/optimize.py`
  `runpy.run_path` the skill scripts (A4 seam stays). Grok cwd is repo root;
  skill-relative `scripts/` is not on PATH — listing `scripts/` then
  improvising was the TUI choke.
- 2026-09-06: **Projection board** `--board` prints the ILP pool (props, else
  implied × depth_prior × POS_FD_SHARE × script_mult) grouped by game/pos so
  Randy can see more than the 7. Default is starters + d2 + anyone with
  `prop_fd`; `--board=all` is the full pool. Same rows in `payload["board"]`.
  Does not change the objective. Rush/target share stay null until CFBD boxes.
- 2026-09-06: **Monte Carlo `--sim`**, not a SaberSim-class game sim.
  `--board` is the point estimate (`prop_fd` or implied×depth×share×script).
  `--sim` (default 10000, `--sim=0` off, `--sim-seed=1`) draws each player
  independently: volume lines ~ Normal(line, σ) → CFB scoring (no NFL
  bonuses, no ints/fumbles without a line); else team points ~
  Normal(implied, 0.20×implied, floor 3) × depth × share × script. Report
  mean/p10/p50/p90 on the board. Mean ILP stays `week1_score` (not sim p50).
  Play-by-play / copula correlation is a later adapter.
- 2026-09-06: **`--objective=mean|floor|ceiling`**. Default ILP stays
  `week1_score` (not sim p50). Floor = independent sim **p10**; ceiling =
  sim **p90**. Not historical FPPG max, not correlated SaberSim. Floor/ceiling
  run `simulate_pool` even without `--sim` (default 10000 / `--sim-seed`).
  Interview lean (×1.08) is re-applied to p10/p90 after the swap — not
  double-applied on mean. `--use-fppg` + floor/ceiling is a loud choke
  (`OBJECTIVE_FPPG`). TUI “let’s optimize”: `--board --sim` (mean ILP, print
  p10/p90); cash/floor or GPP/ceiling when Randy says so. Exclusive one
  game on a 3-gamer is still infeasible (min 3 teams).
- 2026-09-07: 133970 cash 128.66 vs FD perfect 178.12 overlap 2/7; 3-gamer
  slate-shape; DK vs FD LineStar; no knob/floor change.
- 2026-09-09: **Props are a ±20% tilt on implied, not an override.** Dual
  path (book FD pts vs implied×share) gave listed stars a second currency.
  One formula: `base × clamp(prop_fd/base, 0.80, 1.20)`. Script still hits
  prop names. `--skip-props` = factor 1.0.
- 2026-09-09: **Opponent D rush vs pass PPA** (CFBD `defense.*.ppa`, already
  in the mix cache) splits `script_mult`. Gap = pass PPA − rush PPA, coef
  0.40, cap ±0.35. Do not scrape ESPN YPG; overall D stays in implied total.
- 2026-09-11: Randy — **no question card on NCAAF** (same as NFL). Skip
  is product, not a missing Grok tool. Do not call `AskUserQuestion` or
  retry the 404 ladder. Agents: `--agent --interview-defaults`. CLI
  stdin grill in `ncaaf/interview.py` stays on a real TTY. Do not add a
  card to NFL.
- 2026-09-11: **Preflight maps CSV Team/Opponent vs `ncaaf.teams.TEAMS`.**
  Unmapped abbrevs → `down` `LINES_JOIN` (sorted list + add each to
  `ncaaf/teams.py`). Skip when there is no CSV file yet. Stops before
  optimize.
- 2026-09-12: **`--sim` is a structural game draw**, not independent
  player noise. Each game: Vegas total + home spread → both scores;
  `script_mult` uses the **realized** margin. Teammates share the world.
  Mean ILP stays `week1_score` (correlation does not change E[sum]).
  Lineup Fl/Cl are the joint 7 in those worlds. Not a PBP copula / not
  SaberSim; 2025 still unused.
- 2026-09-13: 134050 cash 128.62 vs FD perfect 257.88 overlap 2/7
  (Sheppard, Dixon). Same as entered lock — not improved. Ceiling p90
  still ranks Moore/Hill/Sag over the five 6× smashers. No knob/floor
  change. Do not fit names.
- 2026-09-13: **Leverage seats + ceiling p99.** p90 cannot see P(win)≈8%
  dog upsets. Mean ILP now requires ≥1 +14 WR1/TE1 and ≥1 +21 dog QB
  when those players exist (`ncaaf/leverage.py`). Construction from
  Coleman (133970) + Harris/Mestemaker (134050), not a name list.
  `--leverage=off` restores the old mean 7. Ceiling ILP uses p99.

## 4. Known limitations / environment caveats

- Empty FPPG on early-season slates is why FPPG is not the week-1 objective.
- Greedy may fail min-teams or cap; ILP is the real path.
- $60,000 cap is from 2025 contest primers + NFL classic parity; re-check lobby.

## 5. Audit rubric coverage

See `skill-architecture.md` §B; this skill targets every PASS that applies.

## 6. Notes

Hard dep: repo `ncaaf` package (same git tree). Soft: `pulp` (gated, greedy degrade).
Vegas path: `CFBD_API_KEY` or `ODDS_API_KEY` (hard gate `LINES_KEY`, not an FPPG degrade). Choke ids: `ncaaf/docs/data/sources.md`.
