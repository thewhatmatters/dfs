# NFL data sources (choke map)

**One catalog.** HTTP lives in the modules below. This file is the map of
**seams**: when something fails, stderr is `choke <ID>: …` and JSON has
`"choke": "<ID>"`. Look up the id here. Do not scrape FanDuel.

Skill pointer: `.cursor/skills/optimize-nfl-classic/references/sources.md`.

## Catalog

| choke | seam | module | auth / grant | on failure | skip |
|-------|------|--------|--------------|------------|------|
| `CSV_FANDUEL` | FanDuel players-list CSV (local) | `nfl/players.py` | none — you download it | **stop** | — |
| `REPO` | package + rules doc missing | preflight | — | **stop** | — |
| `LINES_KEY` | no Odds key | `nfl/lines.py` | `ODDS_API_KEY` | **stop** (no silent FPPG) | `--lines-json` |
| `LINES_AUTH` | lines HTTP 401/403 | `nfl/lines.py` | same key | **stop** | — |
| `LINES_ODDS` | Odds API game odds | `nfl/lines.py` | `ODDS_API_KEY` (query param is their contract; do not log it) | **stop** | `--lines-json` |
| `LINES_JSON` | replay `--lines-json` missing/bad | `nfl/lines.py` | — | **stop** | — |
| `LINES_JOIN` | FanDuel abbrev ↔ Odds name | `nfl/teams.py` | — | **stop** | add a `TEAMS` row |
| `LINES_ATTACH` | no pool left after implied totals | `nfl/projections.py` | — | **stop** | — |
| `INJ_ESPN` | ESPN injury dump (`site.web.api`; `site.api` often 403) | `nfl/injuries.py` | none (public ESPN) | **stop** | `--skip-injuries` |
| `DEPTH_OURLADS` | OurLads NFL HTML, slate teams only | `nfl/ourlads.py` | grant: [`ourlads-authorization.md`](ourlads-authorization.md) | **stop** | `--skip-depth` (unlisted prior) |
| `DEPTH_ESPN` | ESPN depth charts (optional `--depth-source=espn`) | `nfl/depth.py` | none (public ESPN; often 403) | **stop** | `--skip-depth` or default OurLads |
| `DEPTH_JOIN` | OurLads/ESPN team/name map | `nfl/ourlads.py`, `nfl/depth.py`, `nfl/teams.py` | — | **stop** if unmapped team; unmatched **names** print on the board, not fatal | `NAME_OVERRIDES`; JAC→JAX, ARI→ARZ; WAS is WAS not WSH |
| `PROPS_ODDS_KEY` | no Odds key for player props | `nfl/props.py` | `ODDS_API_KEY` | **degrade** — skip overlay, keep implied×depth | `--skip-props` |
| `PROPS_ODDS` | events / per-game prop HTTP | `nfl/props.py` | cache `nfl/data/odds-props/` | **stop** | `--skip-props` |
| `PROPS_JOIN` | book name unmatched | `nfl/props.py` | — | **not a stop** — `prop_status=unmatched` on the note | — |
| `SOLVER_PULP` | PuLP not importable | preflight / `nfl/solver.py` | `pip install pulp` | **degrade** greedy | `--greedy` |
| `SOLVER_INFEASIBLE` | no legal 9 | `nfl/solver.py` | — | **stop** exit 2 | relax floor / `--max-per-team=4` / `--stack-qb=off` / `--bring-back` |
| `SALARY_BOUNDS` | `--min-salary` vs cap | `nfl/rules.py` | — | **stop** | — |
| `TARGETS_LINEUPS` | Lineups.com RB/WR/TE target pages (public HTML + SSR JSON) | `nfl/targets.py` | grant: [`lineups-authorization.md`](lineups-authorization.md) | **stop** | omit targets refresh |
| `TARGETS_CSV` | local `nfl/data/targets.csv` (or `--targets-csv`) missing/empty/bad | `nfl/targets.py` | none — produced by `python3 -m nfl.targets --refresh` | **degrade** — usage uses snaps or 1.0 | `--skip-targets` |
| `TARGETS_JOIN` | Lineups full team name ↔ FanDuel abbrev (refresh) | `nfl/targets.py`, `nfl/teams.py` | — | **stop** on unmapped **team**; unmatched **names** print on stderr / JSON `targets`, not fatal | add Odds full name to `TEAMS`; do not invent player aliases |
| `SNAPS_LINEUPS` | Lineups.com RB/WR/TE snap-count pages (public HTML + SSR JSON) | `nfl/snaps.py` | grant: [`lineups-authorization.md`](lineups-authorization.md) | **stop** | omit snaps refresh |
| `SNAPS_CSV` | local `nfl/data/snaps.csv` (or `--snaps-csv`) missing/empty/bad | `nfl/snaps.py` | none — produced by `python3 -m nfl.snaps --refresh` | **degrade** — RB usage uses targets or 1.0 | `--skip-snaps` |
| `SNAPS_JOIN` | Lineups full team name ↔ FanDuel abbrev (snaps refresh) | `nfl/snaps.py`, `nfl/teams.py` | — | **stop** on unmapped **team**; unmatched **names** print on stderr / JSON `snaps`, not fatal | add Odds full name to `TEAMS`; do not invent player aliases |
| `UPLOAD_CSV` | FanDuel upload write/validate | `nfl/upload.py` | — | **stop** | omit `--upload` / `--n-lineups=1` |

## Deferred (not wired this pass)

| id | seam | why deferred |
|----|------|----------------|
| `WEATHER_NWS` | NWS hourly | still too short for Sep 13 kickoffs |
| `INACTIVES_SUNDAY` | Sunday inactives | not ingested |
| `PFF_PRO` | PFF Pro API | not wired |
| `ANYTIME_TD` | Odds anytime-TD overlay | skipped in `nfl/props.py` |
| `PROPS_LINEUPS` | Lineups player-prop pages | inventoried only: `https://www.lineups.com/nfl/player-prop-bets/` — Odds API stays the overlay |

Do not scrape FanDuel. Do not `--refresh-props` unless asked (burns Odds credits).

## Stop vs degrade (quick)

- **Must have to score the slate:** CSV, lines key + fetch + join.
- **May skip:** ESPN injuries (`--skip-injuries`), OurLads depth (`--skip-depth`), Lineups RB/WR/TE targets (`--skip-targets` or missing CSV), Lineups snaps (`--skip-snaps` or missing CSV), props (`--skip-props` or missing Odds key).
- **Optional depth fallback:** `--depth-source=espn` (`DEPTH_ESPN`; often 403).

Depth: [`ourlads-depth.md`](ourlads-depth.md).
- **May degrade:** PuLP → greedy (label the lineup).

Props are a **±20% tilt** on the implied score when a volume line joins.
Missing props is not a lines failure and not a blank choke — see picker
`note` / `prop_status`.

## Targets + snaps refresh + usage tilt

Wednesday-style weekly refresh (after the prior week’s games land):

```
python3 -m nfl.targets --refresh
python3 -m nfl.snaps --refresh
```

Targets: RB + WR + TE public pages → `nfl/data/targets.csv`. Cache: `nfl/data/lineups-targets/`. Formula: [`targets.md`](targets.md).

Snaps: RB + WR + TE public pages → `nfl/data/snaps.csv`. Cache: `nfl/data/lineups-snaps/`. RB snap_share is the rush-role tilt; WR/TE snaps load for later and do not stack on targets. Formula: [`snaps.md`](snaps.md).

Grant: [`lineups-authorization.md`](lineups-authorization.md). Missing CSV degrades (`TARGETS_CSV` / `SNAPS_CSV`); name gaps are reported, not a stop.
