# Cloudflare GraphQL API — Technical Notes

## 1. API Basics

- **Endpoint:** `POST https://api.cloudflare.com/client/v4/graphql`
- **Auth header:** `Authorization: Bearer YOUR_API_TOKEN`
- **Content-Type:** `application/json`
- **Body format:** `{ "query": "..." }`
- API token requires `Zone → Analytics → Read` permission

---

## 2. Dataset: `httpRequestsAdaptiveGroups`

- Returns **aggregated** request counts, not raw individual events
- Grouped by the **unique combination of all requested dimensions**
- The more dimensions you include → the more granular (more rows) the result
- `count` = number of requests matching that dimension combination

### Available filter fields (confirmed working):
- `date_geq`, `date_leq`, `date_gt`, `date_lt` — date range (YYYY-MM-DD format)
- `clientRequestHTTPHost_like` — filter by hostname
- `clientRequestPath_like` — filter by path
- `edgeResponseStatus` — filter by HTTP response code (integer)

### Available dimension fields (confirmed working):
- `clientRequestPath` — URL path
- `clientRequestHTTPHost` — hostname
- `datetime` — groups by timestamp (increases row granularity)
- `date` — groups by day only
- `edgeResponseStatus` — HTTP response code (use this for 403 vs non-403 comparison)

### NOT available in this dataset:
- `userAgent` / `clientRequestUserAgent` — **does not exist** in dimensions
- `userAgent_like` — **does not exist** as a filter field
- No direct user agent filtering is possible in `httpRequestsAdaptiveGroups`

### Workaround for user agent filtering:
- Use `firewallEventsAdaptiveGroups` if requests trigger firewall rules (has `userAgent` field)
- Or use Logpush for full raw per-request user agent data

---

## 3. Dataset: `firewallEventsAdaptiveGroups`

- Returns aggregated firewall events only — requests that **triggered a firewall rule**
- Has `userAgent` in both dimensions and filters (`userAgent_like`)
- `count` = number of firewall events matching that dimension combination
- **Limitation:** Does NOT include requests that passed through without hitting a firewall rule

### Available dimension fields (confirmed working):
- `userAgent`
- `clientRequestPath`
- `clientRequestHTTPHost`
- `datetime`
- `date`
- `action` — firewall action taken (e.g. block, challenge, allow)

### Filter fields:
- `date_geq`, `date_lt` — date range
- `userAgent_like` — supports `%wildcard%` pattern matching

---

## 4. User Agent Filtering Strategy

Since `httpRequestsAdaptiveGroups` lacks user agent support, two approaches:

### Approach A — `firewallEventsAdaptiveGroups` (only blocked/challenged bots)
- Filter `userAgent_like` per bot directly in the API
- Must run a **separate query per user agent** (no OR filter support)
- Only captures requests that hit a firewall rule

### Approach B — Logpush (all bot traffic)
- Full `ClientRequestUserAgent` field on every request
- Filter post-collection in code
- Requires Pro plan or above

### User Agent List (to be provided — run one query per entry):
```
GPTBot
ClaudeBot
anthropic-ai
Google-Extended
PerplexityBot
CCBot
Bytespider
Applebot-Extended
cohere-ai
ImagesiftBot
```
Each entry should be queried as `userAgent_like: "%GPTBot%"` etc.

---

## 5. Blocking Rate Calculation (403 vs All)

To calculate the rate at which bots were blocked, query `edgeResponseStatus` as a dimension in `httpRequestsAdaptiveGroups`:

```graphql
httpRequestsAdaptiveGroups(
  filter: {
    date_geq: "2026-03-19"
    date_leq: "2026-03-19"
  }
  limit: 1000
  orderBy: [count_DESC]
) {
  count
  dimensions {
    clientRequestPath
    clientRequestHTTPHost
    edgeResponseStatus
    date
  }
}
```

Then compute in code:
```
blockRate = count(edgeResponseStatus == 403) / count(all) * 100
```

**Note:** Since user agent is not filterable in this dataset, you cannot directly tie response codes to specific bots via GraphQL alone. You would need to cross-reference with `firewallEventsAdaptiveGroups` data or use Logpush.

---

## 6. Pagination

- Cloudflare GraphQL has **no native cursor/offset pagination**
- Max `limit` per query is **1000 rows**
- Pagination must be implemented manually using the **last record's `datetime` as a cursor**

### Pagination Strategy:
1. Query with `orderBy: [datetime_DESC]`, `limit: 1000`
2. If results returned < 1000 → last page, stop
3. If results = 1000 → take the `datetime` of the last record
4. Next query: add `datetime_lt: "<last_datetime>"` to the filter
5. Repeat until done

### Important pagination notes:
- Use `datetime_lt` (strict less than) to avoid duplicating the boundary record
- `datetime` format is ISO 8601: `"2026-03-19T10:30:00Z"`
- For date-only grouping (`date` dimension instead of `datetime`), pagination is less of an issue as there are only up to 365 unique dates per year
- If a single day has >1000 unique dimension combinations, pagination is needed

---

## 7. Querying Across All Zones (Domains)

### Implemented approach — Sequential per-zone queries:

BotHunter queries each zone individually using `zoneTag` (not `zoneTag_in`). Zones are configured in `.env` as individual env vars:

```
CLOUDFLARE_ZONE_ID_SK=abc...
CLOUDFLARE_ZONE_ID_AT=def...
CLOUDFLARE_ZONE_ID_COM=ghi...
```

For each zone, all bot queries (total + blocked) run sequentially before moving to the next zone. Results are keyed by `(zone_name, url)` and merged into a single CSV with a `zone` column.

This approach was chosen over `zoneTag_in` because:
- Each zone's data stays clearly separated in the output
- Avoids the undocumented `zoneTag_in` batch size limit
- Progress output is clearer (shows which zone is being queried)
- Simpler pagination — each query targets a single zone

### Alternative approaches (not used):

**Option A — `zoneTag_in` in a single request:**
```graphql
viewer {
  zones(filter: { zoneTag_in: ["ZONE_ID_1", "ZONE_ID_2"] }) { ... }
}
```
Returns each zone's data as a separate array element. Has an undocumented batch size limit (~10 zones).

**Option B — REST API zone discovery:**
```
GET https://api.cloudflare.com/client/v4/zones?per_page=50
```
Auto-discovers all zones on the account. Not used because explicit zone configuration is preferred.

---

## 8. General Limitations

| Limitation | Detail |
|---|---|
| Max rows per query | 1,000 |
| No native pagination | Must use `datetime_lt` cursor manually |
| No OR filters | Cannot do `userAgent_like: "%GPTBot% OR %ClaudeBot%"` — run separate queries per bot |
| No user agent in `httpRequestsAdaptiveGroups` | Use `firewallEventsAdaptiveGroups` or Logpush |
| `firewallEventsAdaptive` only captures firewall-triggered requests | Does not represent all traffic |
| Date range limit | Queries spanning >30 days may be rejected or return partial data |
| Rate limiting | Cloudflare throttles GraphQL API — add delays between paginated requests |
| `zoneTag_in` batch size | Undocumented — keep batches small (~10 zones) to be safe |
| Aggregation granularity | Grouping by too many dimensions spreads count across many rows |

---

## 9. Implemented Architecture

```
1. Load zone list from CLOUDFLARE_ZONES env var (JSON array)
2. For each zone:
   a. For each bot user agent in bots.json:
      - Query httpRequestsAdaptiveGroups with userAgent_like filter (all traffic)
      - Query httpRequestsAdaptiveGroups with userAgent_like + edgeResponseStatus: 403 (blocked)
      - Paginate using datetime_lt cursor, chunk date ranges into 7-day windows
      - Aggregate (host, path) → count
   b. Merge into results keyed by (zone_name, full_url)
3. Generate single CSV with zone column + per-bot total/blocked/rate columns
4. Print per-zone terminal summary + combined totals
```

### Note on `userAgent_like`

Earlier testing indicated `userAgent_like` was not available in `httpRequestsAdaptiveGroups`. In practice, it does work as a filter field (just not as a dimension). The implementation uses it directly in `httpRequestsAdaptiveGroups` rather than `firewallEventsAdaptiveGroups`, which allows capturing all bot traffic (not just firewall-triggered requests).
