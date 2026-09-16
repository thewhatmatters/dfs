# Vegas implied team totals (week-1 stand-in)

Default optimizer objective for FanDuel NCAAF classic. CSV `FPPG` is last
season (or empty) and is **not** a projection for this slate. Use `--use-fppg`
only as an explicit opt-in labeled “prior season, not this slate”.

OurLads `depth_rank` is a **role prior** (rank 1 = starter at that
alignment), not snap %. See [`ourlads-depth.md`](ourlads-depth.md).
Player props (when `ODDS_API_KEY` is set): [`player-props.md`](player-props.md).
Choke ids (which scrape/API died): [`sources.md`](sources.md).
Ownership, weather, and rush/target share stay `null`.

## Sources

Preferred: CollegeFootballData `GET /lines` with `CFBD_API_KEY`
(`Authorization: Bearer …`). [Free key](https://collegefootballdata.com/key).

Fallback: [The Odds API](https://the-odds-api.com/) `americanfootball_ncaaf`
odds (`spreads`, `totals`, optional `h2h`) with `ODDS_API_KEY`.

If neither key is set, the optimizer **stops**. It does not fall back to FPPG.

Replay: `--lines-json PATH` (CFBD array, Odds API array, or simple games).

## Team join

FanDuel players-list CSV:

| Column | Role |
|--------|------|
| `Team` | FanDuel abbrev (`TEX`, `TXST`, …) |
| `Opponent` | FanDuel abbrev |
| `Game` | `AWAY@HOME` (e.g. `TXST@TEX` → Texas State at Texas) |

Map: [`ncaaf/teams.py`](../../teams.py) FanDuel abbrev → CFBD **school**
name (`Texas`, `Texas State`, `Texas A&M`) and Odds API **full** name
(`Texas Longhorns`, …). Match is exact and case-insensitive. Unmapped
abbrevs fail loud — do not guess among Texas / Texas State / Texas A&M.

Add a row to `TEAMS` when a new FanDuel abbrev appears.

## Spread-sign convention

CFBD `spread` (and this repo’s `home_spread` / simple-JSON `spread`) is the
**home team’s** spread.

- Negative ⇒ home favorite (home lays points).
- Positive ⇒ home underdog (home gets points).
- `formattedSpread` is like `"Texas -29.5"`. If it names the away team as
  favorite, the stored `spread` must still be the home number (opposite sign).
  A mismatch of more than half a point is a hard error.

The Odds API gives each team its own `point` on the spreads market (negative
= favorite). We store the home team’s point as `home_spread`.

## Implied totals

```
implied_home = (total - home_spread) / 2
implied_away = (total + home_spread) / 2
```

For any team: `implied = (total - team_spread) / 2` where `team_spread` is
negative if that team is favored.

Example: total `60.5`, home Texas `-29.5` → Texas `45.0`, Texas State `15.5`.

Provider: CFBD `consensus` if it has both spread and total; otherwise the
median of providers that do. Odds API prefers the FanDuel book, else median
of US books.

## Player score (ILP objective)

One currency for the field:

```
base      = implied × role_prior × POS_FD_SHARE[pos] × script_mult
objective = base × clamp(prop_fd / base, 0.80, 1.20)   # if a volume line
          = base                                         # otherwise
          + 0.04 * (4000 / salary)
prior: usage / typical starter, else rank1=1.00  rank2=0.40  rank3=0.15  unlisted=0.05
share: QB 0.50  RB 0.30  WR 0.22  TE 0.12
```

A book line is a ±20% vote on `base`, not a replacement. `--skip-props`
keeps the factor at 1.0. `script_mult` still hits prop players (it is in
`base`). Formula: [play-distribution.md](play-distribution.md). Rank 1 is a
**role prior**, not snap %. `0.04` is smaller than a half-point implied-total
gap among equal ranks.

JSON exposes `spread`, `total`, `implied_total`, `implied_opp`,
`depth_rank`, `prop_*`, `prop_status` (`props` | `no_market` | `unmatched`),
`script_mult` (applied to the implied base).

## Per-player notes

Every picker row has `note` (copied onto `slots[*]`). Stderr prints it under
the slot; starters (`depth_rank == 1`) get ` (*)` after the name.

- **Props:** book + the lines; they tilt implied ±20%, they do not replace it.
- **No props:** say “no player props” and the implied × depth prior ×
  `POS_FD_SHARE` fallback (join-miss vs no market when we can tell). Do not
  imply hidden usage.
- **Depth:** starter / d2 / d3 / “OurLads unlisted — tiny prior”.
- **Script:** only from this team’s spread, implied vs opp, attached props
  on the pool, and roster construction. Do not invent “run-heavy” from
  school reputation. WR/TE on favorites ≤ −21 say second-half sit / reduced
  starter usage (and “score haircut” when `script_mult` actually moved the
  fallback). Dogs say throw-to-keep-up; they are not sat.

Solver-level `Lineup.notes` stays greedy diagnostics plus a few lineup
script lines (e.g. `OSU -50.25: huge implied, books price rush on Jackson`).

Salary: FanDuel cap **$60,000**. This optimizer also requires **$58,000** spent (`--min-salary`, `0` disables) so cheap rank-1s on blowouts cannot leave thousands unused.

Construction: SuperFLEX is a **second QB** (`--superflex=qb`, default). `--superflex=any` allows RB/WR/TE in that slot.

## Commands

```
export CFBD_API_KEY='…'   # or ODDS_API_KEY
python3 -m ncaaf.optimize --csv ncaaf/data/<export>.csv
python3 -m ncaaf.optimize --csv ncaaf/data/<export>.csv --board      # pool proj (d1–d2 + props)
python3 -m ncaaf.optimize --csv ncaaf/data/<export>.csv --use-fppg   # prior season
```
