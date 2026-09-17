# Lineups.com NFL scrape authorization

Recorded 2026-09-17 in the DFS project chat.

## Grant

Randy (this repo’s owner) stated that his roommate gave permission to
scrape **Lineups.com** for **personal DFS** in this repository — the same
roommate context as the OurLads NFL grant in
[`ourlads-authorization.md`](ourlads-authorization.md).

**Grantees:** Randy and agents working in this repository (Cursor / Grok CLI).

**Scope:** Retrieve Lineups NFL **targets** and **snap counts** (RB / WR / TE
public SSR pages) for personal FanDuel classic research in this project.
Not a license to republish Lineups compilations, train models on the
corpus, or ship a competing usage-stats product.

## How we scrape (conditions we accept)

- Identified browser-like `User-Agent` (same header block as the existing
  targets scrape). No stealth, no proxy rotation, no Turnstile bypass.
- Public SSR HTML only (`<script type=application/json class=sc-sports-nfl-metrics>`).
  No login. The WR/TE **targets** ingest already used these pages before
  this grant was written down.
- Cache under `nfl/data/lineups-targets/` and `nfl/data/lineups-snaps/`
  (JSON preferred; Mac urllib may Cloudflare-block — fall back to the
  cached payload, same as targets).
- Output derived files we own (`nfl/data/targets.csv`, `nfl/data/snaps.csv`)
  for the optimizer to join — do not mirror the site.
- Do not scrape FanDuel.

If Randy later narrows this, that note supersedes this file.
