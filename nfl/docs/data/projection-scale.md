# Why the board sums to ~70

Observed on the Sep 27 FanDuel main slate: the board-optimal lineup projects **71.6** (Mahomes 16.8, Kelce 4.8). Realistic cash optima land around **120–130**. GPP winners land around **150–180**. Nothing in this page changes `POS_FD_SHARE`, the ±20% prop cap, or the ILP. It records the diagnosis and the fix to apply after a backtest.

## What the board actually scores

`week1_score` is not a FanDuel box score. It is

```
implied team total × depth prior × position share × usage tilt × prop tilt
```

Position shares in `nfl/projections.py`: QB 0.50, RB 0.28, WR 0.18, TE 0.12. Usage and the prop tilt are each clamped to ±20%. DEF is the opponent's points-allowed bucket plus a flat +3.

A starter on a 24-point team, depth 1, no usage tilt, no prop:

| Pos | Board | Rough FanDuel median from the same team volume |
|-----|-------|------------------------------------------------|
| QB | 12.0 | ~17–19 (about 250 pass yards, 1.6 pass TD, a few rush yards) |
| RB | 6.7 | ~12–16 once rush yards, a TD, and half-PPR are counted |
| WR | 4.3 | ~8–12 for a WR1 target share |
| TE | 2.9 | ~6–9 |

Mahomes at 16.8 is the **+20% cap** on a ~14 base (`14 × 1.20 = 16.8`), not his passing prop. A 20-point prop cannot lift him further. Kelce at 4.8 is a TE share of a high implied total, still a fraction of a real tight-end box score.

Nine of those shares, taken from the best teams, plus a DST around 5–8, land near **55–75**. That is the 71.6 lineup. The shares are doing what they were written to do.

## Why FanDuel totals are higher

FanDuel points are not a partition of real team points. Yards, half-PPR, and 4/6-point TDs stack. One 24-point offense can produce ~60–80 FD points across QB + RB + receivers. A lineup only keeps the best few of those, from several games, so a median-optimal nine sits near 120. GPP winners are the right tail of that same scoring (bonuses, multi-TD games), not a different formula.

The lobby rates themselves match `nfl/rules.py` and `skill_fd_points`: 0.04 per pass yard, 0.1 per rush/rec yard, 0.5 per reception, pass TD 4, rush/rec TD 6, INT −1, fumble lost −2, two-point conversions 2, +3 at 300 pass / 100 rush / 100 rec, and the DEF PA buckets (21–27 is 0). The board never calls that function.

## What the sim does

The opportunity path scores the rates above from sampled opportunities. Yardage bonuses are +3 only when that game's sampled yards clear the line (`sigma = max(12, 0.22 × mean)`), so the upper tail gets the bonus and the mean does not carry `3 × P(clear)` on every draw. Players with no target history, and the default ILP (`--projection-source board`), stay on `week1_score`.

Still not drawn: fumbles lost, two-point conversions, return TDs. DEF events other than the PA bucket are the flat +3 prior, not sacks and takeaways one by one. Those gaps are small next to the share-vs-box-score gap.

## Backtest status

`python3 -m nfl.backtest --csv … --season 2026 --week 2` reads gangstash `player_stats_weekly` `fd_points` and prints mean error and MAE by position for the board and the sim, for the full pool and for starters (QB/RB/TE depth 1, WR depth 1–3, after an O/D/IR/NA handoff). It joins depth, the FanDuel injury column, prior-week targets and snaps, and week-scoped lines and props the same way the optimizer does. A missing optional source is named and skipped. No lines stops the run unless `--allow-missing-lines`. Shares and the ±20% prop cap are still unchanged. The sim, not the board, anchors starter passing yards and TDs to the implied total (or a passing prop) on the opportunity path as well as the no-history path, and the starter QB rush count is his own history or a yard floor.

## Proposed fix (not applied)

1. When a player has volume props, use `PlayerProp.fd_points()` as the level. Drop the ±20% cap that pins Mahomes at 16.8.
2. Otherwise build the projection from team pass/rush volume × role share × `skill_fd_points`, with the bonus as `P(yards ≥ line) × 3` on the mean and as a per-game +3 in the sim.
3. Keep printing today's `week1_score` beside it until a weeks-1–2 backtest shows the new mean error by position, and the optimal lineup's actual `fd_points` sum versus the projected sum.
4. Only then consider making `--projection-source sim` the default.

The default ILP mean is now `--projection-source sim` (10000 draws, `--sim-efficiency data`). `--projection-source board` opts out. Missing sim inputs fall back to the board and to placeholder efficiency. Steps 1–3 above are still not applied.
