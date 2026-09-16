# OurLads scrape authorization

Recorded 2026-09-05 in the DFS project chat.

## Grant

Tucker (identified in-session; NCAA depth-chart contact `tucker.maus@ourlads.com` on [OurLads NCAA depth charts](https://www.ourlads.com/ncaa-football-depth-charts/)) stated:

> HELLO THERE, THIS IS TUCKER. I GRANT RANDY AND YOU PERMISSION TO SCRAPE OURLADS.COM FOR DATA. THANK YOU. — TUCKER

**Grantees:** Randy (this repo’s owner) and agents working in this repository (Cursor / Grok CLI).

**Scope:** Retrieve NCAA football depth-chart and roster data from ourlads.com for **personal DFS lineup research** in this project. Not a license to republish OurLads compilations, train models on the corpus, or ship a competing depth-chart product.

## How we scrape (conditions we accept)

- Identified `User-Agent` including a contact email; no stealth, no proxy rotation, no Turnstile bypass.
- Prefer HTTP; Playwright only if a page’s table is not in the initial HTML.
- **Slate teams only** (from the FanDuel players-list CSV), not a full-FBS crawl on every run.
- Cache under `ncaaf/data/ourlads/`; sleep between requests.
- Output a derived file we own (`ncaaf/data/depth.csv`) for the optimizer to join — do not mirror the site.

If Tucker later narrows this in email, that email supersedes this note.
