# RB/WR/TE targets (usage tilt)

The optimizer default is gangstash (`--targets-source=gangstash`). This file
is the legacy Lineups CSV, selected with `--targets-source=lineups`. Grant:
[`lineups-authorization.md`](lineups-authorization.md). Public Lineups
SSR pages, cached. Do not scrape FanDuel.

Gangstash window share is `sum(targets)/sum(team_targets)`. With
`--targets-weeks` unset, the client omits `week` and keeps every completed
week the API returns. The tilt math does not change. See
[`gangstash.md`](gangstash.md).

Legacy optional refresh (only when you want this CSV):

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

Gangstash (`--targets-source=gangstash`) uses the same join. Pass
`--targets-weeks=1,2` for a multi-week share, or `--targets-week=N` for one
week. With neither, every week the endpoint returns is collapsed. Unmatched
names stay usage 1.0. `--refresh-targets` refetches that cache only.

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

## Week-1 BAL WRs — Devontez Walker is not on Lineups

Lineups week-1 Baltimore WRs in [`targets.csv`](../../data/targets.csv):
Zay Flowers, Rashod Bateman, Ja'Kobi Lane, LaJohntay Wester, **Chris Moore**.
FanDuel lists **Devontez Walker** (BAL WR, Q groin). He is **absent** from
the Lineups feed — do **not** invent an alias (`Jahdae Walker` is CHI).
He stays `targets_status=unmatched` / usage 1.0 (“Lineups name unmatched”).
`--diversity=coverage` downweights unmatched OurLads-only fillers so they
are not cheap unique swaps; it does not invent a join.

## Commands

```
python3 -m nfl.targets --refresh
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv"
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --skip-targets
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --targets-week=1
```
