# Gangstash `/data` datasets

Player props stay on `GET .../functions/v1/props` ([`player-props.md`](player-props.md)).
The other NFL inputs share one Edge Function. Exact response field names are
still to be confirmed; mapping lives in `nfl/gangstash_data.py`.

```
GET https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/data?dataset=<name>&...
x-api-key: $GANGSTASH_API_KEY
```

Response: `{ "data": [ ... ], "truncated": bool }`. `truncated=true` is a
hard stop and is not cached. The key is a header only — never a query param,
never a Supabase service-role key.

Same-day cache: `nfl/data/gangstash-data/YYYY-MM-DD/<dataset>/<query>.json`
(gitignored). A same-day file skips the network. If the live call fails or
the key is unset and an older file for that same query exists, that file is
used and marked stale.

## Flags (defaults are today’s sources)

| flag | choices | default | what it reads |
|------|---------|---------|----------------|
| `--lines-source` | `oddsapi`, `gangstash` | **oddsapi** | Vegas spread + total → implied team totals |
| `--targets-source` | `lineups`, `gangstash` | **lineups** | RB/WR/TE `target_share` for the existing usage tilt |
| `--depth-source` | `ourlads`, `espn`, `gangstash` | **ourlads** | depth rank prior |
| `--targets-weeks` | comma list (`1,2`) | unset | gangstash window only |
| `--targets-week` | N | latest CSV week / that gangstash week | single week |
| `--refresh-targets` | flag | off | bypass the gangstash targets day cache |
| `--lines-json` | path | unset | replay file; wins over `--lines-source` |

`--skip-targets` / `--skip-depth` still skip those joins. RB snap share is
**not** on gangstash. The 70/30 RB blend keeps Lineups snaps
([`snaps.md`](snaps.md)). Projection math, ILP rules, and exposure logic are
unchanged.

Team stats are fetch/cache only (not an optimize input):

```
python3 -m nfl.gangstash_data team-stats --season 2026
python3 -m nfl.gangstash_data team-stats-weekly --season 2026 --week 2
```

## Env

| variable | default | role |
|----------|---------|------|
| `GANGSTASH_API_KEY` | unset | `x-api-key` for props and `/data` |
| `GANGSTASH_DATA_PATH` | `data` | path segment under the functions base, or a full URL |
| `GANGSTASH_TARGETS_DATASET` | `targets` | `dataset=` value |
| `GANGSTASH_GAME_LINES_DATASET` | `game_lines` | `dataset=` value |
| `GANGSTASH_DEPTH_DATASET` | `depth_charts` | `dataset=` value |
| `GANGSTASH_TEAM_STATS_DATASET` | `team_stats` | `dataset=` value |
| `GANGSTASH_TEAM_STATS_WEEKLY_DATASET` | `team_stats_weekly` | `dataset=` value |

## Queries this client sends

| dataset | query | notes |
|---------|--------|--------|
| `targets` | `season` (required), `week` (single or `1,2`), optional `position`, `team` | season is the FanDuel CSV year |
| `game_lines` | `date=YYYY-MM-DD` (slate day) **or** `season` + `week` | optimizer sends **date only** |
| `depth_charts` | optional `team`, `position` | optimizer requests the unfiltered board and keeps slate teams |
| `team_stats` | `season` (required), optional `side`, `team` | not scored |
| `team_stats_weekly` | `season` (this client always sends it), optional `week`, `team` | not scored; confirm season is accepted |

## Assumed response fields (confirm when live)

Mapped in `nfl/gangstash_data.py`. The first name is what we expect. Later
names are aliases if the payload differs.

**`targets`** (one player-week):

| assumed field | aliases | use |
|---------------|---------|-----|
| `player_name` | `name`, `player` | join via `match_key` |
| `team_fd` | `team` | FanDuel abbrev (`JAX`→`JAC`, `WSH`→`WAS`) |
| `position` | `pos` | `RB` / `WR` / `TE` (others skipped) |
| `week` | | week number |
| `targets` | | window sum |
| `target_share` | `targetShare` | 0–1; used only when `team_targets` is missing on a single week |
| `team_targets` | `teamTargets` | denominator |
| `targets_total` | `targetsTotal` | stored, not scored |
| `targets_avg` | `targetsAvg` | stored, not scored |
| `gsis_id` | `gsisId` | kept on the normalized row, not joined |
| `player_id` | `playerId` | kept on the normalized row, not joined |

Window share (what the tilt consumes):

```
target_share = sum(targets) / sum(team_targets)
```

over `--targets-weeks`, or over every week returned when neither
`--targets-weeks` nor `--targets-week` is set. All players in that window
are labeled with the max week so the existing single-week join keeps them.
Unmatched slate RB/WR/TE stay `target_share` empty (usage factor 1.0). The
join report is the same `joined` / `unmatched_lineups` /
`unmatched_slate_rb_wr_te` block.

**`game_lines`:**

| assumed field | aliases | use |
|---------------|---------|-----|
| `home_team_fd` | `home_fd`, `home` | FanDuel home abbrev |
| `away_team_fd` | `away_fd`, `away` | FanDuel away abbrev |
| `spread` | `home_spread`, `spread_home` | home spread; negative = home favorite |
| `total` | `game_total` | game total |
| `moneylines` | `moneyline` | assumed object `{home, away}` |
| `home_moneyline` | `moneyline_home` | used when `moneylines` is absent |
| `away_moneyline` | `moneyline_away` | used when `moneylines` is absent |
| `commence_time` | `commence`, `kickoff` | slate window filter when present |

Implied totals use the same formula as Odds API (`nfl/lines.py`). Rows
outside the slate window are dropped when `commence_time` is present. A
missing slate game is `LINES_GANGSTASH` (stop). No key and no cache is
`LINES_GANGSTASH_KEY` (stop — no silent FPPG).

**`depth_charts`:**

| assumed field | aliases | use |
|---------------|---------|-----|
| `player_name` | `name`, `player` | depth name join |
| `team_fd` | `team` | FanDuel abbrev |
| `position` | `pos` | skill positions only (`QB`/`RB`/`WR`/`TE`, including `LWR`-style labels) |
| `depth_rank` | `rank`, `depth_chart_rank` | 1 = starter. Missing rank fails the ingest |

A slate team with no skill rows is `DEPTH_GANGSTASH` (stop). `--skip-depth`
still leaves the unlisted prior.

**`team_stats` / `team_stats_weekly`:** rows are cached raw. Pass rate, run
rate, success rate, and EPA are **not** read. Do not wire them into
`week1_score` until the field names are confirmed.

## Failure

| choke | when | on failure |
|-------|------|------------|
| `LINES_GANGSTASH_KEY` | `--lines-source=gangstash`, no key, no cache | **stop** |
| `LINES_GANGSTASH` | lines HTTP, truncated board, bad fields, missing slate game | **stop** |
| `TARGETS_GANGSTASH_KEY` | `--targets-source=gangstash`, no key, no cache | **degrade** — usage 1.0 |
| `TARGETS_GANGSTASH` | targets HTTP, truncated board, or bad fields | **stop** |
| `TARGETS_JOIN` | unmapped `team_fd` | **stop** |
| `DEPTH_GANGSTASH` | `--depth-source=gangstash` HTTP, truncated, bad fields, or slate team missing | **stop** |
| `TEAM_STATS_GANGSTASH_KEY` | team-stats CLI, no key, no cache | **stop** (CLI only) |
| `TEAM_STATS_GANGSTASH` | team-stats CLI HTTP or truncated board | **stop** (CLI only) |

Catalog: [`sources.md`](sources.md).
