# OurLads NFL scrape authorization

Recorded 2026-09-16 in the DFS project chat.

## Grant

Randy (this repo’s owner) stated that his roommate created OurLads and
explicitly said it is OK for this project to scrape **OurLads NFL depth
charts** the same way as NCAA.

**Grantees:** Randy and agents working in this repository (Cursor / Grok CLI).

**Scope:** Retrieve NFL depth-chart data from
[ourlads.com/nfldepthcharts](https://www.ourlads.com/nfldepthcharts/) for
**personal DFS lineup research** in this project. Not a license to republish
OurLads compilations, train models on the corpus, or ship a competing
depth-chart product.

The Tucker NCAA grant in
[`ncaaf/docs/data/ourlads-authorization.md`](../../../ncaaf/docs/data/ourlads-authorization.md)
is **NCAA-only** and stays unchanged.

## How we scrape (conditions we accept)

- Identified `User-Agent` including a contact email (`OURLADS_CONTACT_EMAIL`,
  else the existing OurLads NCAA contact already used in vendor forms). No
  stealth, no proxy rotation, no Turnstile bypass.
- HTTP only: Scrapling **Fetcher**. No StealthyFetcher / DynamicFetcher.
- **Slate teams only** (from the FanDuel players-list CSV), not a 32-team
  crawl on every run.
- Cache under `nfl/data/ourlads/`; sleep ~1.5s between live requests.
- Output a derived file we own (`nfl/data/depth.csv`) for the optimizer to
  join — do not mirror the site.
- Do not scrape FanDuel.

If Randy later narrows this, that note supersedes this file.
