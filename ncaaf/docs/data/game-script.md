# Game script (weekly playbook)

Closing-spread strategy for FanDuel NCAAF classic. **Use it, then tweak the
knobs after a finished slate** — not off a live leaderboard, not off one
box (n=1). Solver: `ncaaf/script.py`. Formula detail also in
[`play-distribution.md`](play-distribution.md). Skill:
`optimize-ncaaf-classic`.

This is **not** live score and not “this staff is run-heavy.” After week 1,
CFBD 2026 `pass_rate` / usage join via `ncaaf/mix.py` (spread-only if empty).
Never 2025. Player props **tilt** implied ±20%; they do not skip `script_mult`.
`--sim` applies `script_mult` to the **realized** margin in each game world.

## Two run regimes

Competitive or modest-favorite games often stay on the ground (clock,
both sides). Huge leads still run, but **starters come off** and **d2/d3
RBs** get the clock/garbage. Slate 133865: Sheppard in 17–3 (grind RB1);
Carter d2 44.6 vs Covey d1 10.3 (committee / lead); Scott/Mosley WR1s dead
in 48–10 and 59–7.

| Band | Favorite spread | What we assume | ILP |
|------|-----------------|----------------|-----|
| Grind / clock | about **−3 to −14** | RB1 still the back; pass catchers not sat | spread 2−tilt only (RB up a little, WR down a little) |
| Mention | **−14 to −21** | notes only | same tilt; no sit |
| Blowout sit | **≤ −21** (full at **−40**) | WR/TE/QB1 recede; **RB1 recedes** (no clock-chew tilt); **RB2/RB3 up** | sit/boost table below |
| Dog | **+14 or more** | throw to keep up | pass positions up; never sit |

LSU −9.75 finishing 51–10 is the reminder: **closing spread ≠ final margin**.
We still use the close. Do not pretend we saw the 41-point win beforehand.

## Current knobs (`ncaaf/script.py`)

Edit the constants, run tests, append a week line under **Log**. Do not
invent a second copy of the numbers here — if this table and the file
disagree, **the file wins**; fix this table in the same change.

| Knob | Now | Role |
|------|-----|------|
| `SPREAD_COEF` | 0.012 | 1 + coef × spread on pass positions |
| `BLOWOUT` | 14 | notes mention favorite/dog |
| `SIT_RUN` | 21 | sit/boost starts (favorite) |
| `SIT_FULL` | 40 | sit/boost reaches far end |
| `SIT_WR1` | 0.85 → 0.62 | WR/TE starter haircut |
| `SIT_WR` | 0.92 → 0.78 | other WR/TE |
| `SIT_QB` | 0.94 → 0.85 | less throwing / rest |
| `SIT_RB1` | 0.92 → 0.75 | featured back sits some |
| `BOOST_RB23` | 1.12 → 1.28 | d2/d3 clock and garbage |
| `DEF_PPA_COEF` | 0.40 | 1 + coef × (pass PPA − rush PPA) on pass positions |
| `DEF_PPA_GAP_CAP` | 0.35 | n=1 cap on that gap |

Unlisted RBs on a blowout favorite stay 1.0 (no fake committee). Dogs never
sit. Props tilt implied ±20% after this mix. Opponent D PPA is the rush/pass
**split**; overall D stays in the closing implied total.

## How to tweak each week

1. After **0 quarters left**, compare our 7 to what cashed / to a perfect
   card if you have one.
2. Ask: grind vs blowout, RB1 vs RB2, WR1 sit. Change **one band** of knobs
   unless two findings agree.
3. `python3 -m unittest ncaaf.test_script ncaaf.test_explain -q`
4. Log a dated line below. `/refine-skill optimize-ncaaf-classic` only if
   the *procedure* changed (new band, new stop), not for a 0.02 coef nudge.

## Log

- 2026-09-06: **Backtest card** `ncaaf/data/perfect/133865.json`. Overlap vs
  entered lock is the refinement metric (Smith only at lock). Do not fit ILP
  to the perfect 7. Memphis d2 Carter was −10.75 — blowout RB2 boost (≤ −21)
  would not have selected him.
- 2026-09-07: **133970** ND −20.75 dual RB + WR1 fade in hindsight; sit band
  unchanged; LOU@MISS both QBs; WASH chalk unused on perfect; floor $100
  above this perfect ($57,900). Do not fit names. Do not move `SIT_RUN`
  to catch −20.75 off this one card.
- 2026-09-09: opponent D **pass PPA − rush PPA** split (coef 0.40, gap cap
  0.35). CFBD cache, not ESPN. Overall D remains in implied total.
- 2026-09-13: **134050** LineStar FD perfect `ncaaf/data/perfect/134050.json`
  ($58,600 / 257.88). Overlap 2/7 (Sheppard, Dixon) = entered lock; not
  improved. Five misses were 30–40 on proj 3–11 (Mestemaker / Ijeboi /
  Harris / Legree / Lopez). p90 cannot see ~8% dog-upset worlds; ceiling
  is now sim **p99**. Mean card gets GPP seats (`ncaaf/leverage.py`): ≥1
  +14 WR1/TE1 and ≥1 +21 dog QB when the pool has them (Coleman / Harris
  / Mestemaker). d2 darts (Legree) still not forced. `--leverage=off`
  restores unconstrained mean. Do not fit names. Sit knobs unchanged.
