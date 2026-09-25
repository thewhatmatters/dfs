# Gangstash `/data` datasets

Player props stay on `GET .../functions/v1/props` ([`player-props.md`](player-props.md)).
The other NFL inputs share one Edge Function. Mapping lives in
`nfl/gangstash_data.py`.

```
GET https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/data?dataset=<name>&...
x-api-key: $GANGSTASH_API_KEY
```

The server also accepts `Authorization: Bearer`. This client sends
`x-api-key` only. Never put the key in the URL. Never send a Supabase
service-role key.

Response: `{ "data": [ ... ], "truncated": bool }`. Each page is 1,000 rows.
`truncated=true` means another page exists. The client follows `offset=1000`,
`offset=2000`, … and caches the joined board. A result still truncated at
20,000 rows is a hard stop and is not cached. `400` is an unknown dataset or
a missing required param. `401` is no key.

Same-day cache: `nfl/data/gangstash-data/YYYY-MM-DD/<dataset>/<query>.json`
(gitignored). A same-day file skips the network. If the live call fails or
the key is unset and an older file for that same query exists, that file is
used and marked stale. The first page omits `offset`; it is not part of the
cache key.

Optional live check (no network otherwise):

```
GANGSTASH_LIVE_SMOKE=1 python3 -m unittest nfl.test_gangstash_data.LiveSmokeTest
```

## Flags (defaults are gangstash)

| flag | choices | default | what it reads |
|------|---------|---------|----------------|
| `--lines-source` | `gangstash` | **gangstash** | Vegas spread + total → implied team totals |
| `--targets-source` | `gangstash`, `lineups` | **gangstash** | RB/WR/TE `target_share` for the existing usage tilt |
| `--snaps-source` | `gangstash`, `lineups` | **gangstash** | RB/WR/TE `snap_share` (RB rush-role tilt) |
| `--depth-source` | `gangstash`, `ourlads`, `espn` | **gangstash** | depth rank prior |
| `--targets-weeks` | comma list (`1,2`) | unset | every completed week the API returns for the season |
| `--snaps-weeks` | comma list (`1,2`) | unset | every completed week the API returns for the season |
| `--targets-week` | N | latest CSV week / that gangstash week | single week |
| `--snaps-week` | N | latest CSV week | Lineups join week |
| `--refresh-targets` | flag | off | bypass the gangstash targets day cache |
| `--refresh-snaps` | flag | off | bypass the gangstash snaps day cache |
| `--lines-json` | path | unset | replay file; wins over `--lines-source` |

Unset `--targets-weeks` / `--snaps-weeks` omit `week`, so the window is whatever completed weeks gangstash has loaded (2026 weeks 1–2 today; week 3 is not loaded yet). A missing `GANGSTASH_API_KEY` with no cache degrades targets, snaps, and depth, and stops lines (no silent FPPG). The lines error names gangstash. The stderr line for the other three names `--targets-source=lineups --snaps-source=lineups --depth-source=ourlads`. Game lines have no fallback.

`--skip-targets` / `--skip-snaps` / `--skip-depth` still skip those joins.
Gangstash `offense_pct` is a 0–1 fraction, the same scale as Lineups
`snap_share` (Lineups `weeksPct / 100`). The RB blend stays 70% snap share /
30% target share ([`snaps.md`](snaps.md)). Projection math, ILP rules, and
exposure logic are unchanged.

Team stats are not an ILP input. `--sim` reads the season offense and defense rows (EPA sums, `neutral_pass_rate`, `proe`) and, when a week window is set, weekly rates. `week1_score` does not. Cache them on their own with:

```
python3 -m nfl.gangstash_data team-stats --season 2026 --side offense
python3 -m nfl.gangstash_data team-stats-weekly --season 2026 --week 1,2
```

## Env

| variable | default | role |
|----------|---------|------|
| `GANGSTASH_API_KEY` | unset | `x-api-key` for props and `/data` |
| `GANGSTASH_LIVE_SMOKE` | unset | `1` runs the optional live contract test |
| `GANGSTASH_DATA_PATH` | `data` | path segment under the functions base, or a full URL |
| `GANGSTASH_TARGETS_DATASET` | `targets` | `dataset=` value |
| `GANGSTASH_GAME_LINES_DATASET` | `game_lines` | `dataset=` value |
| `GANGSTASH_DEPTH_DATASET` | `depth_charts` | `dataset=` value |
| `GANGSTASH_DEPTH_CHARTS_WEEKLY_DATASET` | `depth_charts_weekly` | `dataset=` value |
| `GANGSTASH_TEAM_STATS_DATASET` | `team_stats` | `dataset=` value |
| `GANGSTASH_TEAM_STATS_WEEKLY_DATASET` | `team_stats_weekly` | `dataset=` value |
| `GANGSTASH_SNAPS_DATASET` | `snaps` | `dataset=` value |
| `GANGSTASH_PLAYER_STATS_WEEKLY_DATASET` | `player_stats_weekly` | `dataset=` value |
| `GANGSTASH_CLOSING_LINES_DATASET` | `closing_lines` | `dataset=` value |
| `GANGSTASH_INJURIES_DATASET` | `injuries` | `dataset=` value |
| `GANGSTASH_DST_WEEKLY_DATASET` | `dst_weekly` | `dataset=` value |
| `GANGSTASH_PLAYER_USAGE_DATASET` | `player_usage` | `dataset=` value |

## Queries this client sends

| dataset | query | notes |
|---------|--------|--------|
| `targets` | `season` (required), `week` (single or `1,2`), optional `position` (`WR`/`TE`/`RB`), `team` | season is the FanDuel CSV year. 2026 weeks 1–2 are loaded (640 rows) |
| `game_lines` | `date=YYYY-MM-DD` (ET kickoff) **or** `season` + one `week` | optimizer sends the slate **date** only, unless `--week` is set |
| `closing_lines` | `season` + one `week` | preferred for a past `--week` and for `nfl.backtest`. Live rows use `home_line` (negative = home favored), `total`, `implied_home_total`, `implied_away_total`. `kickoff` may be null. A row that still will not parse is skipped and the week falls through to `game_lines` |
| `depth_charts` | optional `team`, `position` (that is `pos_abb`), `pos_grp`, `season`, `week` | optimizer requests `pos_grp=3WR 1TE`. A chart with no `week` column is the current chart. Live and nightly projections use this dataset |
| `depth_charts_weekly` | `season` required; `week` (one week or a comma list), `team`, `position`, `pos_grp` optional | last chart strictly before kickoff. Only games that have already kicked off. The backtest uses this for the target week and falls back to `depth_charts` when it is missing |
| `injuries` | `season` required; `week`, `team`, `gsis_id`, and `status` optional. Rows carry `player_id` | live. Backtest stamps the week. No-CSV mode uses this feed. A CSV pool still prefers to stay quiet when the fetch fails |
| `team_stats` | `season` (required), `season_type` (default `REG`), optional `side` (`offense`/`defense`), `team` | not scored |
| `team_stats_weekly` | `season` + `week` (single or comma list), optional `team` | backtest pools weeks before the target into team EPA variance. The optimizer still overlays rates on the season board |
| `dst_weekly` | `season` (required), optional `week`, optional `team` | DEF actuals for the backtest. Join is `(season, week, team)`. `fd_points` is the FanDuel score |
| `player_usage` | `season` required; `week`, `team`, `gsis_id`, `position` optional | one row per player-week. The sim feed sends season and the prior-week list only |
| `snaps` | `season` (required), `week` (single or `1,2`), optional `position` (`WR`/`TE`/`RB`), `team` (FD or nflverse; `JAC` and `JAX` both work) | 2026 weeks 1–2 are 2,994 rows (185 RB, 335 WR, 220 TE). `offense_pct` is a 0–1 fraction |

## Response fields

**`targets`** (one player-week). There is no `targets_total` or `targets_avg`;
the client sets `targets_total` to the window sum of `targets` and
`targets_avg` to that sum divided by the number of weeks.

| field | use |
|-------|-----|
| `season`, `week`, `position` | week window; position is `WR` / `TE` / `RB` |
| `player_name` | join via `match_key` |
| `team_fd` | FanDuel abbrev (`JAX`→`JAC`, `WSH`→`WAS` when those codes appear) |
| `targets`, `target_share`, `team_targets` | window share is `sum(targets)/sum(team_targets)` |
| `team_pass_attempts` | stored on the parsed week row, not scored |
| `air_yards_share`, `wopr`, `receptions`, `rec_yards` | not scored |
| `gsis_id`, `player_id` | kept on the normalized row, not joined onto the pool |

Over `--targets-weeks`, or over every week returned when neither
`--targets-weeks` nor `--targets-week` is set. All players in that window
are labeled with the max week so the existing single-week join keeps them.
Unmatched slate RB/WR/TE stay `target_share` empty (usage factor 1.0). The
join report is the same `joined` / `unmatched_lineups` /
`unmatched_slate_rb_wr_te` block. A row with a null or empty `player_name`
is skipped. One stderr line reports how many were skipped. An empty payload,
or a payload with no `player_name` column, is `TARGETS_GANGSTASH`.

**`game_lines`:** `game_id`, `season`, `week`, `commence_time`,
`home_team_fd`, `away_team_fd`, `spread` (home line; negative = home
favored), `total`, `home_moneyline`, `away_moneyline`, `updated_at`.
Moneylines are flat fields, not an object. Implied totals
(`nfl/lines.py`): `implied_home = (total - home_spread) / 2`,
`implied_away = (total + home_spread) / 2`. Rows outside the slate window are
dropped when `commence_time` is present. A missing slate game is
`LINES_GANGSTASH` (stop). No key and no cache is `LINES_GANGSTASH_KEY`
(stop — no silent FPPG).

**`closing_lines`:** same join as `game_lines`. A past week
(`nfl.optimize --week`, `nfl.backtest`) tries this dataset first. Live rows
use `home_team`, `away_team`, `home_line` (negative when home is favored),
`total`, `implied_home_total`,
`implied_away_total`, and `kickoff` (null on the rows seen so far). Implied
totals win when both are present: total is the sum, and home spread is away
implied minus home implied. nflverse-shaped rows may still send
`home_implied_total` / `home_implied_tt` and `spread_line` (positive when
home is favored, stored as `-spread_line`) with `total_line` rather than the
final-score `total`. A plain `spread` column is already negative when home
is favored and is not flipped. A row that still has no spread and total is skipped. If the
close then does not cover the slate, the week uses `game_lines` instead of
raising. Unknown dataset is still printed (`closing_lines: not available
(Unknown dataset)`) and is not a quiet skip. If `game_lines` also fails and
no `--lines-file` was given, the backtest stops unless
`--allow-missing-lines`. A line failure is `choke LINES`, including a
spread/total parse error. It is not `choke PLAYER_STATS_WEEKLY`. When every
kickoff is null, prop snapshots use Sunday 17:00 UTC of that 2026 week
(Thursday would drop Friday–Sunday scrapes). Depth snapshots stay on the
Thursday guess.
`--lines-file` (CSV or JSON) still wins over both. A file with `season`,
`week`, `home_team`, `away_team`, `spread_line`, and `total_line` is an
nflverse schedule: `game_id`, `gameday`, `gametime`, `away_line`,
moneylines, `*_spread_odds`, `under_odds`, `over_odds`, and
`home_implied_tt` / `away_implied_tt` are ignored. On that file, `home_line`
stays ignored because `spread_line` is the schedule column (positive when
home is favored). The live closing API is the path that reads `home_line`
as negative when home is favored. `JAX`→`JAC`, `LA`→`LAR`.
A missing required column, or zero games after the season/week filter, is
an error. A simple file (no nflverse schedule columns) still errors on a
column the reader does not know.

**`injuries`:** live. `season` is required. `week`, `team`, `gsis_id`, and
`status` are optional. Rows carry `player_id`. The backtest stamps the
week onto the pool (id match, then team and name). `O`, `D`, `IR`, and
`NA` are out of the sim's target and rush shares and hand the chart slot
to the next healthy player. `Q` keeps the pre-game projection. A FanDuel
CSV still has its Injury Indicator. A failed injuries fetch on a CSV pool
is not listed as missing. No-CSV mode has no indicator column, so a failed
fetch is `missing: injuries` and does not stop the week. An Unknown dataset
on a CSV pool is still a quiet skip.

**`depth_charts`** (latest ESPN via nflverse): `team`, `team_fd`, `pos_grp`,
`pos_abb`, `pos_name`, `pos_slot`, `pos_rank`, `player_name`, `gsis_id`,
`espn_id`, `player_id`, `snapshot_at`. The optimizer keeps `pos_grp`
`3WR 1TE` and drops the same player in other groups. `pos_rank` is the
depth rank across that position (WR2 = `pos_abb` `WR`, `pos_rank` 2; WR
ranks run 1–8). Non-skill `pos_abb` values are skipped. A duplicate player
keeps the best (lowest) `pos_rank`. A row with a null `player_name`,
`team_fd`, `pos_abb`, or `pos_rank` (an ESPN id that did not match) is
skipped. One stderr line reports how many were skipped. An empty payload, or
a payload missing those columns, is `DEPTH_GANGSTASH` (stop). A slate team
with no skill rows is the same choke. `--skip-depth` still leaves the
unlisted prior.

**`depth_charts_weekly`** is the same chart plus `season`, `week`,
`game_type`, `game_id`, `opponent`, `kickoff_at`, and `team_fd`. Each row
is the last chart strictly before that game's kickoff, so it exists only
after kickoff. `nfl.backtest` uses it for the target week. A missing
weekly payload is `missing: depth_charts_weekly` and the backtest falls
back to `depth_charts`. Live and nightly projections stay on
`depth_charts`. The chart is ESPN via nflverse from game-day morning and
does not list inactives. A few rows have no `player_id`; those match on
name and team. The listed QB1 is the main-pool starter. The hindsight
pool still uses the QB who actually took the snaps.

**`snaps`** (one player-week). `offense_pct` is a 0–1 fraction. The client
does not divide it by 100. Window `snap_share` is
`sum(offense_snaps) / sum(offense_snaps / offense_pct)`, which equals
`offense_pct` on a single week and is the same number Lineups stores after
`weeksPct / 100`. Fields: `season`, `week`, `position`, `player_name`,
`team_fd`, `offense_snaps`, `offense_pct`, `gsis_id`, `player_id`,
`opponent`, `game_id`, `defense_snaps`, `st_snaps`. Only RB/WR/TE are kept.
The join is `match_key` plus FanDuel team, the same name match as targets
(`gsis_id` / `player_id` are kept, not used as the join). A null
`player_name`, `team_fd`, or `offense_pct` is skipped. One stderr line
reports how many were skipped. An empty payload, or a payload missing those
columns, is `SNAPS_GANGSTASH`. No key and no cache is `SNAPS_GANGSTASH_KEY`
(degrade — RB usage uses targets or 1.0).

**`team_stats` / `team_stats_weekly`:** rows are cached raw and are not
read by `week1_score`. Season rows include `team`, `team_fd`, `side`,
`pass_rate`, `neutral_pass_rate`, `proe`, `success_rate`, `epa_per_play`,
`epa_var`, pass and rush `success_rate` and `epa_per_play`, `early_down_*`,
`explosive_rate`, `third_down_rate`, `red_zone_td_rate`, and raw `n` /
`epa_sum` / `epa_sq_sum` (all, pass, and rush). Weekly rows add `week`,
`opponent`, and `game_id`. Both sides also carry pace and efficiency:
`pace_games`, `pace_plays`, `play_seconds`, `pace_neutral_plays`,
`neutral_play_seconds`, `rush_yards`, `net_pass_yards`, `air_yards`,
`plays_per_game`, `seconds_per_play`, `neutral_seconds_per_play`,
`yards_per_carry`, `yards_per_dropback`, `yards_per_pass_attempt`,
`sack_rate`, `air_yards_per_attempt`. A defense row is what that defense
allowed, and its `sack_rate` is sacks generated. `seconds_per_play` is
`play_seconds / timed_plays` and `neutral_seconds_per_play` is
`neutral_play_seconds / neutral_timed_plays`. League offense averages are
29.80 overall and 32.34 neutral. The sim blends those with plays per game
for team play volume and clamps the pace leg to ±15%.

**`player_usage`** (one player-week): `season`, `week`, `season_type`,
`gsis_id`, `player_id`, `team`, `team_fd`, `position`, `targets`,
`receiving_air_yards` (can be negative), `target_share` (0 for linemen),
`air_yards_share`, `wopr`, `carries`, `rz_targets`, `rz_carries` (inside
the 20), `gl_carries` (inside the 5), `rz_receiving_tds`, `rz_rushing_tds`.
The sim uses it for target share, aDOT, and red-zone / goal-line TD rates.
Weeks before the backtest target only.

## Failure

| choke | when | on failure |
|-------|------|------------|
| `LINES_GANGSTASH_KEY` | `--lines-source=gangstash`, no key, no cache | **stop** |
| `LINES_GANGSTASH` | lines HTTP (including 400/401), truncated board past the cap, bad fields, missing slate game | **stop** |
| `TARGETS_GANGSTASH_KEY` | `--targets-source=gangstash`, no key, no cache | **degrade** — usage 1.0 |
| `TARGETS_GANGSTASH` | targets HTTP, truncated board past the cap, empty payload, or missing `player_name` column | **stop** |
| `TARGETS_JOIN` | unmapped `team_fd` | **stop** |
| `DEPTH_GANGSTASH_KEY` | `--depth-source=gangstash`, no key, no cache | **degrade** — unlisted prior |
| `DEPTH_GANGSTASH` | `--depth-source=gangstash` HTTP, truncated, empty payload, missing columns, or slate team missing | **stop** |
| `SNAPS_GANGSTASH_KEY` | `--snaps-source=gangstash`, no key, no cache | **degrade** — RB usage uses targets or 1.0 |
| `SNAPS_GANGSTASH` | snaps HTTP, truncated board past the cap, empty payload, or missing columns | **stop** |
| `SNAPS_JOIN` | unmapped `team_fd` | **stop** |
| `TEAM_STATS_GANGSTASH_KEY` | team-stats CLI, no key, no cache | **stop** (CLI only) |
| `TEAM_STATS_GANGSTASH` | team-stats CLI HTTP or truncated board | **stop** (CLI only) |

Catalog: [`sources.md`](sources.md).

Nightly board (and optional sim) rows are written with a separate key.
See [`projections.md`](projections.md).

```
POST .../functions/v1/projections
x-api-key: $GANGSTASH_PROJECTIONS_WRITER_KEY
```
