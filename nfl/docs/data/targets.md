# Lineups RB/WR/TE targets (usage tilt)

Grant: [`lineups-authorization.md`](lineups-authorization.md). Public Lineups
SSR pages, cached. Do not scrape FanDuel.

Wednesday-style weekly refresh (after the prior week’s games land):

```
python3 -m nfl.targets --refresh
python3 -m nfl.snaps --refresh
```

writes [`nfl/data/targets.csv`](../../data/targets.csv)
(`player, team, position, week, targets, target_share, …, source=lineups`).
Cache: `nfl/data/lineups-targets/`. Pages: WR, TE, and
https://www.lineups.com/nfl/targets/running-back/ (same SSR metrics family).

## Optimizer join

`python3 -m nfl.optimize` loads the **latest week** in that CSV (or
`--targets-week=N`) and joins RB/WR/TE pool players via `nfl.names.match_key`
(Jr/Sr/II stripped). No invented aliases. `--targets-csv PATH` overrides
the default file. `--skip-targets` leaves `target_share` empty (RB usage
then uses snaps or 1.0).

Name mismatches are **not fatal**. Stderr + JSON `targets` report:

- `joined` — slate RB/WR/TE that hit a Lineups row
- `unmatched_lineups` — that week's Lineups names on a **slate team** with no pool player (off-slate teams omitted)
- `unmatched_slate_rb_wr_te` — slate RB/WR/TE with no Lineups row

Missing/unreadable CSV prints `choke TARGETS_CSV:` and **degrades** (same
path as `--skip-targets`). Team-map failures on refresh stay
`TARGETS_JOIN` / `TARGETS_LINEUPS` — see [`sources.md`](sources.md).

## Score formula

Vegas implied team total stays the environment. Depth prior stays the
role. Lineups `target_share` is a **±20% usage tilt** vs a depth-conditional
expected share — the same clamp style as volume props. It does **not**
replace implied totals or `POS_FD_SHARE`.

WR/TE use targets only. RB blends snaps (70%, rush role) with targets
(30%, receiving). Combined formula: [`snaps.md`](snaps.md).

```
expected WR: rank1=0.24  rank2=0.16  rank3=0.10  unlisted=0.08
expected TE: rank1=0.18  rank2=0.10  rank3=0.06  unlisted=0.06
expected RB: rank1=0.12  rank2=0.07  rank3=0.04  unlisted=0.04

usage_factor = 1.0                         if no join
             = clamp(target_share / expected, 0.80, 1.20)   WR/TE
             = RB blend (see snaps.md)                      RB

week1_score  = implied_team_total
             × depth_prior(rank)
             × position share
             × usage_factor
             × prop_factor          (±20% when a volume line joins)
```

`targets` (raw count) is attached for the picker/JSON but is not a second
objective. `--skip-targets` or an unmatched name keeps `target_share` empty.

## Commands

```
python3 -m nfl.targets --refresh
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv"
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --skip-targets
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --targets-week=1
```
