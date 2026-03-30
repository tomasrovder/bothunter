# BotHunter — Feature Plan

## Context

BotHunter is an AI bot crawl analysis tool that generates reports on bot traffic patterns and blocking rates using Cloudflare's GraphQL Analytics API. It supports two report commands and queries across multiple Cloudflare zones (domains).

---

## Architecture

### Commands

| Command | Description |
|---------|-------------|
| `coverage-report` | GPTBot sitemap coverage report — checks which indexable URLs were hit by GPTBot, per zone using each zone's own sitemap |
| `blocked-report` | Per-bot blocking report — shows total vs blocked (403) traffic per bot per URL |

Both commands query all configured zones and produce a single CSV with a `zone` column.

### Configuration

| File | Purpose |
|------|---------|
| `.env` | Cloudflare credentials, zone IDs, and per-zone sitemap URLs |
| `bots.json` | Bot names and user agent patterns for `blocked-report` |

---

## Multi-Zone Support

### Zone configuration (`.env`)

Zones are configured as individual env vars with the pattern `CLOUDFLARE_ZONE_ID_<SUFFIX>`. The suffix becomes the zone label in reports (lowercased).

```
CLOUDFLARE_ZONE_ID_SK=c3b1e5531511633165727633127babbe
CLOUDFLARE_ZONE_SITEMAP_SK=https://gymbeam.sk/sitemap.xml

CLOUDFLARE_ZONE_ID_AT=230fb19d85399cd0ac628168cf2d3328
CLOUDFLARE_ZONE_SITEMAP_AT=https://gymbeam.at/sitemap.xml

CLOUDFLARE_ZONE_ID_CZ=a84b985f5b96106d9667eb38e6e81e91
CLOUDFLARE_ZONE_SITEMAP_CZ=https://gymbeam.cz/sitemap.xml
...
```

These appear in reports as zones `sk`, `at`, `cz`, `com`, etc.

Sitemaps (`CLOUDFLARE_ZONE_SITEMAP_*`) are optional — only required for `coverage-report`. Zones without a sitemap are skipped during coverage reports. The `blocked-report` command does not use sitemaps.

**Single zone fallback:** If no `CLOUDFLARE_ZONE_ID_*` vars are found, falls back to `CLOUDFLARE_ZONE_ID` with label `"default"`.

### Zone loading (`load_zones()`)

1. Scan all env vars for the `CLOUDFLARE_ZONE_ID_` prefix
2. Extract suffix as zone name (lowercased), value as zone ID
3. Pick up matching `CLOUDFLARE_ZONE_SITEMAP_<SUFFIX>` if set
4. Sort zones alphabetically by name
5. If none found, fall back to `CLOUDFLARE_ZONE_ID`
6. Exit with error if neither is set

### Execution flow

Both commands iterate over all zones sequentially. For each zone, all bot queries are executed before moving to the next zone. This keeps related API calls together and makes progress output clear:

```
============================================================
Zone [1/3]: gymbeam.sk
============================================================
[1/10] Processing GPTBot...
  Querying all traffic...
  Querying blocked (403) traffic...
[2/10] Processing ClaudeBot...
  ...

============================================================
Zone [2/3]: gymbeam.com
============================================================
...
```

---

## Bot Configuration (`bots.json`)

```json
{
  "bots": [
    { "name": "GPTBot", "user_agent_pattern": "%GPTBot%" },
    { "name": "ClaudeBot", "user_agent_pattern": "%ClaudeBot%" },
    { "name": "anthropic-ai", "user_agent_pattern": "%anthropic-ai%" },
    { "name": "Google-Extended", "user_agent_pattern": "%Google-Extended%" },
    { "name": "PerplexityBot", "user_agent_pattern": "%PerplexityBot%" },
    { "name": "CCBot", "user_agent_pattern": "%CCBot%" },
    { "name": "Bytespider", "user_agent_pattern": "%Bytespider%" },
    { "name": "Applebot-Extended", "user_agent_pattern": "%Applebot-Extended%" },
    { "name": "cohere-ai", "user_agent_pattern": "%cohere-ai%" },
    { "name": "ImagesiftBot", "user_agent_pattern": "%ImagesiftBot%" }
  ]
}
```

- `name` — display label in reports
- `user_agent_pattern` — value for `userAgent_like` filter in Cloudflare GraphQL

---

## Cloudflare Query Layer

### `query_cloudflare_paginated()`

Core paginated query function. Handles date chunking (max 7-day ranges) and cursor-based pagination.

```python
def query_cloudflare_paginated(auth_email, auth_key, zone_id, date_start, date_end,
                                user_agent_pattern, status_filter=None, delay=1.0):
```

**GraphQL query:**
```graphql
query {
  viewer {
    zones(filter: { zoneTag: "<zone_id>" }) {
      httpRequestsAdaptiveGroups(
        filter: {
          date_geq: "<date_start>"
          date_leq: "<date_end>"
          userAgent_like: "<pattern>"
          edgeResponseStatus: 403        # only when status_filter=403
          datetime_lt: "<cursor>"        # omitted on first page
        }
        limit: 1000
        orderBy: [datetime_DESC]
      ) {
        count
        dimensions {
          clientRequestPath
          clientRequestHTTPHost
          datetime
        }
      }
    }
  }
}
```

**Pagination:** `datetime_lt` cursor, max 100 pages, 1000 rows per page.

**Date chunking:** Ranges > 7 days are split into weekly chunks automatically.

**Retry logic:** Up to 3 retries with exponential backoff (2^attempt seconds) on HTTP 429 or 5xx.

Returns: `dict` mapping `(host, path)` → `total_count`

### `query_bot_blocking_stats()`

Calls `query_cloudflare_paginated` twice per bot:
1. `status_filter=None` → all traffic
2. `status_filter=403` → blocked traffic only

Returns: `(all_hits, blocked_hits)` — both `(host, path) → count` dicts.

---

## Report: `blocked-report`

### Page type classification

Each URL is classified into a page type using `classify_page_type()`. Rules are applied in order:

| Page Type      | Rule                                         |
|----------------|----------------------------------------------|
| `cdn`          | path starts with `/cdn-cgi`                  |
| `api`          | path starts with `/rest/`                    |
| `blog`         | path starts with `/blog/`                    |
| `media`        | path starts with `/media/`                   |
| `manufacturer` | path starts with `/manufacturer`             |
| `sales`        | path starts with `/sales/`                   |
| `static`       | path starts with `/_astro` or `/_next`       |
| `seo`          | path contains `sitemap.xml` or `robots.txt`  |
| `product`      | path ends with `.html`                       |
| `category`     | everything else (category listings, filters) |

### Data structure

```python
results = {}  # (zone_name, full_url) → {bot_name → {"total": N, "blocked": N}}
```

### CSV output: `~/Downloads/bothunter_blocked_report_YYYY-MM-DD.csv`

Columns:
```
zone, page_type, url, GPTBot_total, GPTBot_blocked, GPTBot_block_rate, ClaudeBot_total, ...
```

Rows sorted by total hits descending (summed across all bots).

### Terminal summary

Per-zone breakdown with bot-level stats, plus an "All Zones Combined" section when multiple zones are configured, followed by a "By Page Type" breakdown:

```
--- Blocked Report Summary ---
Period: 2026-03-29 to 2026-03-29
Zones:  sk

  Zone: sk
  Bot                      Total Hits      Blocked   Block Rate
  ------------------------------------------------------------
  GPTBot                        2,083            0         0.0%
  ChatGPT-User                 11,989            0         0.0%
  Googlebot                   408,489            1         0.0%
  bingbot                     335,231            0         0.0%
  ...
  ------------------------------------------------------------
  TOTAL                       921,233            9         0.0%
  Unique URLs: 35,774

  By Page Type
  Type                URLs   Total Hits      Blocked   Block Rate
  -------------------------------------------------------------
  category          11,283      695,174            1         0.0%
  product            5,729       92,601            0         0.0%
  blog               2,805       48,876            0         0.0%
  static               243       46,442            0         0.0%
  cdn               10,262       19,349            0         0.0%
  media              4,089       10,808            3         0.0%
  api                  310        4,705            0         0.0%
  sales                625        1,287            0         0.0%
  manufacturer         425        1,183            0         0.0%
  seo                    3          808            5         0.6%
  -------------------------------------------------------------
  TOTAL             35,774      921,233            9         0.0%

Report saved to:    ~/Downloads/bothunter_blocked_report_2026-03-30.csv
```

Bots with zero traffic across a zone are omitted from that zone's summary. Page types are sorted by total hits descending.

---

## Report: `coverage-report`

Each zone is validated against its own sitemap (configured via `CLOUDFLARE_ZONE_SITEMAP_*`). Zones without a sitemap are skipped.

### Data structure

```python
zone_results = []  # list of (zone_name, indexable_urls, hit_counts)
```

### CSV output: `~/Downloads/bothunter_coverage_report_YYYY-MM-DD.csv`

Columns:
```
zone, url, hit_by_gptbot, hit_count
```

One row per zone per indexable URL. Each zone's URLs are listed together.

### Terminal summary

Single table with all zones, plus a combined total row:

```
--- Coverage Report Summary ---

Zone     Indexable URLs   Hit by GPTBot   Coverage
--------------------------------------------------
at               8,340             315       3.8%
ba               4,735             230       4.9%
bg               4,984             199       4.0%
com              4,912             207       4.2%
cz               9,060             346       3.8%
de               8,536             229       2.7%
gr               5,862             235       4.0%
hr               5,815              32       0.6%
hu               8,665             304       3.5%
it               9,025             345       3.8%
nl               8,476             755       8.9%
pl               8,859             196       2.2%
ro               8,963              54       0.6%
rs               4,362             220       5.0%
si               4,934             250       5.1%
sk               9,148             444       4.9%
ua               4,814             198       4.1%
--------------------------------------------------
TOTAL          124,490           4,559       3.7%

Report saved to: ~/Downloads/bothunter_coverage_report_2026-03-22.csv
```

---

## CLI Usage

```bash
# GPTBot sitemap coverage report
bothunter coverage-report [--period {day,week,month}] [--zone ZONE]

# Per-bot blocking report
bothunter blocked-report [--period {day,week,month}] [--zone ZONE] [--bots-file PATH] [--delay SECONDS]

# Examples
bothunter coverage-report                        # Yesterday, all zones
bothunter coverage-report --zone sk              # Yesterday, SK only
bothunter coverage-report --period week          # Last 7 days, all zones
bothunter blocked-report --zone cz --period week # Last 7 days, CZ only
bothunter blocked-report --period month --delay 1 # Last 30 days, all zones

# Help
bothunter --help
```

**Arguments:**

| Argument | Default | Applies to | Description |
|----------|---------|------------|-------------|
| `--period` | `day` | both | `day` = yesterday, `week` = last 7 days, `month` = last 30 days |
| `--zone` | all zones | both | Run for a single zone only (e.g. `sk`, `cz`). Must match a `CLOUDFLARE_ZONE_ID_*` suffix. |
| `--bots-file` | `bots.json` | `blocked-report` | Path to bot config (absolute or relative to script dir) |
| `--delay` | `1.0` | `blocked-report` | Seconds between paginated API requests (rate limit avoidance) |

---

## Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_PAGES` | 100 | Max pagination pages per query chunk |
| `MAX_RETRIES` | 3 | Retry attempts on API failure |
| `PAGE_LIMIT` | 1000 | Cloudflare API row limit per page |
| `MAX_CHUNK_DAYS` | 7 | Max date range per query chunk |

---

## Verification

1. `bothunter coverage-report --period day` — queries all zones with sitemaps, single summary table
2. `bothunter coverage-report --zone sk` — queries SK zone only
3. `bothunter blocked-report --period day` — queries all zones per bot, per-zone terminal summary
4. `bothunter blocked-report --zone sk` — queries SK zone only
5. `bothunter blocked-report --zone xyz` — exits with error listing available zones
6. No `CLOUDFLARE_ZONE_ID_*` set, `CLOUDFLARE_ZONE_ID` set — falls back to single zone labeled `"default"`
7. Neither set — exits with clear error message
8. Zone without `CLOUDFLARE_ZONE_SITEMAP_*` — skipped in `coverage-report`, included in `blocked-report`
9. `--bots-file custom.json` — uses custom bot list
10. Missing `bots.json` — exits with error
11. No subcommand — prints help with usage examples
