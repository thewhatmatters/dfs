# Nightly projections → gangstash

`python3 -m nfl.publish_projections` scores the current NFL week from gangstash
inputs and POSTs the rows. It does not need a FanDuel players CSV and it does
not change `week1_score` or the ILP.

```
python3 -m nfl.publish_projections --refresh --sim 10000
```

That is the nightly command. `--refresh` skips the same-day cache so a dead
read cannot fall back onto an older file. `--sim 10000` also posts
`model=sim`. Those rows use `resolve_sim_inputs` and `simulate_games`
the same way `python3 -m nfl.optimize` does (`--projection-source sim`).
`mean` is the simulated mean; `p10` / `p50` / `p90` are the same draws.
If `simulate_games` does not import, the command still posts the board
and prints one line. A sim-input cache marked stale exits non-zero.

`--dry-run` writes `nfl/data/projections/` (gitignored) and does not POST.
It does not need `GANGSTASH_PROJECTIONS_WRITER_KEY`.

`--as-of <ISO-8601>` is the point in time for the run. A missing offset is
UTC. Omitted, the value is this run's start time, the reads stay on the
current datasets (`game_lines`, `depth_charts`, `/props`, `injuries`,
targets, snaps), and the projection numbers match a publish that does not
record provenance. Out, IR, NA, and suspended players are dropped from the
role and the next charted player is promoted, the same way as on main.
Passed explicitly, lines come from `game_line_snapshots`, props from
`props_snapshots`, depth from `depth_charts_weekly`, and injuries from
`injury_snapshots` (rows with `is_baseline=true` are dropped), all at that
`as_of`. The same drop-and-promote step runs on those snapshot rows.
Targets, snaps, team stats, player stats, and usage do not accept
`as_of`; a sim still uses the current boards. A baseline-only `Out` does
not change a rank or a mean. `input_run_ids` includes the latest succeeded
`nflverse-injuries` collector run at or before `as_of`, the same way it
includes the other feed collectors.

Every successful dry-run and every successful POST writes
`nfl/reports/<season>-w<week>-<YYYY-MM-DD>.manifest.json` (gitignored) and
prints `manifest <path>` on stderr. The path uses the same Central Time
day as the markdown report. Fields:

| field | value |
|-------|--------|
| `git_sha` | full `HEAD` (`unknown` if git fails) |
| `dirty` | true when `git status --porcelain` is non-empty |
| `seed` | `--sim-seed` |
| `draws` | `--sim` |
| `as_of` | UTC ISO-8601 |
| `cli_args` | the argv for this run |
| `datasets` | one object per dataset actually read: `row_count` (normalized rows), `observed_at`, `sha256` (canonical JSON, sorted keys and rows), `untimestamped` |
| `input_run_ids` | latest `succeeded` `collector_runs` whose `finished_at` is at or before `as_of`, one per feed collector, cap 100 |
| `python` | `platform.python_version()` |
| `point_in_time` | true only when `--as-of` was passed |

Feed collectors, in this order: `bettingpros-odds`, `bettingpros-pbcs`,
`nflverse-depth-charts`, `nflverse-depth-charts-weekly`,
`nflverse-injuries`, `nflverse-player-stats`, `nflverse-player-usage`,
`nflverse-snaps`, `nflverse-targets`, `nflverse-team-stats`. A collector
with no succeeded run at or before `as_of` is omitted. `collector_runs`
itself is not a dataset in the manifest.

If any dataset's `observed_at` is more than 36 hours before the run start,
or `untimestamped` is true, stderr and the report footer (when `--report`
is set) include `stale inputs:`. A null `observed_at` is not a stale warning.

`--report` writes `nfl/reports/<season>-w<week>-<YYYY-MM-DD>.md` after a
successful POST or `--dry-run` and prints that path. The same run writes
`nfl/reports/<season>-w<week>-games.json` (team median and p10/p90) when
the sim returned game draws. `python3 -m nfl.report --season --week`
reads the stored `model=sim` rows. It shows those sim scores when the
sidecar `run_at` matches, and gangstash implied totals otherwise.
`--csv` fills a null salary from a FanDuel players list. A player whose whole game has no rows in that file shows `off slate` in the salary cell. A missing player in a listed game still shows `—`.

## Keys

| env | role |
|-----|------|
| `GANGSTASH_API_KEY` | reads: game lines, depth, targets, snaps, props |
| `GANGSTASH_PROJECTIONS_WRITER_KEY` | write only |

Write request:

```
POST https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/projections
x-api-key: $GANGSTASH_PROJECTIONS_WRITER_KEY
{"rows": [ ... ]}
```

The key is the header only. A query-string key is not sent (the API returns
400). A read-only key is 403. GET is 405. Chunks are 5,000 rows. The response
`inserted` / `updated` counts are summed and printed. One `run_at` is shared
by every row and both models. Posting that same `run_at` again updates in
place. A new `run_at` is a new run.

Every row also sends `as_of` and `input_run_ids` (projections writer v3).
The same two values go on every row of the request, including both `board`
and `sim`. `input_run_ids` is a JSON array of collector-run UUIDs (at most
100). CSV dry-runs json-encode that array the same way they encode `inputs`.

`model_version` is `git rev-parse --short HEAD`. The run log prints
`sim_efficiency` after any fallback, so a data run that had no sim
inputs shows `sim_efficiency=placeholder`. Each `model=sim` row stores
that effective mode on `inputs.sim_efficiency`. Board rows do not.
Missing sim inputs fall back to placeholder, the sim is not posted, and
the log says the publish stayed on the board.

`GANGSTASH_API_KEY` has to be set. If it is missing the command exits 1
before it posts. The message names the key and says the job reads game
lines, depth, targets, snaps, and props from gangstash and does not
switch to another source. `--refresh` (the default) does not read a
cache when the key is missing.

## What gets scored

Season and week come from gangstash `game_lines`. The client looks forward
from today (ET), then backward, reads `season` and `week` off that game, and
fetches the full week. `--season` and `--week` together skip the search.

| player | source |
|--------|--------|
| QB, RB, WR, TE | `depth_charts` skill `pos_abb`, best `pos_rank` |
| DEF | one row per team on that week's lines (`position` `D`) |

Targets and snaps use every completed week the API returns for the season
(no hardcoded week list). An empty targets or snaps board is usage factor
1.0, not a failed job. Props are the usual ±20% tilt. The default publish
reads `dataset=injuries` for that week. Out, IR, NA, and suspended players
score 0 and the next charted player takes the vacated role. `--as-of`
reads `injury_snapshots` instead and ignores `is_baseline` rows.

`gsis_id` is sent whenever depth, targets, or snaps have it. The trigger
fills `player_id` and team codes. DEF rows usually have no `gsis_id`; the
summary counts those.

`--csv` joins a FanDuel players list on name + team + position and sets
`salary` plus `inputs.fanduel_id`. Without a CSV, `salary` is null.

`inputs` records source labels (`gs-depth`, `gs-tgt`, `gs-snap`, `gs-props`,
`vegas-dst`), implied total, depth rank, usage factor, and prop factor.

Board rows: `mean` = `week1_score`, `p10` / `p50` / `p90` null.
Sim rows: `mean` / `p10` / `p50` / `p90` from the layered Monte Carlo.
`mean` matches `apply_ilp_objective(..., projection_source="sim")`.

## Loud failures

Exit status is non-zero and stderr starts with `publish projections:`.

- `GANGSTASH_PROJECTIONS_WRITER_KEY` missing (unless `--dry-run`)
- no `game_lines` for the target week, or that cache is stale
- a slate team has no skill depth
- `GANGSTASH_API_KEY` missing: exit 1 immediately. The message names the key, the five read datasets (game lines, depth, targets, snaps, props), and that nothing is posted. There is no second read source, and `--refresh` does not use a cache in that case
- read key present but the HTTP call fails, or a stale cache for lines, depth, targets, snaps, or props
- sim inputs come back with a stale cache when `--sim` is set

## Read back

```
GET .../functions/v1/data?dataset=projections&season=&week=&model=&latest=true|false
GET .../functions/v1/data?dataset=projections&season=&week=&provenance=true
GET .../functions/v1/data?dataset=projection_vs_actual&season=&week=
```

`provenance=true` returns the stored `as_of` and `input_run_ids`.

Same read header as the other `/data` datasets (`x-api-key: $GANGSTASH_API_KEY`).
`projection_vs_actual` joins `fd_points`.
