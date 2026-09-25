# Lineups RB/WR/TE snap counts (usage tilt)

Grant: [`lineups-authorization.md`](lineups-authorization.md). Public Lineups
SSR pages, cached. Do not scrape FanDuel.

The optimizer default is gangstash (`--snaps-source=gangstash`). This CSV is
the legacy Lineups path (`--snaps-source=lineups`).

Legacy optional refresh (only when you want this CSV):

```
python3 -m nfl.targets --refresh
python3 -m nfl.snaps --refresh
```

`python3 -m nfl.snaps --refresh` writes [`nfl/data/snaps.csv`](../../data/snaps.csv)
(`player, team, position, week, snaps, snap_share, …, source=lineups`).
Cache: `nfl/data/lineups-snaps/` (JSON preferred; Mac urllib may
Cloudflare-block — same fallback as targets).

Pages:

- https://www.lineups.com/nfl/snap-counts/running-back-rb-snap-counts/
- https://www.lineups.com/nfl/snap-counts/wide-receiver-wr-snap-counts/
- https://www.lineups.com/nfl/snap-counts/tight-end-te-snap-counts/

Same SSR blob as targets: `<script type=application/json class=sc-sports-nfl-metrics>`
with `metric:"snaps"`. Row fields: `name`, `team` (full name), `position`,
`weeks[18]`, `weeksPct[18]` (snap %), `total`, `average`, `teamSnapPct`.

## Optimizer join

Default source is gangstash. `python3 -m nfl.optimize --snaps-source=lineups`
loads the **latest week** in that CSV (or `--snaps-week=N`) and joins RB/WR/TE
pool players via `nfl.names.match_key`. No invented aliases. `--snaps-csv PATH`
overrides the default file. `--skip-snaps` leaves snap fields empty (RB usage
then uses targets or 1.0).

`--snaps-source=gangstash` reads `dataset=snaps` instead of this CSV.
`offense_pct` is already a 0–1 fraction (Lineups stores `weeksPct / 100` as
the same `snap_share`). A window (`--snaps-weeks 1,2`, or `--targets-weeks`
when snaps weeks are omitted, or every week returned) collapses to
`sum(offense_snaps) / sum(offense_snaps / offense_pct)`. See
[`gangstash.md`](gangstash.md).

Name mismatches are **not fatal**. Stderr + JSON `snaps` report:

- `joined` — slate RB/WR/TE that hit a Lineups row
- `unmatched_lineups` — that week's Lineups names on a **slate team** with no pool player
- `unmatched_slate_rb_wr_te` — slate RB/WR/TE with no Lineups row

Missing/unreadable CSV prints `choke SNAPS_CSV:` and **degrades** (same
path as `--skip-snaps`). Team-map failures on refresh stay
`SNAPS_JOIN` / `SNAPS_LINEUPS` — see [`sources.md`](sources.md).

## Score formula

Vegas implied team total stays the environment. Depth prior stays the
role. Lineups `snap_share` is the **RB rush-role** ±20% tilt vs a
depth-conditional expected snap share. RB `target_share` is a receiving
tilt. The two are **blended** (70% snaps / 30% targets), then clamped
again to ±20% — not two stacked clamps (that would be ±36%).

WR/TE snaps **load and attach** but do **not** enter `usage_factor`
(targets already measure receiving). Do not double-count.

```
expected RB snaps:   rank1=0.65  rank2=0.30  rank3=0.15  unlisted=0.10
expected RB targets: rank1=0.12  rank2=0.07  rank3=0.04  unlisted=0.04

snap_factor   = 1.0 if no snap join else clamp(snap_share / expected, 0.80, 1.20)
target_factor = 1.0 if no target join else clamp(target_share / expected, 0.80, 1.20)

RB usage     = snap_factor                         if snaps only
             = target_factor                       if targets only
             = clamp(0.70×snap + 0.30×target, 0.80, 1.20)  if both
             = 1.0                                 if neither

WR/TE usage  = target_factor only                  (snaps stored, ignored)

week1_score  = implied_team_total
             × depth_prior(rank)
             × position share
             × usage_factor
             × prop_factor          (±20% when a volume line joins)
```

`snaps` (raw count) is attached for the picker/JSON but is not a second
objective. `--skip-snaps` or an unmatched name keeps snap fields empty.

## Next scrape candidate (not this pass)

Lineups player-prop pages look like
[`https://www.lineups.com/nfl/player-prop-bets/`](https://www.lineups.com/nfl/player-prop-bets/).
Inventory only — gangstash props stay the live overlay. Do not implement
a Lineups props ingest until asked.

## Commands

```
python3 -m nfl.snaps --refresh
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv"
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --skip-snaps
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --snaps-week=1
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --snaps-source=gangstash --snaps-weeks=1,2
```
