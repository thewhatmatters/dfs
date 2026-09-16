# OurLads NCAA depth charts

Authorized ingest. Grant and scrape conditions:
[`ourlads-authorization.md`](ourlads-authorization.md).

HTTP only: Scrapling **Fetcher** (`curl_cffi`). Identified `User-Agent` +
`From` email. `stealthy_headers=False`, `impersonate=None`. No
DynamicFetcher / StealthyFetcher, no proxy rotation. If the depth table is
missing from the raw HTML, fail loud — do not auto-switch to a browser.

## Join

1. Unique `Team` / `Opponent` on the FanDuel CSV → OurLads slug
   (`ncaaf/ourlads.py` `OURLADS_SLUG`). Unmapped abbrevs fail loud.
2. Index `https://www.ourlads.com/ncaa-football-depth-charts/` →
   `depth-chart.aspx?s={slug}&id={id}` (pretty URL
   `/depth-chart/{slug}/{id}/` after redirect).
3. **Slate teams only.** Cache HTML under `ncaaf/data/ourlads/`. Sleep 1.5s
   between live requests.
4. Parse offense table Player 1–3 for QB / RB / WR-* / TE-* (OL/defense
   dropped). WR-X / WR-Z / WR-H Player 1 are all WR rank 1 (starters at
   each alignment), not a snap share.
5. Write [`ncaaf/data/depth.csv`](../../data/depth.csv):
   `team,pos,rank,name,source_url,fetched_at` (FanDuel abbrev, display name).
6. Join to FanDuel `Nickname` + `Team` via `norm_name` (Last, First + class
   tokens → `first last`, punctuation stripped). Misses go in
   `NAME_OVERRIDES` in `ncaaf/depth.py`.

`depth_rank` 1 is a **role prior**, not 100% snaps. Unlisted players stay
in the pool with a tiny prior. Rush/target share stay `null`.

Week-1 ILP still does **not** maximize FPPG:

```
objective = implied_team_total × depth_prior(rank) + 0.04 × (4000 / salary)
prior: rank1=1.00  rank2=0.40  rank3=0.15  unlisted=0.05
```

## Commands

```
python3 -m ncaaf.depth --csv ncaaf/data/<export>.csv
python3 -m ncaaf.optimize --csv ncaaf/data/<export>.csv
python3 -m ncaaf.optimize --csv ncaaf/data/<export>.csv --skip-depth
```
