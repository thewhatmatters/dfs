# DFS — agent instructions

Salary-cap daily fantasy. First sport/site: **NCAA football, FanDuel classic**. NFL FanDuel classic is a second adapter (`nfl/`, `python3 -m nfl.optimize`). Keep sport code in its package — do **not** import `ncaaf` from `nfl`.

## Runtime — Grok CLI

**Grok CLI** (`~/.grok/bin/grok`, or `$GROK_BIN`) is the implementation agent. It already loads this `AGENTS.md` as project rules. From **Cursor**, do not implement in-session: pack the task and run `bash scripts/dispatch-grok.sh <prompt-file>` (see `.cursor/rules/grok-cli-dispatch.mdc`). Interactive Grok in this directory is also fine — skip Cursor entirely when you want zero Cursor tokens.

## Product lock

- Optimize the **best legal lineup** for a given slate (max objective under FanDuel NCAAF constraints).
- Canonical rules: [`ncaaf/docs/sites/fanduel-ncaaf.md`](ncaaf/docs/sites/fanduel-ncaaf.md). Code copy: `ncaaf/rules.py`. If they drift, fix both.
- NFL FanDuel classic: [`nfl/docs/sites/fanduel-nfl.md`](nfl/docs/sites/fanduel-nfl.md) (`nfl/rules.py`). Optimizer: `python3 -m nfl.optimize`. **“Let’s optimize” stays NCAAF** even though `optimize-nfl-classic` exists. NFL path is explicit (“optimize NFL”, `/optimize-nfl-classic`).
- Do **not** invent contest rules from NFL FanDuel. CFB has **no yardage bonuses**, **no DST/K slots**, TE is **WR-eligible**, SuperFLEX is the 7th slot, OT only **first two periods**.
- Cap is **$60,000**. **Spend floor $58,000** (house rule; `--min-salary=0` to disable). **SuperFLEX is a second QB** by default (`--superflex=any` for FanDuel’s full QB/RB/WR/TE slot). Min **3 teams**, max **4** from one team.
- **Week-1 objective is Vegas implied team totals** (spread + total via CFBD `/lines` or The Odds API), not CSV `FPPG`. Join + formula: [`ncaaf/docs/data/vegas-implied-totals.md`](ncaaf/docs/data/vegas-implied-totals.md). FPPG is prior-season / empty — `--use-fppg` opt-in only, labeled as such. Missing `CFBD_API_KEY` / `ODDS_API_KEY` is a hard gate; do not silently keep FPPG.
- **OurLads:** scrape is **authorized** for this repo — NCAA grant [`ncaaf/docs/data/ourlads-authorization.md`](ncaaf/docs/data/ourlads-authorization.md); NFL grant [`nfl/docs/data/ourlads-authorization.md`](nfl/docs/data/ourlads-authorization.md) (Randy: roommate created OurLads; NFL depth same as NCAA). Polite, slate-only, cached. Do not stealth-crawl. Do not scrape FanDuel. Depth rank is not the week-1 ILP objective.
- **Chokes:** ingest failures print `choke <ID>:` — NCAAF catalog [`ncaaf/docs/data/sources.md`](ncaaf/docs/data/sources.md); NFL catalog [`nfl/docs/data/sources.md`](nfl/docs/data/sources.md).

## Commands

```
python3 -m pip install -r requirements.txt
export CFBD_API_KEY='…'   # https://collegefootballdata.com/key  (or ODDS_API_KEY)
python3 -m ncaaf.optimize --csv ncaaf/data/<export>.csv
python3 -m ncaaf.optimize --csv ncaaf/data/<export>.csv --agent --out results/lineup.json
python3 -m ncaaf.optimize --csv ncaaf/data/<export>.csv --use-fppg   # prior season, not this slate
python3 -m ncaaf.depth --csv ncaaf/data/<export>.csv                 # OurLads slate depth → ncaaf/data/depth.csv
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv"          # FanDuel NFL classic
python3 -m nfl.depth --csv "nfl/data/<players-list>.csv"             # OurLads NFL depth → nfl/data/depth.csv
# NFL uploads: nfl/export/ when --n-lineups>1. Cached props; do not --refresh-props unless asked.
# NCAAF player props: ODDS_API_KEY, cached, ~70 credits/slate (see ncaaf/docs/data/player-props.md)
# NFL player props: GANGSTASH_API_KEY (see nfl/docs/data/player-props.md). Game lines still use ODDS_API_KEY.
# After week 1: CFBD pass/rush mix — ncaaf/docs/data/play-distribution.md (not live until 2026 boxes)
```

Lineup skills: `.cursor/skills/optimize-ncaaf-classic/` (CFB; bare “Let’s optimize”). `.cursor/skills/optimize-nfl-classic/` (NFL; “optimize NFL”, `/optimize-nfl-classic`).

## Interactive lineup builder (Grok TUI)

Start this path when the user says any of:

- “Let’s optimize”
- “Let’s optimize NCAA football”
- “optimize NCAAF”
- “initialize the optimal lineup builder”
- “optimize today’s slate”
- `/optimize-ncaaf-classic`

**“Let’s optimize” = NCAAF** even though `optimize-nfl-classic` exists. Do not ask “NCAAF or NFL?”. NFL only when the user says “optimize NFL”, “Let’s optimize NFL”, “FanDuel NFL classic”, or `/optimize-nfl-classic`.

Crunch first (preflight, cached lines/depth/props — do not `--refresh-props` unless asked). **No question card** (NCAAF and NFL). Agents: `--agent --interview-defaults`. Defaults: all games / keep sit / SuperFLEX qb / no locks. Optional: one line “using interview defaults.”

Solve immediately with `python3 -m ncaaf.optimize --csv … --agent --interview-defaults --board --sim` (mean ILP + leverage seats, print p10/p99). Newest FanDuel CSV in `ncaaf/data/`. Unless the user already gave `--games` / `--cut` / `--sit` / `--superflex` / `--lock` / `--fade` / `--objective`. Cash / floor → `--objective=floor`. GPP / ceiling → `--objective=ceiling`. Exclusive one game on a 3-gamer is still infeasible (min 3 teams). Then picker report + backtest if `ncaaf/data/perfect/<contest>.json` exists.

Stdin interview in `ncaaf/interview.py` is for CLI `python3 -m ncaaf.optimize` without `--agent` (real TTY) — not a question card. No curses TUI. `--interview-defaults` applies recommends even on a TTY.

NFL (`/optimize-nfl-classic`): no interview / question card. Newest `nfl/data/FanDuel-NFL-*-players-list.csv`. `python3 -m nfl.optimize --csv … --sim` (mean default). GPP / ceiling → `--objective=ceiling`. `n-lineups` only if the user named a count. Cached props; do not `--refresh-props`. Uploads: `nfl/export/` when n>1. `--bring-back` default 0. House max **3** per team (`--max-per-team=4` restores FanDuel lobby 4). `--stack-qb` on (2+ WR/TE from a team ⇒ that team's QB; `--stack-qb=off` disables). n>1 defaults `--max-exposure=0.60`, `--min-unique=3` vs every locked 9, `--diversity=coverage` (lineup #1 stays mean). `--max-exposure=1 --diversity=chalk --min-unique=2` restores chalk lock-in.

## Do not

- Scrape FanDuel or automate entries.
- Stealth-crawl OurLads or scrape all FBS / all 32 NFL teams every run (grant is polite + slate-only).
- Treat empty-FPPG players as sleepers without `--include-unprojected` (`--use-fppg` mode).
- Use CSV FPPG as the week-1 objective (it is last season or empty).
- Copy NFL bonuses (100 rush/rec, 300 pass) into CFB scoring.
- Wire a vault project layer until there is durable knowledge to curate (`/wire-vault`).

## After a slate run

Record the CSV filename, solver method (`pulp-cbc` vs `greedy`), and whether the lobby matched `ncaaf/docs/sites/fanduel-ncaaf.md` (or `nfl/docs/sites/fanduel-nfl.md`). If FanDuel changed a rule, update the doc + `rules.py` in the same change. NFL uploads land in `nfl/export/`.
