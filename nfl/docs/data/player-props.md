# Gangstash NFL player props

NFL player props come from Randy’s gangstash HTTP API. The board is
BettingPros consensus via Data Aggregator (one line per player and prop).
Game lines (spreads, totals, moneylines) still use `ODDS_API_KEY` in
`nfl/lines.py`.

## Endpoint

```
GET https://vmzgpslqoeuqmdchdekm.supabase.co/functions/v1/props
x-api-key: $GANGSTASH_API_KEY
```

Optional query params: `player_name`, `prop` (exact match). The optimizer
calls the unfiltered board.

Response: `{ "data": [ { "id", "player_name", "prop", "line", "scraped_at" } ], "truncated": bool }`.

`truncated=true` with no filters is a hard stop (`PROPS_GANGSTASH`). The
partial body is not cached.

## Cache and refresh

Cache: `nfl/data/gangstash-props/YYYY-MM-DD/props.json` (gitignored).

```
export GANGSTASH_API_KEY='…'   # .env is fine; never commit the key
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --refresh-props
```

Same-day cache is used with no network and no key. If the API is unreachable
(or the key is unset) and an older cache file exists, that file is used and
stderr says the cache is stale. No key and no cache degrades
(`PROPS_GANGSTASH_KEY`): the overlay is skipped and implied×depth stays.
`--skip-props` skips the overlay on purpose.

Do not `--refresh-props` unless you mean to refetch.

## What gets scored

Rows collapse to one player (Jr./Sr. stripped). A volume line is a ±20% tilt
on the implied score, including 100/300 yardage bonuses when the line itself
clears the threshold. Join is exact `match_key` against the FanDuel pool.
Two slate players with the same key are left unmatched. Names that are only
on the board are not a join failure.

Mapped `prop` strings (case, punctuation, and a trailing Over/Under ignored):

| field | accepted forms |
|-------|----------------|
| `pass_yds` | Passing Yards, Pass Yards, Pass Yds, `player_pass_yds` |
| `pass_tds` | Passing Touchdowns, Passing TDs, Pass TDs, Pass TD, `player_pass_tds` |
| `rush_yds` | Rushing Yards, Rush Yards, Rush Yds, `player_rush_yds` |
| `rec_yds` | Receiving Yards, Rec Yards, Rec Yds, Reception Yards, `player_reception_yds` |
| `receptions` | Receptions, `player_receptions` |

TODO: confirm the live BettingPros `prop` strings from a `--refresh-props`
dump. The table is the conservative set. Anything else is **not dropped
quietly** — stderr prints `props unmapped (not scored): <exact string> (n)`
and JSON stats include `unmapped_props`. Do not map anytime TD, interceptions,
completions, or attempts onto the five fields until those exact strings are
seen and a scoring rule exists. Same-timestamp duplicate lines keep the first
value and record `conflicts`.
