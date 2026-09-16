# Odds API player-prop overlay

When `ODDS_API_KEY` is set, the optimizer pulls NCAAF player props for **slate
games only** and converts lines to FanDuel points (contest 133865 scoring).

Volume lines (pass/rush/rec yards or receptions) are a **±20% tilt** on the
implied score (`clamp(prop_fd / base, 0.80, 1.20)`). They are not a second
currency. Everyone uses implied × role prior × **position share** × script.
`--skip-props` leaves the factor at 1.0.

Position share is a scoring-identity prior, not snap %: QB 0.50, RB 0.30,
WR 0.22, TE 0.12.

## Credits

`GET /events` (1) + `GET /events/{id}/odds` × slate games × 5 markets × 1
region. Fourteen games ≈ **70 credits**. Cached under
`ncaaf/data/odds-props/YYYY-MM-DD/`. `--refresh-props` refetches.

Markets: `player_pass_yds`, `player_pass_tds`, `player_rush_yds`,
`player_reception_yds`, `player_receptions`. FanDuel book preferred; else
median Over line.

Coverage is thin: Texas/Ohio State have QBs; many WRs/TEs will not.
Unjoined book names (`_join_prop` miss) and players with no market are
labeled on each picker `note` (`prop_status`: `props` | `no_market` |
`unmatched`) — do not treat a blank note as a failed API.

## Commands

```
python3 -m ncaaf.optimize --csv ncaaf/data/<export>.csv
python3 -m ncaaf.optimize --csv ncaaf/data/<export>.csv --skip-props
```
