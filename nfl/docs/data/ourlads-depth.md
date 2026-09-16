# OurLads NFL depth charts

Authorized ingest. Grant and scrape conditions:
[`ourlads-authorization.md`](ourlads-authorization.md).

HTTP only: Scrapling **Fetcher** (`curl_cffi`). Identified `User-Agent` +
`From` email. `stealthy_headers=False`, `impersonate=None`. No
DynamicFetcher / StealthyFetcher, no proxy rotation. If the depth table is
missing from the raw HTML, fail loud — do not auto-switch to a browser.

Default depth path for `nfl.optimize` is OurLads. ESPN
`site.api.../depthcharts` is optional (`--depth-source=espn`) and often 403.

## Join

1. Unique `Team` / `Opponent` on the FanDuel CSV → OurLads key
   (`nfl/ourlads.py` `OURLADS_KEY`). Unmapped abbrevs fail loud.
   Careful aliases: FanDuel `JAC` → OurLads `JAX`; FanDuel `ARI` → OurLads
   `ARZ`; FanDuel `WAS` is OurLads `WAS` (**not** ESPN `WSH`).
2. Team page `https://www.ourlads.com/nfldepthcharts/depthchart/{KEY}`.
   No index-id lookup (NFL keys are the path).
3. **Slate teams only.** Cache HTML under `nfl/data/ourlads/`. Sleep 1.5s
   between live requests.
4. Parse the **first** table (offense) Player 1–5 for LWR / RWR / SWR / QB /
   RB / TE-* (OL, FB, defense, ST dropped). LWR/RWR/SWR Player 1 are all WR
   rank 1 (starters at each alignment), not a snap share.
5. Strip OurLads draft / FA / transaction tails (`17/1`, `SF24`, `U/Sea`,
   `T/NYJ`, `CC/NE`, trailing `O`/`Q`). Flip `Last, First`. Title-case
   ALL-CAPS veterans (`MAHOMES, PATRICK` → `Patrick Mahomes`).
6. Write [`nfl/data/depth.csv`](../../data/depth.csv):
   `team,pos,rank,name,source_url,fetched_at` (FanDuel abbrev, display name).
7. Join to FanDuel `Nickname` + `Team` via `match_key` (Jr/Sr/II stripped).
   Misses go in `NAME_OVERRIDES` in `nfl/depth.py`.

`depth_rank` 1 is a **role prior**, not 100% snaps. Unlisted players stay
in the pool with a tiny prior. Rank 4+ uses the same unlisted prior as
today (`DEPTH_PRIOR` only defines 1–3).

Week-1 ILP still does **not** maximize FPPG:

```
objective = implied_team_total × depth_prior(rank) × position share
            × usage_factor (±20% Lineups target_share tilt; WR/TE)
            × prop_factor (±20% tilt when a volume line joins)
prior: rank1=1.00  rank2=0.40  rank3=0.15  unlisted=0.05
```

Usage tilt: [`targets.md`](targets.md). `--skip-targets` leaves the factor at 1.0.

## Commands

```
python3 -m nfl.depth --csv "nfl/data/<players-list>.csv"
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv"
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --skip-depth
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --depth-source=espn
```
