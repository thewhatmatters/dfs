# NFL

FanDuel NFL classic (contest **133104**). Canonical rules: [`docs/sites/fanduel-nfl.md`](docs/sites/fanduel-nfl.md). Code: [`rules.py`](rules.py).

Nine slots: QB, RB, RB, WR, WR, WR, TE, FLEX, DEF. Cap **$60,000**. House spend floor **$58,000**. House max **3** per team (FanDuel lobby 4; `--max-per-team=4` restores). House stacking: no opponent DST vs the lineup QB (`forbid_qb_opp_dst`) or a stud RB (`forbid_stud_rb_opp_dst`, salary ≥ `stud_rb_min_salary`); 2+ WR/TE from a team requires that team's QB (`require_qb_with_two_pass_catchers`, `--stack-qb=off` disables). ILP stack premium `STACK_COEF=0.12` on QB+WR/TE (printed Proj stays week1_score). No K.

Do not import `ncaaf`. CFB has no DST/K slots and no yardage bonuses.

**“Let’s optimize” stays NCAAF.** This adapter is `python3 -m nfl.optimize` / skill `optimize-nfl-classic` (“optimize NFL”). Do not import `ncaaf`.

`--bring-back=N` (default **0**) is a house stack construction flag, off unless passed. Mean cash and existing 150 ceiling uploads stay unchanged at N=0.

`--n-lineups>1` is **coverage**, not chalk lock-in. Default `--max-exposure=0.60` caps any one player (including DST) as a running count while the set is built; `--max-exposure=1` disables. `--min-unique` defaults to **3** and is stacked vs every locked 9 (a 7-man core cannot persist by swapping the same two cheap seats). `--diversity=coverage` (default when n>1): lineup #1 is the mean-optimal 9; later solves soft-penalize high-exposure players and unmatched-Lineups fillers (OurLads-only names with usage 1.0). `--diversity=chalk` is the old “keep maximizing mean” path. Salary, stack-qb, and house DST rules stay intact.

## Command

```bash
python3 -m nfl.optimize \
  --csv "nfl/data/FanDuel-NFL-2026 CDT-09 CDT-13 CDT-133104-players-list.csv" \
  --out results/nfl-lineup.json
```

JSON on stdout; picker (QB, RB, RB, WR, WR, WR, TE, FLEX, DEF) on stderr — last column is **Sources** (join tags). Slate coverage: `python3 -m nfl.status --csv …` or `--slate-status` (JSON `slate_status`) — **relevant-band** first (QB>$6500, WR>$5000, RB>$5000, TE>$4500, DEF>$3500), full pool on the `all pool` line. Defaults: gangstash lines, targets, snaps, and depth (`GANGSTASH_API_KEY`, or a same-query cache). Fallbacks: `--lines-source=oddsapi` (`ODDS_API_KEY`, hard `LINES_KEY` if that key is missing), `--targets-source=lineups`, `--snaps-source=lineups`, `--depth-source=ourlads` (or `espn`). A missing gangstash key with no cache stops lines and degrades targets, snaps, and depth; the stderr line names those flags. Player props use the same key (cache `nfl/data/gangstash-props/`). Do not `--refresh-props` unless the cache is empty. `offense_pct` is the 0–1 snap share.

Week-1 objective: implied team total × OurLads depth prior × position share × Lineups **usage tilt** (WR/TE `target_share`; RB 70% `snap_share` / 30% `target_share`, each vs a depth-conditional expected share, clamped ±20%). Volume player props (including 100/300 bonuses when the *line* is ≥ threshold) are a **±20% tilt** on that base — not a second currency. `--skip-targets` / `--skip-snaps` / `--skip-props` leave the matching factor at 1.0. DEF uses opponent implied total as PA plus a +3.0 sack/TO prior. `--sim` is a layered Monte Carlo (game total+spread, scripted pass/rush volume, joint target/rush shares). The ILP `mean` objective stays that week-1 point estimate unless `--projection-source sim` (each player's simulated mean; default `board` until layer 4 and a backtest). Docs: [`docs/data/sim.md`](docs/data/sim.md).

Legacy optional Lineups refresh (only with `--targets-source=lineups` / `--snaps-source=lineups`): `python3 -m nfl.targets --refresh` → `nfl/data/targets.csv`; `python3 -m nfl.snaps --refresh` → `nfl/data/snaps.csv`. Name-join gaps print on stderr and JSON `targets` / `snaps`. Docs: [`docs/data/targets.md`](docs/data/targets.md), [`docs/data/snaps.md`](docs/data/snaps.md).

Lineups grant: [`docs/data/lineups-authorization.md`](docs/data/lineups-authorization.md). Depth grant: [`docs/data/ourlads-authorization.md`](docs/data/ourlads-authorization.md).
