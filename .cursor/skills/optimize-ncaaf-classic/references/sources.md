# Sources / choke ids (pointer)

Canonical catalog: `ncaaf/docs/data/sources.md` (from this skill:
`../../../../ncaaf/docs/data/sources.md`). Load that file when preflight is
not `ready` or optimize prints `choke <ID>:`.

Do not duplicate grant text or HTTP recipes here. Scrapers stay in
`ncaaf/ourlads.py`, `ncaaf/lines.py`, `ncaaf/props.py`.

On failure, report the **id** (e.g. `DEPTH_OURLADS`, `LINES_KEY`, `PROPS_ODDS_KEY`)
and the stop-vs-degrade row. Green runs do not dump the catalog.
