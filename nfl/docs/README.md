# NFL docs

Canonical FanDuel NFL classic rules: [`sites/fanduel-nfl.md`](sites/fanduel-nfl.md) (contest 133104). Code copy: `nfl/rules.py`. Lobby capture: [`sites/lobby-133104-rules-scoring.jpg`](sites/lobby-133104-rules-scoring.jpg).

Optimizer: `python3 -m nfl.optimize` (contest 133104). Slate coverage: `python3 -m nfl.status` / `--slate-status`. Do not import `ncaaf`. Do not copy `ncaaf/docs/`. “Let’s optimize” stays NCAAF.

Choke catalog: [`data/sources.md`](data/sources.md). Lineups WR/TE usage tilt: [`data/targets.md`](data/targets.md). Upload CSVs: `nfl/export/`. Hindsight LineStar perfect: [`../data/perfect/`](../data/perfect/) (week-1 12-gamer `133104.json`; not 133647).
