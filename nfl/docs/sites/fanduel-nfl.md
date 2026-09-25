# FanDuel NFL — salary-cap classic

Captured 2026-09-06 from contest **133104** lobby **Rules & Scoring** tab: [`lobby-133104-rules-scoring.jpg`](lobby-133104-rules-scoring.jpg). Roster and CSV fields are from the players-list export and the lineup-upload template in `nfl/data/` (slate date 2026-09-13). **Confirm the contest lobby** before a slate: FanDuel can change scoring or slot mix.

This is the **classic multi-game salary-cap** product. Single-game / showdown contests use a different slot mix. Optimizer: `python3 -m nfl.optimize`. This file + `nfl/rules.py` are the lock.

## Roster (classic, contest 133104)

Nine slots, **picker / upload order** (template header `QB,RB,RB,WR,WR,WR,TE,FLEX,DEF`):

| Order | Slot | Eligible CSV `Position` |
|-------|------|-------------------------|
| 1 | QB | QB |
| 2 | RB | RB |
| 3 | RB | RB |
| 4 | WR | WR |
| 5 | WR | WR |
| 6 | WR | WR |
| 7 | TE | TE |
| 8 | FLEX | RB, WR, or TE |
| 9 | DEF | D |

- CSV `Position`: `QB` / `RB` / `WR` / `TE` / **`D`** (defense). **No K** on this contest.
- CSV `Roster Position`: `QB`, `RB/FLEX`, `WR/FLEX`, `TE/FLEX`, `DEF`.
- FLEX = RB or WR or TE. **Not** QB, **not** DEF.
- TE has a dedicated slot. WR slots are WR-only (TE is not WR-eligible).

## Salary

| Rule | Value | Source |
|------|--------|--------|
| Lineup salary cap | **$60,000** | contest 133104 (FanDuel NFL classic) |
| Floor in export | **$3,000** this slate (DST) | players-list CSV `Salary` |
| Skill floor in export | QB **$6,000**; RB/WR/TE **$4,000** | same CSV |
| Ceiling in export | **$9,100** (top RB this slate) | same CSV |
| Must spend | Cap is a **maximum**. Unused salary is legal. | FanDuel |
| House spend floor | **$58,000–$60,000** | this project, not FanDuel. `--min-salary=0` later to restore leftover-cap-legal lineups |

There is no published per-slot salary *requirement* beyond filling every slot with a legal player under the cap.

## Lineup restrictions (FanDuel salary-cap)

- Players from **at least 3 different teams**
- **At most 4 players** from any one team (FanDuel lobby)
- Restrictions apply **at draft time** (not after games start)

### House max per team (not FanDuel)

FanDuel allows 4. **House default is 3** (`FanDuelNflClassic.max_per_team = 3`). Randy does not want 4 from one team (e.g. Herbert + Hampton + Ladd + QJ). `--max-per-team=4` restores the lobby. CLI validates **1..4**. JSON payload `max_per_team`. CBC, greedy, and post-solve all enforce. Picker notes when a team hits the bound: `house 3/team bound on LAC`.

## Scoring (contest 133104 Rules & Scoring — 2026-09-06)

Half-PPR. **Yardage bonuses apply** (unlike FanDuel CFB). Codes are FanDuel’s. Numbers copied from the lobby table.

### Skill (QB, RB, WR, TE, FLEX)

| Lobby label | Code | Points |
|-------------|------|--------|
| 300+ Passing Yards | 300+ PaY Gm | **+3** |
| 100+ Rushing Yards | 100+ RuY Gm | **+3** |
| 100+ Receiving Yards | 100+ ReY Gm | **+3** |
| Passing TDs | PaTD | 4 |
| Rushing TDs | RuTD | 6 |
| Receiving TDs | ReTD | 6 |
| Kickoff Return TDs | KR/TD | 6 |
| Punt Return TDs | PR/TD | 6 |
| Own Fumbles Recovered TDs | FU/TD | 6 |
| Two-point Conversion Passes | 2PC/P | 2 |
| Two-point Conversions Scored | 2PC/S | 2 |
| Passing Yards | PaY | 0.04 / yd |
| Rushing Yards | RuY | 0.1 / yd |
| Receiving Yards | ReY | 0.1 / yd |
| Receptions | Re | 0.5 |
| Interceptions | I | −1 |
| Fumbles Lost | FU/L | −2 |

### DEF

| Lobby label | Code | Points |
|-------------|------|--------|
| 0 Points Allowed | DE/PA0 | 10 |
| 1–6 Points Allowed | DE/PA1-6 | 7 |
| 7–13 Points Allowed | DE/PA7-13 | 4 |
| 14–20 Points Allowed | DE/PA14-20 | 1 |
| **21–27 Points Allowed** | **DE/PA21-27** | **0** |
| 28–34 Points Allowed | DE/PA28-34 | −1 |
| 35+ Points Allowed | DE/PA35+ | −4 |
| Sacks | S | 1 |
| Interceptions | I | 2 |
| Fumbles Recovered | FR | 2 |
| Safeties | DE/SF | 2 |
| Blocked Punts/Kicks | DE/B | 2 |
| Extra Point Return | DE/XPR | 2 |
| Return TDs | DE/RTD | 6 |
| Blocked Kick Return TDs | DE/BRTD | 6 |
| Fumble Return TDs | DE/FRTD | 6 |

**PA 21–27 is omitted** on the lobby tab (the table jumps from DE/PA14-20 to DE/PA28-34). Lock **0** — that band scores nothing, not a missing rule.

No kicker scoring (no K slot on this contest). Ignore FG / XP tables from other FanDuel products.

## House construction: no opponent DST vs our explosion (give-and-take)

**Not FanDuel scoring.** Defaults in `nfl/rules.py`: `forbid_qb_opp_dst=True`, `forbid_stud_rb_opp_dst=True`, `stud_rb_min_salary=7000`.

Offense vs opponent DST is **zero-sum** on that game: the stack/stud is betting points scored; that DST is betting they don’t. Paying both is fighting yourself. Bring-back *skill* from the other side can still be the “give and take”; **DST vs our own explosion is not**.

### QB (any salary)

Stacking means QB + pass-catchers on the same team (e.g. Joe Burrow + Ja'Marr Chase + Tee Higgins, CIN) in a high-total game. The **opposing DST eats the points that stack is built on** (sacks, INTs, points-allowed). Randy’s note: he will **never** take that DST.

- If the lineup QB’s `Opponent` equals the DEF’s `Team`, the lineup is **illegal**.
- Trigger is **the QB**, not a 2-WR minimum. Solo Burrow still cannot take the opponent DST.
- Contest 133104 game is **TB@CIN**. Opponent DST for a CIN QB is **TB**. **BAL** was the illustration of the same rule (CIN QB vs BAL DST), not this slate’s opponent.

### Stud RB (salary ≥ $7,000)

Same logic for a high-impact RB: if he’s on Bijan, he will **never** take that opponent DST. Salary is the tell (Bijan commands a huge chunk of the $60k). Cheap RB2 / handcuff does **not** get this ban — that’s the balance.

- If any RB in the lineup has salary ≥ `stud_rb_min_salary` and `Opponent` equals the DEF’s `Team`, the lineup is **illegal**.
- FLEX counts: check CSV `Position` **RB** + salary, ignore slot name.
- Bijan Robinson is **$8,800** on contest 133104 (`ATL@PIT`). **NO** DST was the illustration (Bijan vs Saints), same as BAL for Burrow — not this slate’s opponent (PIT).

### Still legal

- Same-team DST (CIN QB + CIN DST; ATL stud RB + ATL DST). Rare and usually wrong for other reasons.
- DST from some other game.
- Bring-back WRs / TE from the opponent (skill give-and-take). Optional ILP require: `--bring-back=N` (default **0**).
- Cheap RB vs that DST ($4,000–$5,000; under the $7,000 house cut).

### Bring-back (optional ILP require)

**Not FanDuel scoring.** House term: **bring-back** = opposing pass game vs our QB stack (WR/TE, optionally the other QB). Not kick-return. Not opponent DST (already illegal). `--bring-back=N` on `nfl.optimize` (int, default **0**). Mean cash and existing 150 ceiling uploads stay unchanged unless the flag is passed.

When `N >= 1`:

- A **pass stack** exists if the lineup QB’s team has ≥1 WR or TE in the 9 (FLEX WR/TE counts; FLEX RB does not).
- If there is a pass stack, require ≥ N players whose **team == QB.opponent** and position in `{WR, TE, QB}`. RB and DST do not count. FLEX WR/TE on the opponent counts.
- If there is **no** pass stack (solo QB, no teammate WR/TE), do **not** require a bring-back.
- Do **not** auto-fade the opponent RB.
- CBC + greedy both enforce. Post-solve check like `opp_dst_illegal`.
- Picker notes: `bring-back TB WR/TE (Egbuka)` or `bring-back off` when N=0.

Helpers: `pass_stack_players`, `bring_back_players`, `bring_back_illegal(slots, n)` in `nfl/rules.py`.

## House construction: 2+ WR/TE ⇒ that team's QB

**Not FanDuel scoring.** Default `require_qb_with_two_pass_catchers=True`. `--stack-qb=off` disables.

Two or more WR/TE from the same team (FLEX WR/TE counts) without that team's QB in the 9 is **illegal**. Correlation, not a second salary cap: Ladd + QJ going off should raise Herbert, not sit as four independent numbers.

- One WR without that team's QB is **legal**.
- Two RBs without that team's QB is **legal** (Gibbs + another DET RB). Hampton (RB) does **not** trigger this.
- FLEX WR/TE counts; FLEX RB does not.
- CBC + greedy both enforce. Post-solve check like `opp_dst_illegal`.
- Picker notes when it fires: `stack LAC QB+WR (McConkey, Johnston)`.

Helpers: `pass_catchers_by_team`, `stack_qb_illegal(slots, enabled)` in `nfl/rules.py`.

## House ILP stack premium (not FanDuel)

Independent `week1_score` stays the **printed Proj**. The ILP objective adds a house premium when the lineup QB and a teammate WR/TE are both selected: for each such catcher, `STACK_COEF * catcher.projection` (`STACK_COEF = 0.12` in `nfl/rules.py`).

Product binaries: `bonus ≤ x_qb`, `bonus ≤ x_wr`. RB/DST do not get the premium (Hampton rushing is not Herbert pass volume). Displayed picker Proj remains `week1_score` — do not lie on the board. Notes: `stack premium on`.

This is **not** a full game copula / PBP copula / SaberSim. Do not fake a cash-150 ceiling. `--sim` is a layered game draw (Vegas total+spread, scripted volume, joint opportunity shares). Layers and inputs: [`../data/sim.md`](../data/sim.md). The ILP `mean` objective stays `week1_score` unless `--projection-source sim` (simulated mean; board kept for comparison). Default is `board`. A board-optimal sum near 70 is the share model, not a missing bonus; see [`../data/projection-scale.md`](../data/projection-scale.md). Shares are unchanged.

Helpers:

- `qb_opp_dst_illegal(qb_team, qb_opp, def_team)` — CIN + TB DST illegal; CIN + CIN DST legal; CIN + DET DST legal. QB trigger unchanged (any salary).
- `opp_dst_illegal(def_team=…, qb_opp=…, rbs=[(opp, salary), …], skill=[SkillSide(position, opp, salary), …])` — QB rule plus stud RBs. FLEX Bijan counts; a $5,000 ATL RB vs NO DST does not.

## Injury codes (export)

`Injury Indicator` on the 133104 players-list CSV: blank, **`Q`**, **`IR`**, **`NA`**. This slate has no `O` / `D` / `P`.

| Code | Meaning on this export | Default |
|------|------------------------|----------------------------------|
| (blank) | healthy / none | keep |
| `Q` | questionable | keep unless `--exclude-questionable` |
| `IR` | injured reserve | drop (out-equivalent) |
| `NA` | not available | drop (out-equivalent) |

## House cash line (not FanDuel)

Randy’s 2025-ish FanDuel NFL classic **cash ~150** FD points. `--cash-line=150` is a house reporting line, **not** a FanDuel contest rule and **not** an ILP constraint. Do not fake cash-150.

`--sim` is a layered **game draw**: one world per game. Layer 1 draws the Vegas total and home spread (team EPA variance widens or tightens that draw when `--sim-inputs` has team stats). Layer 2 draws team plays and a pass/rush split that shifts with the margin. Layer 3 draws joint target shares, and RB rush shares when snap history is present. With no weekly target history, skill players keep the deterministic role share and yardage bonuses still fire on yards scaled by `team_pts / implied`. DST PA is opponent points in that world. Not SaberSim and not a PBP copula. Full layer notes: [`../data/sim.md`](../data/sim.md). When `--sim` ran, JSON `sim_diagnostic` plus stderr report percentiles and same-team correlations, and the lineup fields are:

| Field | Meaning |
|-------|---------|
| `lineup_proj` | sum of **week1_score** (mean path), not the ILP objective if floor/ceiling |
| `lineup_floor` | joint-9 sim p10 (same worlds) |
| `lineup_ceiling` | joint-9 sim p90 (same worlds; ILP ceiling is still per-player p90) |
| `cash_line` | house 150 (override with `--cash-line`) |
| `ceiling_minus_cash` | can be negative |

Do not invent a 150 ceiling.

## What this project optimizes

**Max objective** (week1_score + house stack premium) subject to the roster, cap, team limits (house max **3**/team; FanDuel 4), house spend floor, `forbid_qb_opp_dst`, `forbid_stud_rb_opp_dst` (`stud_rb_min_salary`), `require_qb_with_two_pass_catchers` (default on), and `--bring-back` (default 0). `--n-lineups=1..150` unique 9s (default 1). When n>1: `--max-exposure=0.60` (any one player; `--max-exposure=1` disables), `--min-unique=3` vs **every** locked 9 (not only the previous), `--diversity=coverage` (lineup #1 is mean-optimal; later 9s soft-penalize high-exposure / unmatched-Lineups fillers). `--diversity=chalk` keeps maximizing mean every 9. Upload CSV: picker order, `Id:Nickname` cells. Entries-upload templates keep `entry_id`/`contest_id`/`contest_name`/`entry_fee` and write the 9s under `QB`…`DEF` only — `--upload` needs that contest file on disk (`nfl/data/FanDuel-NFL-*-entries-upload-template.csv`, or a legacy QB-first lineup-upload template). `--n-lineups>1` always writes `nfl/export/nfl-{contest}-{objective}-{YYYYMMDD}-{HHMMSS}.csv` (local time); `--upload PATH` writes that path as well.

## Sources

- Contest **133104** lobby Rules & Scoring tab, 2026-09-06 — [`lobby-133104-rules-scoring.jpg`](lobby-133104-rules-scoring.jpg) (matched this doc; PA 21–27 omitted on the tab, locked at 0)
- `nfl/data/FanDuel-NFL-2026 CDT-09 CDT-13 CDT-133104-players-list.csv` — `Position`, `Roster Position`, `Salary`, `Injury Indicator`, `Game` / `Team` / `Opponent`
- `nfl/data/FanDuel-NFL-2026-09-13-133104-lineup-upload-template.csv` — legacy picker header QB, RB, RB, WR, WR, WR, TE, FLEX, DEF
- `nfl/data/FanDuel-NFL-*-entries-upload-template.csv` — FanDuel multi-entry file (`entry_id`…`entry_fee` then the same 9 slots). Default when present (newest filename).
