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

`model_version` is `git rev-parse --short HEAD`. The run log also prints
`sim_efficiency` (default `data`). Each `model=sim` row stores that
mode on `inputs.sim_efficiency`. Board rows do not. Missing sim inputs
fall back to placeholder, the sim is not posted, and the log says the
publish stayed on the board.

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
1.0, not a failed job. Props are the usual ±20% tilt.

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
- read key missing, HTTP error, or a stale cache for lines, depth, targets, snaps, or props
- sim inputs come back with a stale cache when `--sim` is set

## Read back

```
GET .../functions/v1/data?dataset=projections&season=&week=&model=&latest=true|false
GET .../functions/v1/data?dataset=projection_vs_actual&season=&week=
```

Same read header as the other `/data` datasets (`x-api-key: $GANGSTASH_API_KEY`).
`projection_vs_actual` joins `fd_points`.
