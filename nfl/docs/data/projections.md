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

`--sim-mode team` uses the same run to upsert one row per game into
`public.nfl_game_projections`. That write is PostgREST with
`GANGSTASH_SERVICE_ROLE_KEY`. It is not sent to `/functions/v1/projections`
(that function only writes `nfl_player_projections`). `--sim-mode off`
still posts player rows and skips the game table. A dry run writes
`<stem>-game-projections.json` next to the player file.

`--report` writes `nfl/reports/<season>-w<week>-<YYYY-MM-DD>.md` after a
successful POST or `--dry-run` and prints that path. The same run writes
`nfl/reports/<season>-w<week>-games.json` (team median and p10/p90) when
the sim returned game draws. `python3 -m nfl.report --season --week`
reads the stored `model=sim` rows. It shows those sim scores when the
sidecar `run_at` matches, and gangstash implied totals otherwise.
`--csv` fills a null salary from a FanDuel players list. A player whose whole game has no rows in that file shows `off slate` in the salary cell. A missing player in a listed game still shows `—`.

`--slate-csv PATH` (also on `python3 -m nfl.report`) limits the report's games section and every top list to games in that players-list and to players in it (normalized name + team; DEF by team). Top lists backfill from the next eligible player. Salaries and pts/$1k come from the CSV. `--slate-csv auto` uses the newest `nfl/data/FanDuel-NFL-*-players-list.csv` whose filename slate date is today or later in America/Chicago, and ignores entries-upload templates. A missing, empty, malformed, or unmatched file, or a bad filename date, keeps the unfiltered report, prints one warning, and still exits 0. Gangstash rows are the full week either way. Team codes are normalized in the report (JAX→JAC, WSH→WAS, LA→LAR, OAK→LV) so a JAC game score still lists Jacksonville players stored as JAX.

## Keys

| env | role |
|-----|------|
| `GANGSTASH_API_KEY` | reads: game lines, depth, targets, snaps, props |
| `GANGSTASH_PROJECTIONS_WRITER_KEY` | write only (`nfl_player_projections`) |
| `GANGSTASH_SERVICE_ROLE_KEY` | write only (`nfl_game_projections` PostgREST). Not the read key. Never sent to `/functions/v1` |

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

Sim rows store two extra `inputs` fields when the draw has a scoring-TD
count. There is no `nfl_player_projections` column for them in this repo,
and this job cannot migrate gangstash, so they stay on `inputs`:

| field | meaning |
|-------|---------|
| `anytime_td_prob` | Mean over draws of `1 - exp(-λ)`. `λ` is that draw's expected rush TDs plus receiving TDs. The sim does not sample an integer TD, so this is the Poisson chance of at least one score, averaged across worlds. |
| `td_mean` | Mean of those same `λ` values. |

Passing TDs stay on the passer and are not an anytime score. DEF return
TDs are not drawn, so a `D` row omits both fields. A skill player who
only has a role-share point total (no opportunity draw) also omits them.
Board rows omit them. Anytime TD props on the odds board stay unmapped;
these two fields are sim-derived.

## Game rows (`nfl_game_projections`)

Only `--sim-mode team`. One row per game in that sim, same `run_at` and
`model_version` as the player rows. `model` is `sim`.

```
POST https://vmzgpslqoeuqmdchdekm.supabase.co/rest/v1/nfl_game_projections?on_conflict=season,week,season_type,run_at,model,game_id
apikey: $GANGSTASH_SERVICE_ROLE_KEY
Authorization: Bearer $GANGSTASH_SERVICE_ROLE_KEY
Prefer: resolution=merge-duplicates,return=representation
```

The body is a JSON array, not `{"rows": ...}`. `id` and `created_at` are
left to the database defaults. `game_id` has to be an `nfl_games.id` uuid
(week 4 ATL@NO is `89c7243e-cb87-4fa4-b2c7-068be7c16a68`). A non-uuid
`game_lines.game_id` is skipped. The uuid is read from `game_id` when that
value is a uuid, otherwise `nfl_game_id` / `game_uuid` / `id`.

`consensus_home_line` and `consensus_total` are copied from
`nfl_game_lines` where `book=consensus` (home line negative when home is
favored). A feed with no `book` column is already that board. Another
book on the same game is ignored. Means and p10/p50/p90 use the raw
simulated points. Wins, covers, and the total use each draw rounded to
the nearest integer, so an integer line can push. Home covers when
`(home - away) + consensus_home_line > 0`. Over is the rounded sum above
`consensus_total`.

A missing service-role key still posts the player rows and prints that
the game table was not written. A rejected upsert exits non-zero after
the player post.

## Loud failures

Exit status is non-zero and stderr starts with `publish projections:`.

- `GANGSTASH_PROJECTIONS_WRITER_KEY` missing (unless `--dry-run`)
- no `game_lines` for the target week, or that cache is stale
- a slate team has no skill depth
- `GANGSTASH_API_KEY` missing: exit 1 immediately. The message names the key, the five read datasets (game lines, depth, targets, snaps, props), and that nothing is posted. There is no second read source, and `--refresh` does not use a cache in that case
- read key present but the HTTP call fails, or a stale cache for lines, depth, targets, snaps, or props
- sim inputs come back with a stale cache when `--sim` is set
- `--sim-mode team` game upsert is rejected (`nfl_game_projections`). A missing `GANGSTASH_SERVICE_ROLE_KEY` does not fail the player post

## Read back

```
GET .../functions/v1/data?dataset=projections&season=&week=&model=&latest=true|false
GET .../functions/v1/data?dataset=projection_vs_actual&season=&week=
```

Same read header as the other `/data` datasets (`x-api-key: $GANGSTASH_API_KEY`).
`projection_vs_actual` joins `fd_points`.
