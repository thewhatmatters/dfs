# NCAAF data sources (choke map)

**One catalog.** Scrapers and HTTP live in the modules below. This file is
the map of **seams**: when something fails, stderr is `choke <ID>: …` and
JSON has `"choke": "<ID>"`. Look up the id here. Do not scrape FanDuel.

Skill pointer: `.cursor/skills/optimize-ncaaf-classic/references/sources.md`.

## Catalog

| choke | seam | module | auth / grant | on failure | skip |
|-------|------|--------|--------------|------------|------|
| `CSV_FANDUEL` | FanDuel players-list CSV (local) | `ncaaf/players.py` | none — you download it | **stop** | — |
| `REPO` | package + rules doc missing | preflight | — | **stop** | — |
| `LINES_KEY` | no CFBD and no Odds key | `ncaaf/lines.py` | `CFBD_API_KEY` or `ODDS_API_KEY` | **stop** (no silent FPPG) | `--use-fppg` only if asked |
| `LINES_AUTH` | lines HTTP 401/403 | `ncaaf/lines.py` | same keys | **stop** | — |
| `LINES_CFBD` | CFBD `GET /lines` parse/HTTP | `ncaaf/lines.py` | `CFBD_API_KEY` | **stop** | `--lines-source=odds` or `--lines-json` |
| `LINES_ODDS` | Odds API game odds | `ncaaf/lines.py` | `ODDS_API_KEY` (query param is their contract; do not log it) | **stop** | `--lines-source=cfbd` |
| `LINES_JSON` | replay `--lines-json` missing/bad | `ncaaf/lines.py` | — | **stop** | — |
| `LINES_JOIN` | FanDuel abbrev ↔ school | `ncaaf/teams.py` | — | **stop** | add a `TEAMS` row |
| `LINES_ATTACH` | no pool left after implied totals | `ncaaf/projections.py` | — | **stop** | — |
| `DEPTH_OURLADS` | OurLads HTML, slate teams only | `ncaaf/ourlads.py` | grant: [`ourlads-authorization.md`](ourlads-authorization.md) | **stop** | `--skip-depth` (unlisted prior) |
| `DEPTH_JOIN` | OurLads team/name map | `ncaaf/ourlads.py`, `ncaaf/depth.py` | — | **stop** if unmapped team; unmatched **names** print on the board, not fatal | `NAME_OVERRIDES` |
| `MIX_CFBD` | CFBD advanced / usage HTTP | `ncaaf/mix.py` | `CFBD_API_KEY` | **stop** | `--skip-mix` (spread-only + depth prior) |
| `MIX_AUTH` | mix HTTP 401/403 | `ncaaf/mix.py` | `CFBD_API_KEY` | **stop** | `--skip-mix` |
| `PROPS_ODDS_KEY` | no Odds key for player props | `ncaaf/props.py` | `ODDS_API_KEY` | **degrade** — skip overlay, keep implied×depth×script | `--skip-props` |
| `PROPS_ODDS` | events / per-game prop HTTP | `ncaaf/props.py` | ~70 credits/slate; cache `ncaaf/data/odds-props/` | **stop** | `--skip-props` |
| `PROPS_JOIN` | book name unmatched | `ncaaf/props.py` | — | **not a stop** — `prop_status=unmatched` on the note | — |
| `SOLVER_PULP` | PuLP not importable | preflight / `ncaaf/solver.py` | `pip install pulp` | **degrade** greedy | `--greedy` |
| `SOLVER_INFEASIBLE` | no legal 7 | `ncaaf/solver.py` | — | **stop** exit 2 | relax floor / SuperFLEX |
| `INTERVIEW` | stdin grill parse / lock not in pool | `ncaaf/interview.py` | — | **stop** | `--agent` or `--interview-defaults` |
| `OBJECTIVE_FPPG` | `--use-fppg` with `--objective=floor\|ceiling` | `ncaaf/optimize.py` | — | **stop** | `--objective=mean` or drop `--use-fppg` |
| `SALARY_BOUNDS` | `--min-salary` vs cap | `ncaaf/rules.py` | — | **stop** | — |

## Stop vs degrade (quick)

- **Must have to score the slate:** CSV, lines key + fetch + join.
- **May skip:** OurLads (`--skip-depth`), props (`--skip-props` or missing Odds key).
- **May degrade:** PuLP → greedy (label the lineup).

Props are a **±20% tilt** on the implied score when a volume line joins.
Missing props is not a lines failure and not a blank choke — see picker
`note` / `prop_status`.

## Related

- Lines formula: [`vegas-implied-totals.md`](vegas-implied-totals.md)
- Props overlay: [`player-props.md`](player-props.md)
- Depth: [`ourlads-depth.md`](ourlads-depth.md)
