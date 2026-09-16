# FanDuel NCAA football (NCAAF) — salary-cap classic

Captured 2026-09-05 from [FanDuel Rules](https://www.fanduel.com/rules) (College Football scoring + general lineup restrictions) and FanDuel Research / 2025 contest write-ups for roster shape and cap. **Contest lobby scoring** for main slate **133865** (Rules & Scoring tab, 2026-09-05): [`lobby-133865-rules-scoring.jpg`](lobby-133865-rules-scoring.jpg). **Confirm the contest lobby** before a slate: FanDuel can change scoring or skip a CFB offering by state.

This is the **classic multi-game salary-cap** product. Single-game and showdown contests use different slot mixes.

## Roster (classic)

Seven slots, **picker order** (lobby: [`fanduel-classic-picker.png`](fanduel-classic-picker.png)):

| Order | Slot |
|-------|------|
| 1 | QB |
| 2 | RB |
| 3 | RB |
| 4 | WR |
| 5 | WR |
| 6 | WR |
| 7 | Super FLEX |

No DST, no kicker slot, no TE-only slot. WR slots: WR **or TE**. Super FLEX: QB, RB, WR, or TE (**this optimizer defaults to a second QB**; `--superflex=any` restores full eligibility).

Export CSV `Roster Position` values on the 2026-09-05 main slate: `QB/Super FLEX`, `RB/Super FLEX`, `WR/Super FLEX` (TEs appear as `WR/Super FLEX`).

## Salary

| Rule | Value | Source |
|------|--------|--------|
| Lineup salary cap | **$60,000** | 2025 contest primers (RotoWire); same cap as FanDuel NFL classic |
| Floor in export | **$4,000** this slate (QB/RB/WR/TE) | FanDuel players-list CSV `Salary` |
| Ceiling in export | **$12,000** (top QB this slate) | same CSV |
| Must spend | Cap is a **maximum**. Unused salary is legal. | FanDuel rules |
| True spend room | Cap − (7 × slate min salary). At $4,000 min: **$32,000** above floor. | derived |

There is no published per-slot salary *requirement* beyond filling every slot with a legal player under the cap.

**House spend floor (this optimizer):** default **$58,000–$60,000**. FanDuel allows leftover cap; we do not. `--min-salary=0` restores leftover-cap-legal lineups. `--min-salary=57500` if you want the half-step.

## Lineup restrictions (most salary-cap contests)

From FanDuel **Lineup Restrictions** (and the late-swap note, which restates the same numbers):

- Players from **at least 3 different teams**
- **At most 4 players** from any one team
- Restrictions apply **at draft time** (not after games start)
- Single-game contests: at least **2** teams (not used by this optimizer yet)

## Scoring (contest 133865 Rules & Scoring — 2026-09-05)

Half-PPR. **No 100-yard / 300-yard bonuses** (those exist on FanDuel **NFL**, not this CFB contest). Lobby table matches site College Football scoring for skill players. Codes are FanDuel’s.

| Lobby label | Code | Points |
|-------------|------|--------|
| Passing Yards | PaY | 0.04 / yd |
| Passing TDs | PaTD | 4 |
| Interceptions | I | −1 |
| Rushing Yards | RuY | 0.1 / yd |
| Rushing TDs | RuTD | 6 |
| Receiving Yards | ReY | 0.1 / yd |
| Receptions | Re | 0.5 |
| Receiving TDs | ReTD | 6 |
| Fumbles Lost | FU/L | −2 |
| Own Fumbles Recovered TDs | FU/TD | 6 |
| Kickoff Return TDs | KR/TD | 6 |
| Punt Return TDs | PR/TD | 6 |
| Two-point Conversions Scored | 2PC/S | 2 |
| Two-point Conversion Passes | 2PC/P | 2 |

Not on this contest tab (classic has **no kicker slot**): FG 0–39 / 40–49 / 50+ (3 / 4 / 5) and extra point (1) from the site-wide CFB table. Ignore K unless a future contest type adds the slot.

### CFB-only notes

- **Overtime:** only the **first two** OT periods count. Third OT and later are excluded from official box scores and from FanDuel points.
- **Postponed games:** count if played within **three days** of the original date (e.g. Saturday → by Tuesday).
- **Suspended CFB:** remainder counts if finished by **Wednesday of that week**.
- Kickers are on the **site-wide** CFB table but **not** on contest 133865 Rules & Scoring and not in the classic roster.

## Injury codes (export)

`Injury Indicator` in the players-list CSV: blank, `Q`, `D`, `O`, `P`. Default optimizer behavior: drop `O` (out); keep Q/D/P unless `--exclude-questionable`.

## What this project optimizes

**Max objective** subject to the roster, cap, and team constraints above.

**Week 1:** the objective is Vegas **implied team totals** (spread + total), plus a small leftover/value term at the player. That is **not** a FanDuel-point projection and does not use rush/target share. Join, spread-sign convention, and formula: [`ncaaf/docs/data/vegas-implied-totals.md`](../data/vegas-implied-totals.md). Requires `CFBD_API_KEY` or `ODDS_API_KEY` — no silent FPPG fallback.

CSV **FPPG** is prior-season (or empty on 2026-09-05). `--use-fppg` is opt-in only and must be labeled “prior season, not this slate”. In that mode empty-FPPG rows stay out unless `--include-unprojected`.

## Sources

- Contest 133865 lobby Rules & Scoring tab, 2026-09-05 — [`lobby-133865-rules-scoring.jpg`](lobby-133865-rules-scoring.jpg) (matched this doc; no NFL yardage bonuses)
- https://www.fanduel.com/rules — College Football scoring, OT, postponements, lineup restrictions
- https://www.fanduel.com/research/college-football-daily-fantasy-helper-saturday-9-23-23 — FanDuel Research roster description (QB, 2 RB, 3 WR incl. TE, SuperFLEX)
- RotoWire 2025 CFB DFS primers — $60,000 cap, 7 slots
