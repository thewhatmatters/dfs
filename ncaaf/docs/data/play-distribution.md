# Run / pass distribution (after week 1) and closing-script (week 1)

Team implied totals say **how many points**. Depth says **who starts**. Props
say **this player’s line** when a book posts one. What’s still missing is
**how the offense is allocated** — 70% pass vs a run-heavy staff — and that
the **opponent + game script** can shove that mix.

Do **not** invent week-1 usage from last season. 2026 CFBD advanced stats
are empty until boxes exist. Last season’s pass rate is a labeled prior only
if we opt in later; it is not the default.

**Closing-script is not that.** The closing spread is already on the player.
`script_mult` applies a bounded spread tilt (and a second-half sit on huge
favorites) **before** 2026 rates exist. That is the closing number, not a
live score, not a 2025 pass rate, and not a live remainder / Q3 re-optimize.

## What we store (team-level, not snap %)

| Field | Meaning |
|-------|---------|
| `pass_rate` | Offense pass-play share (CFBD `offense.passingPlays.rate`) |
| `opp_pass_rate` | Pass-play share **faced** by this opponent’s defense |
| `opp_pass_ppa` / `opp_rush_ppa` | Opponent D PPA allowed (pass − rush gap splits the tilt) |
| `spread` | Already on the player (this team’s spread; negative = favorite) |

Player `rush_share` (RB, CFBD `usage.rush`) and `target_share` (WR/TE,
`usage.pass`) replace the OurLads depth prior on the implied path when
present. Typical starter refs: RB 0.40, WR/TE 0.22. QB stays depth (team
`pass_rate` still tilts script). Props tilt that base ±20%; they do not skip usage.

## Source

CollegeFootballData (key we already have), **after week 1 boxes**:

- `GET /stats/season/advanced?year=2026` — season-to-date mix
- `GET /stats/game/advanced?year=2026&week=N` — last week, if we want
  opponent-adjusted one-game samples

Join via existing FanDuel abbrev → CFBD school map (`ncaaf/teams.py`).

Spread itself comes from the lines ingest (`ncaaf/docs/data/vegas-implied-totals.md`),
not from CFBD advanced.

## How it hits the lineup

Everyone:

```
base      = implied_total × role_prior × position_share × script_mult
objective = base × clamp(prop_fd / base, 0.80, 1.20)   # volume line
          = base                                         # else
```

`script_mult` (`ncaaf/script.py`) — two paths, then sit:

**Spread-only** (`pass_rate` is None — week 1):

```
tilt = 1 + 0.012 × team_spread          # missing spread → 1.0
m    = tilt if QB/WR/TE else 2 − tilt
m    = clamp(m, 0.55, 1.45)
m   *= sit(pos, spread, depth_rank)     # 1.0 unless favorite ≤ −21
m    = clamp(m, 0.35, 1.65)
```

**Mix × spread** (`pass_rate` set):

```
tilt = (1 + 0.7 × (pass_rate − 0.55))
     × (1 + 0.35 × (opp_pass_rate − 0.55))   # if opp rate
     × (1 + 0.012 × team_spread)             # if spread
     × (1 + 0.40 × clamp(pass_ppa − rush_ppa, ±0.35))  # opp D split
then same invert / clamp / sit as above
```

- Pass-heavy staff (`pass_rate` 0.70 vs FBS ~0.55) → QB/WR/TE up, RB down
- Run-heavy → opposite
- Opponent that **faces** a lot of pass nudges pass
- Opponent D leakier vs pass than run (pass PPA − rush PPA > 0) → WR/QB up, RB down. Overall D quality stays in implied total; this is the split only. Gap capped at ±0.35 (n=1).
- Spread: underdog (positive team spread) throws more; favorite runs more

**Second-half sit** (favorites only, `team_spread ≤ −21`; `−14` is notes-only):

```
t = min(1, (−spread − 21) / (40 − 21))     # 0 at −21, 1 at −40
WR/TE starter:  0.85 → 0.62
WR/TE other:    0.92 → 0.78
QB:             0.94 → 0.85                 # modest; less throwing / rest
RB d1:          0.92 → 0.75                 # featured back recedes
RB d2/d3:       1.12 → 1.28                 # clock / garbage
RB unlisted:    1.0
```

Dogs (`team_spread` large positive) keep “throw to keep up” on QB/WR; they
are never sat. Grind favorites (−3 to −14) only get the spread 2−tilt.
Weekly playbook and knob log: [`game-script.md`](game-script.md).

Outer clamp is 0.35–1.65 so a −40 favorite still moves a WR starter below the
mix floor (0.55). Example: same implied total, Bama −27.75 WR1 ≈ 0.51× vs a
WR1 on a 3-point favorite ≈ 0.96×.

Player props **tilt** the implied base ±20%; they do not skip `script_mult`.
JSON `script_mult` is the value on that base.

## Game Monte Carlo (`--sim`)

Not independent player noise. Each draw: game total ~ Normal(Vegas total,
0.15×total) and home spread ~ Normal(close, 16). Home/away points split from
that pair. `script_mult` uses the **realized** margin (sit/boost in that
world). Every player in the game shares it. Mean ILP stays the close
point estimate — E[QB+WR] = E[QB]+E[WR]. Lineup Fl/Cl are the joint 7.
Not a PBP copula.

## When to turn mix ingest on

After week 1 boxes: `ncaaf/mix.py` pulls `/stats/season/advanced` +
`/player/usage` for the CSV year (≥2026). Cache `ncaaf/data/cfbd-mix/`.
Empty 2026 → no-op mix (spread-only + depth). Never silently use 2025.
`--skip-mix` / `--refresh-mix`.
