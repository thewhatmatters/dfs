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
| `LINES_AUTH` | gangstash lines HTTP 401/403 | `nfl/lines.py`, `nfl/http.py` | `GANGSTASH_API_KEY` | **stop** | `--lines-json` or `--lines-file` |
| `LINES_JSON` | replay `--lines-json` missing/bad | `nfl/lines.py` | — | **stop** | — |
| `LINES_GANGSTASH_KEY` | gangstash lines and no key / no cache | `nfl/lines.py`, `nfl/gangstash.py` | `GANGSTASH_API_KEY` (`x-api-key`; never a service-role key) | **stop** (no silent FPPG). Message names gangstash | `--lines-json` or `--lines-file` |
| `LINES_GANGSTASH` | gangstash `dataset=game_lines` or `closing_lines` HTTP, truncated board, bad fields, or missing slate game | `nfl/gangstash_data.py`, `nfl/lines.py` | cache `nfl/data/gangstash-data/` | **stop**. Message names gangstash | `--lines-json` or `--lines-file` |
| `LINES_JOIN` | FanDuel abbrev ↔ gangstash team code | `nfl/teams.py` | — | **stop** | add a `TEAMS` row (`JAX`→`JAC`, `WSH`→`WAS`) |
| `LINES_ATTACH` | no pool left after implied totals | `nfl/projections.py` | — | **stop** | — |
| `INJ_ESPN` | ESPN injury dump (`site.web.api`; `site.api` often 403) | `nfl/injuries.py` | none (public ESPN) | **stop** | `--skip-injuries` |
| `DEPTH_OURLADS` | OurLads NFL HTML, slate teams only | `nfl/ourlads.py` | grant: [`ourlads-authorization.md`](ourlads-authorization.md) | **stop** | `--skip-depth` (unlisted prior) |
| `DEPTH_ESPN` | ESPN depth charts (optional `--depth-source=espn`) | `nfl/depth.py` | none (public ESPN; often 403) | **stop** | `--skip-depth` or default OurLads |
| `DEPTH_GANGSTASH_KEY` | default `--depth-source=gangstash` and no key / no cache | `nfl/depth.py`, `nfl/gangstash.py` | `GANGSTASH_API_KEY` | **degrade** — unlisted prior. Stderr names `--depth-source=ourlads` | `--depth-source=ourlads` or `--skip-depth` |
| `DEPTH_GANGSTASH` | gangstash `dataset=depth_charts` HTTP, truncated board, empty payload, missing columns, or slate team missing | `nfl/depth.py`, `nfl/gangstash_data.py` | `GANGSTASH_API_KEY`; cache `nfl/data/gangstash-data/` | **stop** | `--skip-depth` or `--depth-source=ourlads` |
| `DEPTH_JOIN` | OurLads/ESPN/gangstash team/name map | `nfl/ourlads.py`, `nfl/depth.py`, `nfl/teams.py` | — | **stop** if unmapped team; unmatched **names** print on the board, not fatal | `NAME_OVERRIDES`; JAC→JAX, ARI→ARZ; WAS is WAS not WSH |
| `PROPS_GANGSTASH_KEY` | no Gangstash key and no props cache | `nfl/props.py`, `nfl/gangstash.py` | `GANGSTASH_API_KEY` | **degrade** — skip overlay, keep implied×depth | `--skip-props` |
| `PROPS_GANGSTASH` | gangstash props HTTP, truncated board, or bad payload | `nfl/gangstash.py` | cache `nfl/data/gangstash-props/`; header `x-api-key` (do not log it) | **stop** | `--skip-props` |
| `PROPS_JOIN` | book name unmatched | `nfl/props.py` | — | **not a stop** — `prop_status=unmatched` on the note | — |
| `SOLVER_PULP` | PuLP not importable | preflight / `nfl/solver.py` | `pip install pulp` | **degrade** greedy | `--greedy` |
| `SOLVER_INFEASIBLE` | no legal 9 | `nfl/solver.py` | — | **stop** exit 2 | relax floor / `--max-per-team=4` / `--stack-qb=off` / `--bring-back` |
| `SALARY_BOUNDS` | `--min-salary` vs cap | `nfl/rules.py` | — | **stop** | — |
| `TARGETS_LINEUPS` | Lineups.com RB/WR/TE target pages (public HTML + SSR JSON) | `nfl/targets.py` | grant: [`lineups-authorization.md`](lineups-authorization.md) | **stop** | omit targets refresh |
| `TARGETS_CSV` | local `nfl/data/targets.csv` (or `--targets-csv`) missing/empty/bad | `nfl/targets.py` | none — legacy optional `python3 -m nfl.targets --refresh` | **degrade** — usage uses snaps or 1.0 | `--skip-targets` |
| `TARGETS_GANGSTASH_KEY` | `--targets-source=gangstash` and no key / no cache | `nfl/targets.py`, `nfl/gangstash.py` | `GANGSTASH_API_KEY` | **degrade** — usage 1.0 | `--skip-targets` or `--targets-source=lineups` |
| `TARGETS_GANGSTASH` | gangstash `dataset=targets` HTTP, truncated board, empty payload, or missing `player_name` column | `nfl/gangstash_data.py`, `nfl/targets.py` | cache `nfl/data/gangstash-data/` | **stop** | `--targets-source=lineups` |
| `TARGETS_SOURCE` | unknown `--targets-source` | `nfl/targets.py` | — | **stop** | `lineups` or `gangstash` |
| `TARGETS_JOIN` | team code ↔ FanDuel abbrev (Lineups refresh or gangstash `team_fd`) | `nfl/targets.py`, `nfl/teams.py` | — | **stop** on unmapped **team**; unmatched **names** print on stderr / JSON `targets`, not fatal | add the abbrev to `TEAMS`; do not invent player aliases (week-1 BAL: Devontez Walker is absent — see [`targets.md`](targets.md)) |
| `SNAPS_LINEUPS` | Lineups.com RB/WR/TE snap-count pages (public HTML + SSR JSON) | `nfl/snaps.py` | grant: [`lineups-authorization.md`](lineups-authorization.md) | **stop** | omit snaps refresh |
| `SNAPS_CSV` | local `nfl/data/snaps.csv` (or `--snaps-csv`) missing/empty/bad | `nfl/snaps.py` | none — legacy optional `python3 -m nfl.snaps --refresh` | **degrade** — RB usage uses targets or 1.0 | `--skip-snaps` |
| `SNAPS_GANGSTASH_KEY` | `--snaps-source=gangstash` and no key / no cache | `nfl/snaps.py`, `nfl/gangstash.py` | `GANGSTASH_API_KEY` | **degrade** — RB usage uses targets or 1.0 | `--skip-snaps` or `--snaps-source=lineups` |
| `SNAPS_GANGSTASH` | gangstash `dataset=snaps` HTTP, truncated board, empty payload, or missing columns | `nfl/gangstash_data.py`, `nfl/snaps.py` | cache `nfl/data/gangstash-data/` | **stop** | `--snaps-source=lineups` |
| `SNAPS_SOURCE` | unknown `--snaps-source` | `nfl/snaps.py` | — | **stop** | `lineups` or `gangstash` |
| `SNAPS_JOIN` | team code ↔ FanDuel abbrev (Lineups refresh or gangstash `team_fd`) | `nfl/snaps.py`, `nfl/teams.py` | — | **stop** on unmapped **team**; unmatched **names** print on stderr / JSON `snaps`, not fatal | add the abbrev to `TEAMS`; do not invent player aliases |
| `UPLOAD_CSV` | FanDuel upload write/validate | `nfl/upload.py` | contest `FanDuel-NFL-*-entries-upload-template.csv` (or legacy QB-first lineup-upload) in `nfl/data/` | **stop** | omit `--upload` and `--export`; pass `template=`. A template `contest_id` that is not the players CSV contest prints a warning |
| `SIM_INPUTS` | `--sim-inputs` JSON missing or not `{team_stats, targets, snaps}` | `nfl/sim_inputs.py` | local file only | **stop** | omit `--sim-inputs` (gangstash feed, then role shares) |
| `SIM_CALIBRATION` | `--sim-calibration` is not `off`, `all`, or `team` / `level` / `rates` | `nfl/sim.py` | — | **stop** | `--sim-calibration off` (the default) |

## Deferred (not wired this pass)

| id | seam | why deferred |
|----|------|----------------|
| `WEATHER_NWS` | NWS hourly | still too short for Sep 13 kickoffs |
| `INACTIVES_SUNDAY` | Sunday inactives | not ingested |
| `PFF_PRO` | PFF Pro API | not wired |
| `ANYTIME_TD` | anytime-TD prop strings | not scored; counted in `unmapped_props` (`nfl/props.py`) |
| `PROPS_LINEUPS` | Lineups player-prop pages | inventoried only: `https://www.lineups.com/nfl/player-prop-bets/` — Gangstash is the overlay |
| `TEAM_STATS` | gangstash `team_stats` / `team_stats_weekly` | Not an ILP input. `--sim` reads them via `nfl/sim_feed.py` (degrade to role shares when the key and cache are missing). CLI chokes: `TEAM_STATS_GANGSTASH_KEY`, `TEAM_STATS_GANGSTASH` |

Do not scrape FanDuel. Do not `--refresh-props` unless asked (refetches the Gangstash board).

## Stop vs degrade (quick)

- **Must have to score the slate:** CSV, gangstash lines key + fetch + join (`GANGSTASH_API_KEY`, `LINES_GANGSTASH_KEY`). A missing key or a failed fetch stops the run and names gangstash. There is no other lines source and no silent FPPG. `--lines-json` / `--lines-file` replay a simple spread/total file.
- **May skip:** ESPN injuries (`--skip-injuries`), depth (`--skip-depth`, or gangstash depth with no key and no cache → unlisted prior), targets (`--skip-targets`, missing legacy CSV, or gangstash targets with no key and no cache), snaps (`--skip-snaps`, missing legacy CSV, or gangstash snaps with no key and no cache), props (`--skip-props` or missing Gangstash key and no cache).
- **Depth fallbacks:** `--depth-source=ourlads` or `--depth-source=espn` (`DEPTH_ESPN`; often 403). Default is gangstash.

Depth: [`ourlads-depth.md`](ourlads-depth.md).
- **May degrade:** PuLP → greedy (label the lineup).

Props are a **±20% tilt** on the implied score when a volume line joins.
Source and prop-string map: [`player-props.md`](player-props.md).
Missing props is not a lines failure and not a blank choke — see picker
`note` / `prop_status` / **Sources** (join tags) and JSON `slate_status`.
`--slate-status` (or `python3 -m nfl.status`) prints pool + per-source
coverage without solving. **Lead with cash-relevant salary bands**
(strictly greater than: QB $6,500 / WR $5,000 / RB $5,000 / TE $4,500 /
DEF $3,500) — not the full FanDuel pool of $4k fillers. Per-position
expects: skill depth (OurLads/ESPN); RB/WR/TE Lineups targets; RB snaps
(WR/TE snaps reported, not required for the headline); props when not
skipped. DEF: Vegas implied opp / optional prop; no depth/targets/snaps.
Missing relevant names print on stderr (capped) and in JSON `slate_status`.
Full-pool ratios stay on the secondary `all pool` line. Floors are
constants in `nfl/slate_status.py` (`RELEVANT_SALARY_FLOOR`).

## Targets + snaps refresh + usage tilt

Wednesday-style weekly refresh (after the prior week’s games land):

Legacy optional Lineups refresh (only for `--targets-source=lineups` / `--snaps-source=lineups`):

```
python3 -m nfl.targets --refresh
python3 -m nfl.snaps --refresh
```

The optimizer defaults read gangstash instead. Those commands still write `nfl/data/targets.csv` and `nfl/data/snaps.csv` when you want the old CSVs. Formula: [`targets.md`](targets.md), [`snaps.md`](snaps.md).

Grant: [`lineups-authorization.md`](lineups-authorization.md). Missing CSV degrades (`TARGETS_CSV` / `SNAPS_CSV`); name gaps are reported, not a stop.
