# NFL

FanDuel NFL classic (contest **133104**). Canonical rules: [`docs/sites/fanduel-nfl.md`](docs/sites/fanduel-nfl.md). Code: [`rules.py`](rules.py).

Nine slots: QB, RB, RB, WR, WR, WR, TE, FLEX, DEF. Cap **$60,000**. House spend floor **$58,000**. House max **3** per team (FanDuel lobby 4; `--max-per-team=4` restores). House stacking: no opponent DST vs the lineup QB (`forbid_qb_opp_dst`) or a stud RB (`forbid_stud_rb_opp_dst`, salary ≥ `stud_rb_min_salary`); 2+ WR/TE from a team requires that team's QB (`require_qb_with_two_pass_catchers`, `--stack-qb=off` disables). ILP stack premium `STACK_COEF=0.12` on QB+WR/TE (printed Proj stays week1_score). No K.

Do not import `ncaaf`. CFB has no DST/K slots and no yardage bonuses.

**“Let’s optimize” stays NCAAF.** This adapter is `python3 -m nfl.optimize` / skill `optimize-nfl-classic` (“optimize NFL”). Do not import `ncaaf`.

`--bring-back=N` (default **0**) is a house stack construction flag, off unless passed. Mean cash and existing 150 ceiling uploads stay unchanged at N=0.

## Command

```bash
python3 -m nfl.optimize \
  --csv "nfl/data/FanDuel-NFL-2026 CDT-09 CDT-13 CDT-133104-players-list.csv" \
  --out results/nfl-lineup.json
```

JSON on stdout; picker (QB, RB, RB, WR, WR, WR, TE, FLEX, DEF) on stderr. Needs `ODDS_API_KEY` (hard `LINES_KEY` if missing). Cached OurLads depth (`python3 -m nfl.depth --csv …`), ESPN injuries, and Odds props; do not `--refresh-props` unless the cache is empty. `--depth-source=espn` is an optional fallback (often 403).

Week-1 objective: implied team total × OurLads depth prior × position share. Volume player props (including 100/300 bonuses when the *line* is ≥ threshold) are a **±20% tilt** on that base — not a second currency. `--skip-props` leaves the factor at 1.0. DEF uses opponent implied total as PA plus a +3.0 sack/TO prior. `--sim` is a structural game draw (Vegas total+spread; teammates share the world), not independent per-player noise.

Depth grant: [`docs/data/ourlads-authorization.md`](docs/data/ourlads-authorization.md).
