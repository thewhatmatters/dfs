# optimize-nfl-classic — Why

Living record of what this skill is, the decisions behind it, and any
non-obvious constraints (spec A12).

Created: 2026-09-06  ·  Cloned from optimize-ncaaf-classic (no generate-skill ceremony)

## 1. Purpose

Solve the highest-projection legal FanDuel NFL classic lineup for a slate CSV.

## 2. Reusable patterns (link to spec A1..A15)

Follows `~/.cursor/skills/skill-architecture.md` A1–A15. Solver lives in the
**repo** (`nfl/`), not vendored inside the skill — skill scripts are a thin
CLI seam (A4 one-concern). Scoring tables are in `nfl/docs/sites/` (A1 branch:
only loaded when rules need a check). No stdin interview / question card —
NFL path is flags-only.

## 3. Decision log

- 2026-09-06: **v1 lock** — `nfl/` is a real FanDuel classic optimizer
  (`python3 -m nfl.optimize`), not a stub. Do not import `ncaaf`. Bare
  “Let’s optimize” stays NCAAF even though this skill exists. Triggers are
  explicit: “optimize NFL”, “Let’s optimize NFL”, `/optimize-nfl-classic`.
- 2026-09-06: **No interview / question card.** NCAAF TUI grill does not
  apply. Solve with `--sim` (mean default). GPP/ceiling → `--objective=ceiling`.
  `n-lineups` only if the user named a count. Cached props; no
  `--refresh-props` unless asked. Uploads: `nfl/export/` when n>1.
- 2026-09-06: **`--bring-back=N` default 0.** House term: opposing pass
  game vs our QB stack (WR/TE, optionally the other QB). Not kick-return,
  not opponent DST. Mean cash and existing 150 ceiling uploads stay
  unchanged unless Randy passes the flag.
- 2026-09-06: **Not this pass:** weather NWS, Sunday inactives, PFF,
  anytime-TD overlay, fading opposing RBs by default,
  changing mean/ceiling defaults.
- 2026-09-08: **Report = paste stderr box table**; Grok must not reflow.
- 2026-09-10: **Props are a ±20% tilt on implied, not an override.** Same
  lock as NCAAF 2026-09-09. Dual path (book FD pts vs implied×share) gave
  listed stars a second currency. One formula:
  `base × clamp(prop_fd/base, 0.80, 1.20)`. `--skip-props` = factor 1.0.
  NFL still has thicker books; that is more tilt signal, not a second
  objective. Bonuses stay in `prop_fd` when the *line* is ≥ 100/300.
- 2026-09-11: **House max 3/team** (FanDuel lobby 4). Randy's mean 9 was
  4 LAC (Herbert, Hampton, Ladd, QJ). `--max-per-team=4` restores lobby.
- 2026-09-11: **`--stack-qb` on.** 2+ WR/TE from a team without that team's
  QB is illegal. One WR without QB is fine. Two RBs (Gibbs + DET RB)
  without Goff is fine. Hampton (RB) does not trigger.
- 2026-09-11: **ILP stack premium** `STACK_COEF=0.12` × catcher.projection
  when QB + teammate WR/TE are both selected (product binaries). Printed
  Proj stays week1_score. Not a game copula; do not fake correlated p90
  to clear cash 150.
- 2026-09-16: **NFL depth default is OurLads** (`nfl/ourlads.py` →
  `nfl/data/depth.csv`). Randy: roommate created OurLads; NFL scrape is
  authorized the same way as NCAA. Grant:
  `nfl/docs/data/ourlads-authorization.md`. ESPN `--depth-source=espn` is
  optional (often 403). Do not change the Tucker NCAA grant file.
- 2026-09-14: **`--sim` is a structural game draw** (not independent per
  player). One Vegas total+spread world per game (`TOTAL_SIGMA_FRAC=0.12`,
  `SPREAD_SIGMA=10`). Skill pts = team_pts × depth × share × prop_factor;
  yardage bonuses on yards scaled by team_pts/implied; DST PA = opponent
  points in that world. Lineup Fl/Cl = joint 9 p10/p90. ILP ceiling stays
  p90. Not SaberSim / not a PBP copula. Do not fake cash-150.

## 4. Known limitations / environment caveats

- `--sim` is a game draw (teammates share the world), not SaberSim / not a
  PBP copula. Do not fake cash-150.
- Greedy may fail min-teams, cap, or bring-back; ILP is the real path.
- Classic roster has one QB slot — opposing QB as a bring-back is legal in
  the helper but cannot fill a second QB.

## 5. Audit rubric coverage

See `skill-architecture.md` §B; this skill targets every PASS that applies.

## 6. Notes

Hard dep: repo `nfl` package (same git tree). Soft: `pulp` (gated, greedy degrade).
Vegas path: `ODDS_API_KEY` (hard gate `LINES_KEY`). Choke ids: `nfl/docs/data/sources.md`.
