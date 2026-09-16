# Lineups WR/TE targets (usage tilt)

Refresh (public Lineups pages, cached):

```
python3 -m nfl.targets --refresh
```

writes [`nfl/data/targets.csv`](../../data/targets.csv)
(`player, team, position, week, targets, target_share, …, source=lineups`).
Cache: `nfl/data/lineups-targets/`. ToS risk and Cloudflare 403 notes live
in `nfl/targets.py`. Do not scrape FanDuel.

## Optimizer join

`python3 -m nfl.optimize` loads the **latest week** in that CSV (or
`--targets-week=N`) and joins WR/TE pool players — including TE-eligible
FanDuel `TE` / `WR` — via `nfl.names.match_key` (Jr/Sr/II stripped).
No invented aliases. `--targets-csv PATH` overrides the default file.
`--skip-targets` leaves the usage factor at **1.0** (current implied×depth
path).

Name mismatches are **not fatal**. Stderr + JSON `targets` report:

- `joined` — slate WR/TE that hit a Lineups row
- `unmatched_lineups` — that week's Lineups names on a **slate team** with no pool player (off-slate teams omitted)
- `unmatched_slate_wr_te` — slate WR/TE with no Lineups row

Missing/unreadable CSV prints `choke TARGETS_CSV:` and **degrades** (same
path as `--skip-targets`). Team-map failures on refresh stay
`TARGETS_JOIN` / `TARGETS_LINEUPS` — see [`sources.md`](sources.md).

## Score formula

Vegas implied team total stays the environment. Depth prior stays the
role. Lineups `target_share` is a **±20% usage tilt** vs a depth-conditional
expected share — the same clamp style as volume props. It does **not**
replace implied totals or `POS_FD_SHARE`.

```
expected WR: rank1=0.24  rank2=0.16  rank3=0.10  unlisted=0.08
expected TE: rank1=0.18  rank2=0.10  rank3=0.06  unlisted=0.06

usage_factor = 1.0                         if no join / not WR|TE
             = clamp(target_share / expected, 0.80, 1.20)

week1_score  = implied_team_total
             × depth_prior(rank)
             × position share
             × usage_factor
             × prop_factor          (±20% when a volume line joins)
```

`targets` (raw count) is attached for the picker/JSON but is not a second
objective. QB / RB / DST are unchanged. `--skip-targets` or an unmatched
name keeps `usage_factor = 1.0`.

## Commands

```
python3 -m nfl.targets --refresh
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv"
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --skip-targets
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --targets-week=1
```
