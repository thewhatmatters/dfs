# NFL layered Monte Carlo

`--sim` draws FanDuel points for one slate. Each draw is one world. Games are independent. Players in the same game share that world, so lineup floor and ceiling are the joint 9 (the sum inside a world), not the sum of player percentiles.

This is not a play-by-play model and not a sportsbook scrape. The sim never calls the network. Weekly inputs are a local JSON file or in-memory rows. Gangstash `/data` readers are a separate client; this page is only the shape the sim consumes.

## What the ILP optimizes

`--projection-source` chooses the `mean` score. Default is `board`.

| Objective | `board` (default) | `sim` |
|-----------|-------------------|--------|
| `mean` | `week1_score`: implied total × depth prior × position share × usage, ±20% prop tilt | that player's simulated mean FD points |
| `floor` | that player's sim p10 | that player's sim p10 |
| `ceiling` | that player's sim p90 | that player's sim p90 |

`--projection-source sim` runs the Monte Carlo even when `--sim` is omitted (same as `floor` / `ceiling`). `--sim 0` does not turn that off. A player with no sim row keeps `week1_score`.

The board point estimate stays `week1_score`. `--board` still prints it in the proj column (built before the objective swap). With `--projection-source board`, `lineup_proj` is the sum of `week1_score`. With `--projection-source sim`, the lineup total row sums the same sim means the Proj column shows, and `lineup_board` keeps the week-1 sum. The diagnostic lists the largest `|sim mean − board|` gaps and says which source the ILP mean is using.

Leave the default at `board` until layer 4 (prop-calibrated efficiency and TDs) and a backtest against gangstash `player_stats_weekly` `fd_points` show the sim is at least as accurate as the board. Why a board-optimal lineup sums near 70 while cash scores 120–130: [`projection-scale.md`](projection-scale.md). Shares and the ±20% prop cap are unchanged.

JSON `flags.projection_source` is `board` or `sim`. The top-level `projection_source` string is still the Vegas / week-1 description, not this flag.

## Layers

### 1. Game — total and spread

One Vegas total and one home spread per game. Team points:

```
home = (total − home_spread) / 2
away = (total + home_spread) / 2
```

Dispersion:

```
scale_team = clamp(sqrt(epa_var) / 1.15, 0.60, 1.80)
scale_game = sqrt((scale_home² + scale_away²) / 2)
total_sigma = 0.12 × |total| × scale_game
spread_sigma = 10 × scale_game
```

`1.15` is the per-play EPA standard deviation that leaves the sigmas at the old constants (`0.12` and `10`). Missing variance uses scale `1`.

**Scoring variance for a team** is the mean of:

- that team's **offense** EPA variance
- the opponent's **defense** EPA variance

when that row exists. `epa_variance` is, in order:

1. Pooled per-play variance from the overall sums: `(epa_sq_sum − epa_sum² / n) / (n − 1)`, `n ≥ 2`. This wins over a precomputed `epa_var`.
2. Else the `epa_var` field.
3. Else a play-weighted blend of the pass and rush pools (`pass_n` / `rush_n` with their epa sums, or `pass_epa_var` / `rush_epa_var`).

**Inputs:** `team_stats` rows: `team_fd`, `side`, `n`, `epa_sum`, `epa_sq_sum`, `epa_var`, `pass_n`, `rush_n`, `pass_epa_sum`, `pass_epa_sq_sum`, `rush_epa_sum`, `rush_epa_sq_sum`, `pass_epa_var`, `rush_epa_var`. `epa_per_play` is stored and not used in the draw.

### 2. Volume and script

Used only for teams that have at least one WR/TE/RB with weekly target history. Other teams do not spend RNG here.

```
plays ~ Normal(plays_mu, 4), floored at 40
pass_rate = clamp(neutral + 0.012 × (−margin), 0.38, 0.78)
rush_rate = team_stats rush share, shifted by that same script, else 1 − pass_rate
pass_attempts = plays × pass_rate
rush_attempts = min(plays × rush_rate, implied × rush_rate × 9.5 / 4.4)
pass_yards = prop_pass_yds, or implied × pass_rate × 17.5
pass_tds = prop_pass_tds, or (implied / 7) × clamp(pass_rate × 1.08, 0.40, 0.82)
```

`plays_mu` is `pass_n + rush_n` when that sum is one game (40–95). Otherwise it is 63. `margin` is this team's drawn points minus the opponent's. A trailing team passes more. A leading team runs more.

Passing yards and passing TDs are **not** target volume times a league rate. The level is the Vegas implied total times the scripted pass rate. `prop_pass_yds` replaces the yard anchor. `prop_pass_tds` replaces the TD anchor. A 22-point team at a 0.57 pass rate is about 220 passing yards. The receiving lines are still one shared draw; they are rescaled so they sum to a sample around that anchor. A high implied total raises the starter QB even when the script trims his pass rate.

**Neutral pass rate** (offense row only):

| Fields present | Neutral rate |
|----------------|--------------|
| `neutral_pass_rate` | that value (PROE is not added again) |
| `pass_rate` and `proe` | `pass_rate − proe` |
| `pass_rate` only | `pass_rate` |
| `proe` only | `0.57 + proe` |
| nothing | `0.57` |

Rates are fractions (`0.58`). Values above `1.5` are treated as percents.

**Inputs:** offense `pass_rate`, `neutral_pass_rate`, `proe`. The margin comes from layer 1, not from a stat row.

### 3. Opportunity

Catchers with weekly target history (WR/TE/RB, joined by `player_id` / `gsis_id` when that id equals the FanDuel id, otherwise `team_fd` + normalized name) draw a **Dirichlet** target share. An "other" bucket keeps the shares from summing past the historical total, so unrostered teammates still own the rest of the targets.

```
team_targets = pass_attempts × (mean team_targets / team_pass_attempts)
player_targets = team_targets × drawn_share
```

The default targets-per-attempt is `0.90` when no week has both `team_targets` and `team_pass_attempts`.

Concentration `κ` comes from each player's weekly shares: `Var = μ(1−μ)/(κ+1)`. The team uses the median `κ`, clamped to `[2, 80]`. Fewer than two weeks uses `κ = 10`.

Weekly shares are the mean over **games played**. A week with `targets > 0` or `target_share > 0` counts. A zero-target week is dropped, so a missed week does not dilute the share. A snap row with `offense_pct > 0` keeps a zero-target week (he played and was not targeted). `offense_pct == 0` drops that zero-target week. No played weeks → share 0, and the player stays on the depth role share. κ uses the same played weeks.

Same-team pass catchers therefore compete (negative share correlation).

**One QB gets the passing volume.** That is the depth-1 QB. If several players are depth 1, the one with `prop_pass_yds` or `prop_pass_tds` wins, then higher salary, then pid. If nobody is depth 1, the QB with a passing prop wins, else the lowest depth rank. Every other QB on that team is scored at 0 on this path (they are not given the role-share team-points formula).

The starter's passing yards and passing TDs are the **sum of the receiving lines** realized for his catchers and for the other bucket in that same world, after those lines are scaled to the pass anchor above. A catcher's points use his own scaled line. Rush yards and interceptions stay on the QB's own attempt and rush counts. A leading script still pushes rush points up and pass points down, but the yard level moves with the implied total, so a higher team total does not produce a lower QB.

**RB rush share**

Team rush attempts are the scripted count above (team_stats rush rate plus the margin, capped by the implied-total yard budget). Every roster RB splits that pool, not only the backs who have target weeks.

- Snap share and carry share are each normalized, then combined with a geometric mean when a back has both. Carry counts come from `carries` / `rushing_attempts` on a target row or from `player_stats_weekly`.
- A back with only snaps (or only carries) keeps that one signal.
- If neither is present, rush shares are fixed depth weights: `depth_prior × expected snap share`, normalized across the RBs. No extra random draw.
- `prop_rush_yds` sets that back's rush attempts to `prop / 4.4` yards per carry. Props that sum past the team pool are scaled down. The other backs split what remains.

The starter QB keeps 8% of team rushes. Snaps and carries are optional; an empty list is valid.

Players with no target weeks stay on the deterministic role share, including an RB who only has snaps. Snaps change rush mix only for RBs who already have target weeks.

The passing QB on that path does not. Role share was team points × 0.50, so an implied 17.5 starter landed near 8.8 with a p90 near 11.4. That QB is now the pass-yard and pass-TD anchors (neutral pass rate, not the drawn margin) scaled by drawn team points / implied, plus a rush floor of max(12 yards, 0.8 × implied) or the rush-yard prop. Other QBs on the team score 0. The scale keeps the QB linear in team points, so a teammate on the role share stays highly correlated with him.

**Inputs:** `targets` rows: `season`, `week`, `position`, `player_name`, `team_fd`, `targets`, `target_share`, `team_targets`, `team_pass_attempts`, `gsis_id` (optional `player_id`). `snaps` rows: the same identity fields plus `offense_pct` (fraction, or a percent above `1.5`).

### Efficiency (placeholder, layer 4 later)

`PlaceholderEfficiency` in `nfl/sim_efficiency.py` turns opportunities into FanDuel points: yards per target / rush and TD rates, plus the 100- and 300-yard bonuses when that game's sampled yards cross the line. Passing props are applied as the team anchor before this class scores the line. A rush-yard prop is that RB's rush attempts (`prop / 4.4`).

`receiving_line(rng, position, targets)` realizes one allocation and samples receiving yards around that conditional mean (`sigma = max(12, 0.22 × mean)`). Catcher points use that sampled line. QB passing yards and TDs sum the same lines, including the other bucket. A 300-yard passing bonus or a 100-yard rush/rec bonus is +3 only in games whose sampled yards clear the line. It is not `3 × P(clear)` added to every draw, and it is not a cliff on the mean yards. A standalone `points` call with no `team_receiving` samples passing yards around `7.1` yards per attempt.

Fumbles lost, two-point conversions, and return TDs are in `skill_fd_points` (the lobby table). This placeholder does not draw them. DEF in the sim is still the points-allowed bucket plus the +3 sack/turnover prior, not a sack-by-sack draw.

Layer 4 replaces this class. The calls are:

```python
receiving_line(rng, position, targets) -> ReceivingLine
points(rng, player, OpportunityCount(...)) -> float
```

`rng` and the player's `prop_*` fields are there for that replacement (residual yards, lumpy TD counts, prop medians). Layer 4 must draw inside `receiving_line` and leave `points` reading the attached line, or the QB will not match his catchers.

Until then, a catcher's points are linear in his targets, which is why the share draw shows up cleanly in the correlations.

## Gangstash feed

When `--sim` runs and `--sim-inputs` is omitted, `nfl/sim_feed.py` builds `SimInputs` from the readers in `nfl/gangstash_data.py` (same-day cache, then live, then a stale cache). `week1_score` does not read these rows.

| Sim use | Dataset | What is kept |
|---------|---------|----------------|
| EPA variance | `team_stats`, season, offense and defense | raw `n`, `epa_sum`, `epa_sq_sum` (pass/rush sums only if the overall sums are missing) |
| Neutral pass rate and PROE | same season rows | `neutral_pass_rate`, `proe`, `pass_rate` |
| Recent script | `team_stats_weekly` for `--targets-weeks`, or `--targets-week` | averages those three rates onto the matching team/side. EPA sums stay seasonal. Skipped when no week is set (the weekly query requires `week`) |
| Target shares | `targets`, that same week list, or every week the season query returns | one player-week each, not the single-share usage aggregate |
| RB rush shares | `snaps`, same window, `position=RB` | `offense_pct` |
| RB carry shares | `player_stats_weekly`, same window | `carries` or `rushing_attempts` (skipped when the dataset is missing) |

No key and no cache for every dataset prints one line and keeps role shares:

```
sim inputs: gangstash unavailable — role shares deterministic
```

A partial board is used. `--sim-inputs PATH` does not call gangstash.

## Fallback

No `--sim-inputs`, or inputs that do not match anyone's name:

- total sigma `0.12 × |total|`, spread sigma `10`
- no plays draw, no Dirichlet
- skill points = team points × depth prior × position share × usage
- a volume prop still multiplies by the ±20% factor, and yardage bonuses scale with `team_pts / implied`
- DST = PA bucket of opponent points in that world + the sack/TO prior

`simulate_player` (single-player tests) stays on that role-share path. The slate path is `simulate_games`.

## Diagnostic

`format_sim_diagnostic` prints each player's p10 / p50 / p90, a same-team correlation block, and the largest board-vs-sim gaps (`score_player` vs sim mean).

Two summaries:

| Line | Pairs |
|------|--------|
| `QB–WR mean r` / `WR–WR mean r` | every QB with every WR, and every WR pair, including backups and bench players with no target history |
| `QB–WR starters mean r` / `WR–WR starters mean r` / `QB–TE starters mean r` | depth-1 QB vs WR depth 1–3, those WRs with each other, and depth-1 QB vs TE depth 1 |

Starters with target history should show QB–WR positive and WR–WR negative. The all-pairs means can flip on a full roster: bench WRs with no target weeks stay on team points, and team points move against pass rate (a trailing script passes more while the team is behind). Those bench pairs outnumber the starters. Read the starter lines for the sign check.

The optimizer writes the full text to JSON `sim_diagnostic` and prints the mean lines (all pairs and starters) on stderr. `--sim-inputs PATH` loads the JSON. A bad file is `choke SIM_INPUTS`.

```bash
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv" --sim \
  --sim-inputs nfl/testdata/sim_layers.json
```

Fixture: `nfl/testdata/sim_layers.json` (DET offense/defense EPA sums, four weeks of swapping WR target shares, Gibbs target shares, no snaps).

## Seeds

One `random.Random(seed)` for the slate. Games run in game-id order, players in pid order. Inside an opportunity team the order is: plays gaussian, target-share gammas (pid order, then the other bucket), rush-share gammas when snaps or carries exist, then one yards gaussian per receiving line (catchers, then the other bucket), then one team pass-yard gaussian around the anchor (skipped when the anchor is 0). Scoring then draws rush yards when that player has rushes. Share draws stay on the gamma sequence. Empty inputs add no draws beyond the total and the spread.

## Backtest

`python3 -m nfl.backtest` joins a FanDuel players list to gangstash `player_stats_weekly` `fd_points` for one season and week. It prints mean error (projection minus actual) and MAE by position for the board (`week1_score`) and for the sim mean.

```bash
python3 -m nfl.backtest --csv "nfl/data/<players-list>.csv" --season 2026 --week 2
```

The reader is `fetch_player_stats_weekly` (`dataset=player_stats_weekly&season=&week=`, header `x-api-key`, same-day cache as the other `/data` datasets). The board and the sim are built with the same joins as the optimizer for that week: `closing_lines` (then `game_lines`), week injuries, depth, prior-week targets and snaps, and props. An empty source is named on `missing:` and the week still scores. Targets and snaps are weeks `1..W-1` only. Props keep the latest `scraped_at` strictly before kickoff for that season and week; a row with no week, or a later week's board, is dropped. `--lines-file` (CSV or JSON) supplies historical lines and wins over the network. `--starters-only` prints depth-1 players who played, plus DEF; the default prints that slice and the full pool. The optimizer default stays `--projection-source board`.
