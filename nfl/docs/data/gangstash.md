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

## Flags (defaults are today’s sources)

| flag | choices | default | what it reads |
|------|---------|---------|----------------|
| `--lines-source` | `oddsapi`, `gangstash` | **oddsapi** | Vegas spread + total → implied team totals |
| `--targets-source` | `lineups`, `gangstash` | **lineups** | RB/WR/TE `target_share` for the existing usage tilt |
| `--snaps-source` | `lineups`, `gangstash` | **lineups** | RB/WR/TE `snap_share` (RB rush-role tilt) |
| `--depth-source` | `ourlads`, `espn`, `gangstash` | **ourlads** | depth rank prior |
| `--targets-weeks` | comma list (`1,2`) | unset | gangstash target window only |
| `--snaps-weeks` | comma list (`1,2`) | unset | gangstash snap window; if unset, uses `--targets-weeks`, else every week returned |
| `--targets-week` | N | latest CSV week / that gangstash week | single week |
| `--snaps-week` | N | latest CSV week | Lineups join week |
| `--refresh-targets` | flag | off | bypass the gangstash targets day cache |
| `--refresh-snaps` | flag | off | bypass the gangstash snaps day cache |
| `--lines-json` | path | unset | replay file; wins over `--lines-source` |

`--skip-targets` / `--skip-snaps` / `--skip-depth` still skip those joins.
Gangstash `offense_pct` is a 0–1 fraction, the same scale as Lineups
`snap_share` (Lineups `weeksPct / 100`). The RB blend stays 70% snap share /
30% target share ([`snaps.md`](snaps.md)). Projection math, ILP rules, and
exposure logic are unchanged.

Team stats are fetch/cache only (not an optimize input):

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
| `GANGSTASH_TEAM_STATS_DATASET` | `team_stats` | `dataset=` value |
| `GANGSTASH_TEAM_STATS_WEEKLY_DATASET` | `team_stats_weekly` | `dataset=` value |
| `GANGSTASH_SNAPS_DATASET` | `snaps` | `dataset=` value |

## Queries this client sends

| dataset | query | notes |
|---------|--------|--------|
| `targets` | `season` (required), `week` (single or `1,2`), optional `position` (`WR`/`TE`/`RB`), `team` | season is the FanDuel CSV year. 2026 weeks 1–2 are loaded (640 rows) |
| `game_lines` | `date=YYYY-MM-DD` (ET kickoff) **or** `season` + one `week` | optimizer sends the slate **date** only. Week 3 is 16 games |
| `depth_charts` | optional `team`, `position` (that is `pos_abb`), `pos_grp` | optimizer requests `pos_grp=3WR 1TE` |
| `team_stats` | `season` (required), `season_type` (default `REG`), optional `side` (`offense`/`defense`), `team` | not scored |
| `team_stats_weekly` | `season` + `week` (single or comma list), optional `team` | not scored. Adds `week`, `opponent`, `game_id` on each row |
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
Moneylines are flat fields, not an object. Implied totals use the same
formula as the Odds API (`nfl/lines.py`). Rows outside the slate window are
dropped when `commence_time` is present. A missing slate game is
`LINES_GANGSTASH` (stop). No key and no cache is `LINES_GANGSTASH_KEY`
(stop — no silent FPPG).

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
`opponent`, and `game_id`.

## Failure

| choke | when | on failure |
|-------|------|------------|
| `LINES_GANGSTASH_KEY` | `--lines-source=gangstash`, no key, no cache | **stop** |
| `LINES_GANGSTASH` | lines HTTP (including 400/401), truncated board past the cap, bad fields, missing slate game | **stop** |
| `TARGETS_GANGSTASH_KEY` | `--targets-source=gangstash`, no key, no cache | **degrade** — usage 1.0 |
| `TARGETS_GANGSTASH` | targets HTTP, truncated board past the cap, empty payload, or missing `player_name` column | **stop** |
| `TARGETS_JOIN` | unmapped `team_fd` | **stop** |
| `DEPTH_GANGSTASH` | `--depth-source=gangstash` HTTP, truncated, empty payload, missing columns, or slate team missing | **stop** |
| `SNAPS_GANGSTASH_KEY` | `--snaps-source=gangstash`, no key, no cache | **degrade** — RB usage uses targets or 1.0 |
| `SNAPS_GANGSTASH` | snaps HTTP, truncated board past the cap, empty payload, or missing columns | **stop** |
| `SNAPS_JOIN` | unmapped `team_fd` | **stop** |
| `TEAM_STATS_GANGSTASH_KEY` | team-stats CLI, no key, no cache | **stop** (CLI only) |
| `TEAM_STATS_GANGSTASH` | team-stats CLI HTTP or truncated board | **stop** (CLI only) |

Catalog: [`sources.md`](sources.md).
