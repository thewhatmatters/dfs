# Sources / choke ids (pointer)

Canonical catalog: `nfl/docs/data/sources.md` (from this skill:
`../../../../nfl/docs/data/sources.md`). Load that file when preflight is
not `ready` or optimize prints `choke <ID>:`.

Do not duplicate grant text or HTTP recipes here. Ingest stays in
`nfl/lines.py`, `nfl/injuries.py`, `nfl/ourlads.py`, `nfl/depth.py`,
`nfl/targets.py`, `nfl/snaps.py`, `nfl/props.py`, `nfl/gangstash.py`.
Default Lineups grant: `nfl/docs/data/lineups-authorization.md`.

On failure, report the **id** (e.g. `LINES_KEY`, `INJ_ESPN`, `DEPTH_OURLADS`,
`DEPTH_ESPN`, `PROPS_GANGSTASH_KEY`) and the stop-vs-degrade row. Green runs do not
dump the catalog. Do not scrape FanDuel. Default depth is OurLads
(`nfl/docs/data/ourlads-authorization.md`).
