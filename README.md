# DFS

Daily fantasy lineup research and optimization. **NCAA football on FanDuel classic is first.** NFL FanDuel classic is a second adapter (`nfl/`). Do not import `ncaaf` from `nfl`. Bare “Let’s optimize” stays NCAAF.

## What you get

- Exact (PuLP/CBC) salary-cap lineup for a FanDuel NCAAF or NFL players-list CSV
- Contest rules in `ncaaf/docs/sites/fanduel-ncaaf.md` and `nfl/docs/sites/fanduel-nfl.md`
- Agent skills `optimize-ncaaf-classic` (bare “Let’s optimize”) and `optimize-nfl-classic` (“optimize NFL”)

## How to run

```bash
python3 -m pip install -r requirements.txt
export CFBD_API_KEY='…'   # https://collegefootballdata.com/key
python3 -m ncaaf.optimize --csv "ncaaf/data/<FanDuel-export>.csv"
python3 -m nfl.optimize --csv "nfl/data/<players-list>.csv"
```

JSON on stdout; human lineup + per-game spread/total/implied totals on stderr. `--out PATH` writes the JSON. `--agent` skips prompts. NCAAF `--use-fppg` is prior-season FPPG, not this slate. NFL uploads: `nfl/export/` when `--n-lineups>1`.

Implementation work is meant to run on **Grok CLI**. From Cursor, the in-session model should dispatch via `bash scripts/dispatch-grok.sh results/grok-prompt.md` rather than editing the tree itself. Or just `grok` in this directory.

## Layout

| Path | Role |
|------|------|
| `ncaaf/docs/sites/fanduel-ncaaf.md` | Roster, cap, scoring, team limits — confirm vs lobby |
| `ncaaf/docs/data/vegas-implied-totals.md` | Team join, spread sign, implied-total formula |
| `ncaaf/docs/data/ourlads-depth.md` | Authorized OurLads depth ingest + name join |
| `ncaaf/docs/data/player-props.md` | Odds API player-prop overlay |
| `ncaaf/docs/data/sources.md` | Scrape/API choke ids (`choke LINES_KEY:`) |
| `ncaaf/docs/data/game-script.md` | Weekly grind vs blowout sit knobs |
| `ncaaf/rules.py` | Same numbers as code |
| `ncaaf/optimize.py` | CLI |
| `ncaaf/data/` | FanDuel "Download Players List" CSVs |
| `.cursor/skills/optimize-ncaaf-classic/` | Agent skill (NCAAF classic; bare “Let’s optimize”) |
| `nfl/` | FanDuel NFL classic adapter (`python3 -m nfl.optimize`) |
| `nfl/export/` | FanDuel upload CSVs when `--n-lineups>1` |
| `.cursor/skills/optimize-nfl-classic/` | Agent skill (NFL; “optimize NFL”, `/optimize-nfl-classic`) |

Default objective is **Vegas implied team totals** (spread + total). CSV `FPPG` is last season / empty — not the week-1 score. See [`ncaaf/docs/data/vegas-implied-totals.md`](ncaaf/docs/data/vegas-implied-totals.md). Missing `CFBD_API_KEY` (or `ODDS_API_KEY`) stops the run.
