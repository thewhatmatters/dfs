# Nightly projections → gangstash

`python3 -m nfl.publish_projections` scores the current NFL week from gangstash
inputs and POSTs the rows. It does not need a FanDuel players CSV and it does
not change `week1_score` or the ILP.

```
python3 -m nfl.publish_projections --refresh --sim 10000
```

That is the nightly command. `--refresh` skips the same-day cache so a dead
read cannot fall back onto an older file. A publish gate runs after the
slate load and before the sim or any POST. `--sim 10000` also posts
`model=sim`. Those rows use `resolve_sim_inputs` and `simulate_games`
the same way `python3 -m nfl.optimize` does (`--projection-source sim`).
`mean` is the simulated mean; `p10` / `p50` / `p90` are the same draws.
If `simulate_games` does not import, the command still posts the board
and prints one line. A sim-input cache marked stale exits non-zero.

## Publish gate

The gate refuses the run (exit 1, one stderr line per failure) when an
input is stale or coverage is incomplete. It does not change scoring, the
sim, or the optimizer. `--gate-only` runs the checks and exits without a
sim or a POST, and it does not need the writer key.

Freshness and load status come from `dataset=collector_runs`
(`public.collector_runs`: `run_id`, `collector`, `started_at`,
`finished_at`, `status`, `row_counts`, `error`, `host`, `git_sha`, `args`).
`status` is `running`, `succeeded`, `partial`, or `failed`. A missing or
unknown `status` blocks. Age is the latest `finished_at` among
`status=succeeded` for that collector. The latest load is the newest row
by `started_at`: `partial` and `failed` block, and `running` blocks when
`started_at` is more than 30 minutes ago. `updated_at`, `as_of`, and the
local cache file's mtime are not freshness. A same-day cache whose last
succeeded depth run is four days old still blocks.

Collector names (one map in `nfl/publish_gate.py`, `COLLECTORS`):

| input | collector | default max age |
|-------|-----------|-----------------|
| lines | `bettingpros-odds` | 26h |
| props | `bettingpros-pbcs` | 26h |
| injuries | `nflverse-injuries` | 26h |
| depth charts | `nflverse-depth-charts` | 72h |

Both depth collectors are checked. Team coverage uses the current
`depth_charts` rows. `depth_charts_weekly` is freshness and load status
only, because that dataset omits games that have not kicked off.

Coverage is read from the data rows. Expected teams are the home and away
clubs on that week's games, so a bye is not required. With no byes, all
32 must be present. Each of those teams needs a line (spread and total),
at least one injury row, and at least one depth-chart row. A load that
misses teams names them and blocks (`injuries missing 9 teams: ARI, ...`).

`player_stats_weekly`, `player_usage`, `snaps`, and `targets` must include
the previous completed week. Week 1 has no previous week, so that check
is skipped.

Defaults are flags: `--lines-max-age-hours` (26), `--props-max-age-hours`
(26), `--injuries-max-age-hours` (26), `--depth-max-age-hours` (72),
`--running-max-minutes` (30).

`--allow-stale` and `--skip-gate` publish anyway. Every failed check is
printed as `GATE WARNING:` and copied into the report header. The header
always lists each collector's age and the lines / injuries / depth
coverage result.

The older `cache_stale` stop still applies when a live read fails and an
older cache file is the fallback. That is separate from this gate.

`--dry-run` writes `nfl/data/projections/` (gitignored) and does not POST.
It does not need `GANGSTASH_PROJECTIONS_WRITER_KEY`. The gate still runs.

`--sim-mode team` uses the same run to POST one row per game. That is a
second request to `/functions/v1/projections` with the writer key and
body `{"game_projections": [...]}`, after the player `rows` request, with
the same `run_at`. `--sim-mode off` still posts player rows and skips
the game request. A dry run writes `<stem>-game-projections.json` next
to the player file.

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

An explicit `--as-of` does not replace the nightly files. The manifest,
the `--report` markdown, and the games sidecar use
`<season>-w<week>-asof-<YYYYMMDDTHHMMZ>` (for example
`2026-w3-asof-20260925T1043Z.manifest.json`, `.md`, and `-games.json`).
The default names stay `<season>-w<week>-<YYYY-MM-DD>.manifest.json`,
`<season>-w<week>-<YYYY-MM-DD>.md`, and `<season>-w<week>-games.json`.

Every successful dry-run and every successful POST writes the manifest
(gitignored) and prints `manifest <path>` on stderr. The default path
uses the same Central Time day as the markdown report. Fields:

| field | value |
|-------|--------|
| `git_sha` | full `HEAD` (`unknown` if git fails) |
| `dirty` | true when `git status --porcelain` is non-empty |
| `seed` | `--sim-seed` |
| `draws` | `--sim` |
| `as_of` | UTC ISO-8601 |
| `cli_args` | the argv for this run |
| `datasets` | one object per dataset actually read: `row_count` (normalized rows), `observed_at`, `sha256` (canonical JSON, sorted keys and rows), `untimestamped`. When `--as-of` was passed, each object also has `point_in_time` and `after_as_of` |
| `input_run_ids` | latest `succeeded` `collector_runs` whose `finished_at` is at or before `as_of`, one per feed collector, cap 100. Empty when that read fails |
| `python` | `platform.python_version()` |
| `point_in_time` | true only when `--as-of` was passed |
| `current_reads` | datasets that were read live during an explicit `--as-of` run (`targets`, `snaps`, and, when `--sim` captured them, `team_stats`, `player_usage`, `player_stats_weekly`). Empty on the default path |
| `warnings` | the same lines printed on stderr (collector failure, empty point-in-time injuries, current reads, staleness, `after_as_of`) |

Feed collectors, in this order: `bettingpros-odds`, `bettingpros-pbcs`,
`nflverse-depth-charts`, `nflverse-depth-charts-weekly`,
`nflverse-injuries`, `nflverse-player-stats`, `nflverse-player-usage`,
`nflverse-snaps`, `nflverse-targets`, `nflverse-team-stats`. A collector
with no succeeded run at or before `as_of` is omitted. `collector_runs`
itself is not a dataset in the manifest.

The `collector_runs` read is bounded to the UTC days covering the 48
hours before `as_of` (`date_from` / `date_to`). It does not page the
whole log. If that read fails or times out, the command logs
`collector_runs unavailable (...); input_run_ids empty`, stores `[]`,
and still posts. Provenance never fails the nightly publish.

`/props` returns no `meta`. On the default path, `observed_at` for
`props` is the newest `scraped_at` in the rows, and the 36-hour check
uses that stamp. A server `observed_at`, when present, wins.

If any dataset's `observed_at` is more than 36 hours before the clock
the check uses, or `untimestamped` is true, stderr and the report
footer (when `--report` is set) include `stale inputs:`. The default
path measures 36 hours from the run start. An explicit `--as-of`
measures 36 hours from that `as_of`. A null `observed_at` is not a
stale warning. An `observed_at` later than an explicit `as_of` is
warned and stored as `after_as_of: true`.

An explicit `--as-of` also warns, and records in `warnings`, when
`injury_snapshots` has no usable rows after baseline rows are dropped:
`no point-in-time injuries at as_of; injuries NOT applied`. Injuries
are still passed through the same drop-and-promote step (a no-op when
the gangstash rows are empty). The same warning lists the current
reads: `current reads (not point-in-time): ...`.

`--report` writes `nfl/reports/<season>-w<week>-<YYYY-MM-DD>.md` after a
successful POST or `--dry-run` and prints that path. An explicit
`--as-of` writes `<season>-w<week>-asof-<YYYYMMDDTHHMMZ>.md` instead.
The same run writes `nfl/reports/<season>-w<week>-games.json` (team
median and p10/p90) when the sim returned game draws, or the matching
`asof-` sidecar when `--as-of` was passed. `python3 -m nfl.report`
still reads the nightly sidecar. `python3 -m nfl.report --season --week`
reads the stored `model=sim` rows. It shows those sim scores when the
sidecar `run_at` matches, and gangstash implied totals otherwise.
`--csv` fills a null salary from a FanDuel players list. A player whose whole game has no rows in that file shows `off slate` in the salary cell. A missing player in a listed game still shows `—`.

`--slate-csv PATH` (also on `python3 -m nfl.report`) limits the report's games section and every top list to games in that players-list and to players in it (normalized name + team; DEF by team). Top lists backfill from the next eligible player. Salaries and pts/$1k come from the CSV. `--slate-csv auto` uses the newest `nfl/data/FanDuel-NFL-*-players-list.csv` whose filename slate date is today or later in America/Chicago, and ignores entries-upload templates. A missing, empty, malformed, or unmatched file, or a bad filename date, keeps the unfiltered report, prints one warning, and still exits 0. Gangstash rows are the full week either way. Team codes are normalized in the report (JAX→JAC, WSH→WAS, LA→LAR, OAK→LV) so a JAC game score still lists Jacksonville players stored as JAX.

## Keys

| env | role |
|-----|------|
| `GANGSTASH_API_KEY` | reads: game lines, depth, targets, snaps, props |
| `GANGSTASH_PROJECTIONS_WRITER_KEY` | write only. Player `rows` and team-mode `game_projections`, both on `/functions/v1/projections` |

Write request:

```
POST https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/projections
x-api-key: $GANGSTASH_PROJECTIONS_WRITER_KEY
{"rows": [ ... ]}
```

Team mode sends game rows in a second POST to that URL:

```
{"game_projections": [ ... ]}
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

## Availability

Randy, 2026-10-09: a player who only missed practice can still play.
Practice designations (DNP, Limited, Full) and Questionable keep their
normal projection. Nothing in this pipeline was zeroing those rows; the
injury feed's `practice_status` is not a game status.

Out, IR, and the house out codes (`IR` / `NA` on `FANDUEL_NFL.out_codes`)
project 0. Doubtful does too, unless `--no-zero-doubtful`. The vacated
chart slot goes to the next player at that position (same handoff as
before: they keep the higher of their own target/snap share and the
vacated share). In the sim, that player's target share is added to the
next active catcher at the same position, rush and snap weight stays
inside the remaining running backs, and the next active QB inherits the
starter role (`passing_qb`, the Mayfield → Jalon Daniels path). Team
target and rush totals are unchanged; receiving yards still scale to the
pass anchor.

```
python3 -m nfl.publish_projections --availability-rule ruled-out
python3 -m nfl.publish_projections --no-zero-doubtful
python3 -m nfl.publish_projections --availability-rule legacy
```

`ruled-out` is the default. `legacy` is the previous pool for the same
seed: Doubtful stays at full value (the sim copy has a blank injury),
and Out/IR/NA are dropped before the draw instead of folded onto the
next man. `--zero-doubtful` is ignored on `legacy`. The optimizer's ILP
and scoring are unchanged.

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

Sim rows store extra `inputs` fields when the draw has a scoring-TD
count. There is no `nfl_player_projections` column for them in this repo,
and this job cannot migrate gangstash, so they stay on `inputs`.

The sim's per-draw TD component is an expectation (targets × rate, or a
scaled anchor). Fantasy points still use that expectation. The histogram
is a separate Poisson sample of it, on a second `random.Random` seeded
with the sim seed. That generator is not the one that draws scores, so
`mean` / `p10` / `p50` / `p90` do not move.

| field | meaning |
|-------|---------|
| `anytime_td_prob` | `1 - td_0 / n_draws`. |
| `td_mean` | Mean of the rush + receiving expectations (not the samples). |
| `td_0`, `td_1`, `td_2`, `td_3plus` | Counts of Poisson samples of those expectations. They sum to `n_draws`. `3plus` is 3 or more. |
| `n_draws` | Number of draws in the tally. |

When a player's expectation barely moves, `anytime_td_prob` sits within
Monte Carlo noise of `1 - exp(-td_mean)`.

QBs also sample the passing TDs the scorer used. In opportunity mode
that expectation is the sum of the team's receiving TD expectations
that draw. On the fallback path it is the pass-TD anchor times the
score scale. The pass sample is a second Poisson on the same side RNG.
`total_td_*` counts rush+rec sample plus pass sample. Non-QBs omit
these. `td_*` stays rush + receiving either way.

| field | meaning |
|-------|---------|
| `pass_td_mean` | Mean of the passing-TD expectations (not the samples). |
| `total_td_mean` | Mean of rush + receiving + passing expectations. |
| `total_td_0`, `total_td_1`, `total_td_2`, `total_td_3plus` | Counts of the summed integer samples. They sum to `n_draws`. |

DEF return TDs are not drawn, so a `D` row omits the block. A skill
player who only has a role-share point total (no opportunity draw) also
omits it. Board rows omit it. Anytime TD props on the odds board stay
unmapped; these fields are sim-derived.

## Game rows (`nfl_game_projections`)

Only `--sim-mode team`. One row per game in that sim, same `run_at` and
`model_version` as the player rows. `model` is `sim`. The request goes
out after the player rows, to the same URL, with the writer key:

```
POST https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/projections
x-api-key: $GANGSTASH_PROJECTIONS_WRITER_KEY
{"game_projections": [ ... ]}
```

The response adds `game_inserted`, `game_updated`, and `game_upserted`.
A 400 lists per-row errors with `source: "game_projections"`. That error
is printed and the command exits non-zero. The player rows from the
first request stay posted.

`id` and `created_at` are left to the database defaults. `game_id` has
to be an `nfl_games.id` uuid (week 4 ATL@NO is
`89c7243e-cb87-4fa4-b2c7-068be7c16a68`). A non-uuid `game_lines.game_id`
is skipped. The uuid is read from `game_id` when that value is a uuid,
otherwise `nfl_game_id` / `game_uuid` / `id`.

`season_type` is `REG` or `POST`. `home_wins + away_wins + ties` equals
`n_draws`. `home_win_prob` and `away_win_prob` are in `[0, 1]`. Percentile
bands are ordered `p10 <= p50 <= p90`. Team codes on the wire are
`JAX`, `LA`, and `WAS` (`JAC`, `LAR`, and `WSH` are translated). Other
codes are unchanged.

`consensus_home_line` and `consensus_total` are copied from
`nfl_game_lines` where `book=consensus` (home line negative when home is
favored). A feed with no `book` column is already that board. Another
book on the same game is ignored. Those fields, and the cover / over
probabilities, are omitted when the line is missing. Means and
p10/p50/p90 use the raw simulated points. Wins, covers, and the total
use each draw rounded to the nearest integer, so an integer line can
push. Home covers when `(home - away) + consensus_home_line > 0`. Over
is the rounded sum above `consensus_total`.

## Loud failures

Exit status is non-zero and stderr starts with `publish projections:`.

- `GANGSTASH_PROJECTIONS_WRITER_KEY` missing (unless `--dry-run`)
- no `game_lines` for the target week, or that cache is stale
- a slate team has no skill depth
- `GANGSTASH_API_KEY` missing: exit 1 immediately. The message names the key, the five read datasets (game lines, depth, targets, snaps, props), and that nothing is posted. There is no second read source, and `--refresh` does not use a cache in that case
- read key present but the HTTP call fails, or a stale cache for lines, depth, targets, snaps, or props
- sim inputs come back with a stale cache when `--sim` is set
- `--sim-mode team` game request is rejected (`source: game_projections`). Player rows from the first request stay posted

## Read back

```
GET .../functions/v1/data?dataset=projections&season=&week=&model=&latest=true|false
GET .../functions/v1/data?dataset=projections&season=&week=&provenance=true
GET .../functions/v1/data?dataset=projection_vs_actual&season=&week=
```

`provenance=true` returns the stored `as_of` and `input_run_ids`.

Same read header as the other `/data` datasets (`x-api-key: $GANGSTASH_API_KEY`).
`projection_vs_actual` joins `fd_points`.
