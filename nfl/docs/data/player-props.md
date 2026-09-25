# Gangstash NFL player props

NFL player props come from Randy’s gangstash HTTP API. The board is
BettingPros consensus via Data Aggregator (one line per player and prop).
Game lines default to gangstash (`--lines-source=gangstash`).
`--lines-source=oddsapi` is the Odds API fallback
([`gangstash.md`](gangstash.md)).

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
| `pass_yds` | **Pass YDs** (live), Passing Yards, Pass Yards, Pass Yds, `player_pass_yds` |
| `pass_tds` | **Pass TDs** (live), Passing Touchdowns, Passing TDs, Pass TD, `player_pass_tds` |
| `rush_yds` | **Rush YDs** (live), Rushing Yards, Rush Yards, Rush Yds, `player_rush_yds` |
| `rec_yds` | **Rec YDs** (live), Receiving Yards, Rec Yards, Rec Yds, Reception Yards, `player_reception_yds` |
| `receptions` | **Recs** (live, confirmed), Receptions, Rec, `player_receptions` |

Live BettingPros `prop` counter, 2026-09-24 gangstash smoke (671 rows):

| prop | rows | scored |
|------|------|--------|
| Rec YDs | 188 | `rec_yds` |
| Recs | 180 | `receptions` |
| Rush YDs | 87 | `rush_yds` |
| Rush ATTs | 67 | unmapped |
| Pass TDs | 29 | `pass_tds` |
| Pass CMPs | 28 | unmapped |
| Pass YDs | 28 | `pass_yds` |
| INTs | 27 | unmapped |
| Pass ATTs | 23 | unmapped |
| Rsh + Rec | 14 | unmapped |

Recs is confirmed as receptions. INTs, Pass ATTs, Pass CMPs, Rush ATTs, and
Rsh + Rec stay unmapped on purpose. Anything else still prints
`props unmapped (not scored): <exact string> (n)` and is counted in
`unmapped_props`. Same-timestamp duplicate lines keep the first value and
record `conflicts`.
