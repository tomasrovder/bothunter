#!/usr/bin/env python3
"""BotHunter - AI bot crawl report CLI for GymBeam sitemap URLs."""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import requests
from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent

CLOUDFLARE_GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
SITEMAP_NS = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}

MAX_PAGES = 100
MAX_RETRIES = 3
PAGE_LIMIT = 1000
MAX_CHUNK_DAYS = 7  # Cloudflare httpRequestsAdaptiveGroups max range is 1w1d


def fetch_sitemap_index(url):
    """Fetch the sitemap index and return child sitemap URLs, skipping product_pictures."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.content)
    urls = []
    for sitemap in root.findall("ns:sitemap", SITEMAP_NS):
        loc = sitemap.find("ns:loc", SITEMAP_NS)
        if loc is not None and loc.text:
            if "product_pictures" not in loc.text:
                urls.append(loc.text.strip())
    return urls


def fetch_sitemap_urls(sitemap_url):
    """Fetch a single sitemap and return all <loc> URLs."""
    resp = requests.get(sitemap_url, timeout=30)
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.content)
    urls = []
    for url_elem in root.findall("ns:url", SITEMAP_NS):
        loc = url_elem.find("ns:loc", SITEMAP_NS)
        if loc is not None and loc.text:
            urls.append(loc.text.strip().rstrip("/"))
    return urls


def get_indexable_urls(sitemap_url):
    """Fetch all indexable URLs from a sitemap index URL."""
    print(f"Fetching sitemap index: {sitemap_url}")
    child_sitemaps = fetch_sitemap_index(sitemap_url)
    print(f"Found {len(child_sitemaps)} child sitemaps (excluding product_pictures)")

    all_urls = []
    for i, sitemap_url in enumerate(child_sitemaps, 1):
        print(f"  Fetching sitemap {i}/{len(child_sitemaps)}: {sitemap_url}")
        urls = fetch_sitemap_urls(sitemap_url)
        all_urls.extend(urls)

    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for url in all_urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    print(f"Total indexable URLs: {len(unique_urls)}")
    return unique_urls


def load_bots_config(bots_file):
    """Load and validate bot configuration from a JSON file."""
    path = Path(bots_file)
    if not path.exists():
        print(f"Error: Bots config file not found: {bots_file}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {bots_file}: {e}", file=sys.stderr)
        sys.exit(1)

    bots = config.get("bots")
    if not bots or not isinstance(bots, list):
        print(f"Error: {bots_file} must contain a non-empty 'bots' array", file=sys.stderr)
        sys.exit(1)

    for i, bot in enumerate(bots):
        if not bot.get("name") or not bot.get("user_agent_pattern"):
            print(
                f"Error: Bot entry {i} in {bots_file} must have 'name' and 'user_agent_pattern'",
                file=sys.stderr,
            )
            sys.exit(1)

    return config


def load_zones():
    """Load zone configuration from CLOUDFLARE_ZONE_ID_* env vars.

    Scans for env vars matching CLOUDFLARE_ZONE_ID_<SUFFIX> (e.g. CLOUDFLARE_ZONE_ID_SK).
    The suffix becomes the zone label (lowercased).
    Optionally picks up CLOUDFLARE_ZONE_SITEMAP_<SUFFIX> for the report command.
    Returns a list of dicts with 'name', 'id', and optional 'sitemap' keys, sorted by name.
    """
    prefix = "CLOUDFLARE_ZONE_ID_"
    sitemap_prefix = "CLOUDFLARE_ZONE_SITEMAP_"
    zones = []
    for key, value in os.environ.items():
        if key.startswith(prefix) and value:
            suffix = key[len(prefix):]
            zone = {"name": suffix.lower(), "id": value}
            sitemap = os.getenv(f"{sitemap_prefix}{suffix}")
            if sitemap:
                zone["sitemap"] = sitemap
            zones.append(zone)

    if zones:
        zones.sort(key=lambda z: z["name"])
        return zones

    # Fallback to single CLOUDFLARE_ZONE_ID
    zone_id = os.getenv("CLOUDFLARE_ZONE_ID")
    if not zone_id:
        print(
            "Error: Set CLOUDFLARE_ZONE_ID_<SUFFIX> vars (e.g. CLOUDFLARE_ZONE_ID_SK) in .env",
            file=sys.stderr,
        )
        sys.exit(1)

    return [{"name": "default", "id": zone_id}]


def _date_chunks(date_start, date_end):
    """Split a date range into chunks of MAX_CHUNK_DAYS days each.

    Yields (chunk_start, chunk_end) as ISO date strings.
    """
    from datetime import date as date_cls
    start = date_cls.fromisoformat(date_start)
    end = date_cls.fromisoformat(date_end)
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=MAX_CHUNK_DAYS - 1), end)
        yield chunk_start.isoformat(), chunk_end.isoformat()
        chunk_start = chunk_end + timedelta(days=1)


def _query_cloudflare_single_chunk(headers, zone_id, chunk_start, chunk_end,
                                    user_agent_pattern, status_filter, delay):
    """Query a single date chunk with pagination. Returns (host, path) -> count dict."""
    hit_counts = {}
    cursor = None

    for page in range(1, MAX_PAGES + 1):
        # Build filter
        filter_parts = [
            f'date_geq: "{chunk_start}"',
            f'date_leq: "{chunk_end}"',
            f'userAgent_like: "{user_agent_pattern}"',
        ]
        if status_filter is not None:
            filter_parts.append(f"edgeResponseStatus: {status_filter}")
        if cursor is not None:
            filter_parts.append(f'datetime_lt: "{cursor}"')

        filter_str = "\n              ".join(filter_parts)

        query = f"""
        query {{
          viewer {{
            zones(filter: {{ zoneTag: "{zone_id}" }}) {{
              httpRequestsAdaptiveGroups(
                filter: {{
                  {filter_str}
                }}
                limit: {PAGE_LIMIT}
                orderBy: [datetime_DESC]
              ) {{
                count
                dimensions {{
                  clientRequestPath
                  clientRequestHTTPHost
                  datetime
                }}
              }}
            }}
          }}
        }}
        """

        # Execute with retry logic
        resp = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    CLOUDFLARE_GRAPHQL_URL,
                    json={"query": query},
                    headers=headers,
                    timeout=60,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = 2 ** attempt
                    print(f"    HTTP {resp.status_code}, retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                if attempt == MAX_RETRIES:
                    raise
                wait = 2 ** attempt
                print(f"    Request error: {e}, retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})...")
                time.sleep(wait)

        data = resp.json()

        if data.get("errors"):
            print("Cloudflare API errors:", file=sys.stderr)
            for err in data["errors"]:
                print(f"  {err.get('message', err)}", file=sys.stderr)
            sys.exit(1)

        zones = data.get("data", {}).get("viewer", {}).get("zones", [])
        if not zones:
            print("No zone data returned. Check your CLOUDFLARE_ZONE_ID.", file=sys.stderr)
            sys.exit(1)

        groups = zones[0].get("httpRequestsAdaptiveGroups", [])

        for group in groups:
            dims = group["dimensions"]
            host = dims["clientRequestHTTPHost"]
            path = dims["clientRequestPath"]
            hit_counts[(host, path)] = hit_counts.get((host, path), 0) + group["count"]

        # Check if we need more pages
        if len(groups) < PAGE_LIMIT:
            break

        # Set cursor to last row's datetime
        cursor = groups[-1]["dimensions"]["datetime"]

        if page < MAX_PAGES:
            time.sleep(delay)

    return hit_counts


def query_cloudflare_paginated(auth_email, auth_key, zone_id, date_start, date_end,
                                user_agent_pattern, status_filter=None, delay=1.0):
    """Query Cloudflare httpRequestsAdaptiveGroups with pagination and date chunking.

    Automatically splits ranges longer than MAX_CHUNK_DAYS into weekly chunks
    to stay within Cloudflare's query limits.

    Returns a dict mapping (host, path) -> total_count.
    """
    headers = {
        "X-Auth-Email": auth_email,
        "X-Auth-Key": auth_key,
        "Content-Type": "application/json",
    }

    chunks = list(_date_chunks(date_start, date_end))
    if len(chunks) > 1:
        print(f"    Date range split into {len(chunks)} weekly chunks")

    hit_counts = {}
    for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"    Chunk {i}/{len(chunks)}: {chunk_start} to {chunk_end}")

        chunk_hits = _query_cloudflare_single_chunk(
            headers, zone_id, chunk_start, chunk_end,
            user_agent_pattern, status_filter, delay,
        )

        for key, count in chunk_hits.items():
            hit_counts[key] = hit_counts.get(key, 0) + count

    return hit_counts


def query_bot_blocking_stats(auth_email, auth_key, zone_id, date_start, date_end,
                              bot_name, user_agent_pattern, delay=1.0):
    """Query all traffic and blocked (403) traffic for a single bot.

    Returns (all_hits, blocked_hits) — both dicts of (host, path) -> count.
    """
    print(f"  Querying all traffic for {bot_name}...")
    all_hits = query_cloudflare_paginated(
        auth_email, auth_key, zone_id, date_start, date_end,
        user_agent_pattern, status_filter=None, delay=delay,
    )
    total = sum(all_hits.values())
    print(f"    All traffic: {total:,} hits across {len(all_hits)} URLs")

    print(f"  Querying blocked (403) traffic for {bot_name}...")
    blocked_hits = query_cloudflare_paginated(
        auth_email, auth_key, zone_id, date_start, date_end,
        user_agent_pattern, status_filter=403, delay=delay,
    )
    blocked = sum(blocked_hits.values())
    print(f"    Blocked traffic: {blocked:,} hits across {len(blocked_hits)} URLs")

    return all_hits, blocked_hits


def classify_page_type(url):
    """Classify a URL into a page type based on its path."""
    from urllib.parse import urlparse
    path = urlparse(url).path

    if path.startswith("/cdn-cgi"):
        return "cdn"
    if path.startswith("/rest/"):
        return "api"
    if path.startswith("/blog/"):
        return "blog"
    if path.startswith("/media/"):
        return "media"
    if path.startswith("/manufacturer"):
        return "manufacturer"
    if path.startswith("/sales/"):
        return "sales"
    if path.startswith("/_astro") or path.startswith("/_next"):
        return "static"
    if "sitemap.xml" in path or "robots.txt" in path:
        return "seo"
    if path.endswith(".html"):
        return "product"
    return "category"


def generate_blocked_report(results, bots, zones, date_start, date_end, output_path):
    """Generate CSV and terminal summary for the blocked report.

    results: dict of (zone_name, full_url) -> {bot_name -> {"total": N, "blocked": N}}
    bots: list of bot config dicts
    zones: list of zone config dicts
    """
    bot_names = [b["name"] for b in bots]
    zone_names = [z["name"] for z in zones]

    # Classify page types
    page_types = {key: classify_page_type(key[1]) for key in results}

    # Build CSV header
    header = ["zone", "page_type", "url"]
    for name in bot_names:
        header.extend([f"{name}_total", f"{name}_blocked", f"{name}_block_rate"])

    # Sort rows by total hits descending (sum across all bots)
    def total_hits_for_key(key):
        bot_data = results[key]
        return sum(bot_data.get(name, {}).get("total", 0) for name in bot_names)

    sorted_keys = sorted(results.keys(), key=total_hits_for_key, reverse=True)

    # Write CSV
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for zone_name, url in sorted_keys:
            row = [zone_name, page_types[(zone_name, url)], url]
            bot_data = results[(zone_name, url)]
            for name in bot_names:
                stats = bot_data.get(name, {"total": 0, "blocked": 0})
                total = stats["total"]
                blocked = stats["blocked"]
                rate = (blocked / total * 100) if total > 0 else 0.0
                row.extend([total, blocked, f"{rate:.1f}%"])
            writer.writerow(row)

    # Terminal summary
    print(f"\n--- Blocked Report Summary ---")
    print(f"Period: {date_start} to {date_end}")
    print(f"Zones:  {', '.join(zone_names)}")
    print()

    # Per-zone per-bot totals
    for zone_name in zone_names:
        zone_keys = [k for k in results if k[0] == zone_name]
        if not zone_keys:
            continue

        print(f"  Zone: {zone_name}")
        print(f"  {'Bot':<22} {'Total Hits':>12} {'Blocked':>12} {'Block Rate':>12}")
        print(f"  {'-' * 60}")

        zone_total = 0
        zone_blocked = 0
        for name in bot_names:
            t = sum(results[k].get(name, {}).get("total", 0) for k in zone_keys)
            b = sum(results[k].get(name, {}).get("blocked", 0) for k in zone_keys)
            zone_total += t
            zone_blocked += b
            if t > 0:
                rate = b / t * 100
                print(f"  {name:<22} {t:>12,} {b:>12,} {rate:>11.1f}%")

        print(f"  {'-' * 60}")
        zone_rate = (zone_blocked / zone_total * 100) if zone_total > 0 else 0.0
        print(f"  {'TOTAL':<22} {zone_total:>12,} {zone_blocked:>12,} {zone_rate:>11.1f}%")
        print(f"  Unique URLs: {len(zone_keys):,}")
        print()

    # Grand totals across all zones
    if len(zone_names) > 1:
        print(f"  All Zones Combined")
        print(f"  {'Bot':<22} {'Total Hits':>12} {'Blocked':>12} {'Block Rate':>12}")
        print(f"  {'-' * 60}")

        grand_total = 0
        grand_blocked = 0
        for name in bot_names:
            t = sum(results[k].get(name, {}).get("total", 0) for k in results)
            b = sum(results[k].get(name, {}).get("blocked", 0) for k in results)
            grand_total += t
            grand_blocked += b
            if t > 0:
                rate = b / t * 100
                print(f"  {name:<22} {t:>12,} {b:>12,} {rate:>11.1f}%")

        print(f"  {'-' * 60}")
        grand_rate = (grand_blocked / grand_total * 100) if grand_total > 0 else 0.0
        print(f"  {'TOTAL':<22} {grand_total:>12,} {grand_blocked:>12,} {grand_rate:>11.1f}%")
        print()

    # Per page type summary
    print(f"  By Page Type")
    print(f"  {'Type':<15} {'URLs':>8} {'Total Hits':>12} {'Blocked':>12} {'Block Rate':>12}")
    print(f"  {'-' * 61}")

    type_stats = {}
    for key in results:
        pt = page_types[key]
        if pt not in type_stats:
            type_stats[pt] = {"urls": 0, "total": 0, "blocked": 0}
        type_stats[pt]["urls"] += 1
        for name in bot_names:
            type_stats[pt]["total"] += results[key].get(name, {}).get("total", 0)
            type_stats[pt]["blocked"] += results[key].get(name, {}).get("blocked", 0)

    pt_total = 0
    pt_blocked = 0
    for pt in sorted(type_stats, key=lambda x: type_stats[x]["total"], reverse=True):
        s = type_stats[pt]
        pt_total += s["total"]
        pt_blocked += s["blocked"]
        rate = (s["blocked"] / s["total"] * 100) if s["total"] > 0 else 0.0
        print(f"  {pt:<15} {s['urls']:>8,} {s['total']:>12,} {s['blocked']:>12,} {rate:>11.1f}%")

    print(f"  {'-' * 61}")
    pt_rate = (pt_blocked / pt_total * 100) if pt_total > 0 else 0.0
    print(f"  {'TOTAL':<15} {len(results):>8,} {pt_total:>12,} {pt_blocked:>12,} {pt_rate:>11.1f}%")
    print()

    print(f"Report saved to:    {output_path}")


def run_blocked_report(auth_email, auth_key, zones, date_start, date_end,
                        bots_config, delay):
    """Orchestrate the blocked-report: query all bots across all zones and generate report."""
    bots = bots_config["bots"]
    results = {}  # (zone_name, full_url) -> {bot_name -> {"total": N, "blocked": N}}

    for zi, zone in enumerate(zones, 1):
        zone_name = zone["name"]
        zone_id = zone["id"]
        print(f"\n{'='*60}")
        print(f"Zone [{zi}/{len(zones)}]: {zone_name}")
        print(f"{'='*60}")

        for i, bot in enumerate(bots, 1):
            bot_name = bot["name"]
            pattern = bot["user_agent_pattern"]
            print(f"\n[{i}/{len(bots)}] Processing {bot_name}...")

            all_hits, blocked_hits = query_bot_blocking_stats(
                auth_email, auth_key, zone_id, date_start, date_end,
                bot_name, pattern, delay=delay,
            )

            # Merge into results
            all_keys = set(all_hits.keys()) | set(blocked_hits.keys())
            for host, path in all_keys:
                full_url = f"https://{host}{path}".rstrip("/")
                key = (zone_name, full_url)
                if key not in results:
                    results[key] = {}
                results[key][bot_name] = {
                    "total": all_hits.get((host, path), 0),
                    "blocked": blocked_hits.get((host, path), 0),
                }

    # Generate report
    output_dir = Path.home() / "Downloads"
    output_dir.mkdir(exist_ok=True)
    today = datetime.now().date().isoformat()
    output_path = output_dir / f"bothunter_blocked_report_{today}.csv"

    generate_blocked_report(results, bots, zones, date_start, date_end, output_path)


def query_gptbot_hits(auth_email, auth_key, zone_id, date_start, date_end, delay=1.0):
    """Query GPTBot hits using paginated queries. Returns url -> count dict."""
    print(f"  Querying GPTBot hits ({date_start} to {date_end})...")
    raw_hits = query_cloudflare_paginated(
        auth_email, auth_key, zone_id, date_start, date_end,
        user_agent_pattern="%GPTBot%", status_filter=None, delay=delay,
    )
    # Convert (host, path) keys to full URLs
    hit_counts = {}
    for (host, path), count in raw_hits.items():
        full_url = f"https://{host}{path}".rstrip("/")
        hit_counts[full_url] = hit_counts.get(full_url, 0) + count

    total = sum(hit_counts.values())
    print(f"    GPTBot traffic: {total:,} hits across {len(hit_counts)} URLs")
    return hit_counts


def generate_report(zone_results, output_path):
    """Generate a CSV report and print summary stats.

    zone_results: list of (zone_name, indexable_urls, hit_counts) tuples
    """
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["zone", "url", "hit_by_gptbot", "hit_count"])
        for zone_name, indexable_urls, hit_counts in zone_results:
            for url in indexable_urls:
                count = hit_counts.get(url, 0)
                hit = count > 0
                writer.writerow([zone_name, url, str(hit).lower(), count])

    # Terminal summary
    print(f"\n--- Coverage Report Summary ---")
    print()
    print(f"{'Zone':<8} {'Indexable URLs':>15} {'Hit by GPTBot':>15} {'Coverage':>10}")
    print(f"{'-' * 50}")

    grand_total = 0
    grand_hit = 0
    for zone_name, indexable_urls, hit_counts in zone_results:
        hit_total = sum(1 for url in indexable_urls if hit_counts.get(url, 0) > 0)
        total = len(indexable_urls)
        pct = (hit_total / total * 100) if total else 0
        grand_total += total
        grand_hit += hit_total
        print(f"{zone_name:<8} {total:>15,} {hit_total:>15,} {pct:>9.1f}%")

    if len(zone_results) > 1:
        print(f"{'-' * 50}")
        grand_pct = (grand_hit / grand_total * 100) if grand_total else 0
        print(f"{'TOTAL':<8} {grand_total:>15,} {grand_hit:>15,} {grand_pct:>9.1f}%")

    print()
    print(f"Report saved to: {output_path}")


def run_report(auth_email, auth_key, zones, date_start, date_end):
    """Run the GPTBot sitemap report across all zones, each with its own sitemap."""
    zone_results = []  # list of (zone_name, indexable_urls, hit_counts)

    for zi, zone in enumerate(zones, 1):
        zone_name = zone["name"]
        zone_id = zone["id"]
        sitemap = zone.get("sitemap")

        print(f"\n{'='*60}")
        print(f"Zone [{zi}/{len(zones)}]: {zone_name}")
        print(f"{'='*60}")

        if not sitemap:
            print(f"  Skipping — no CLOUDFLARE_ZONE_SITEMAP_{zone_name.upper()} set")
            continue

        indexable_urls = get_indexable_urls(sitemap)
        hit_counts = query_gptbot_hits(
            auth_email, auth_key, zone_id, date_start, date_end,
        )
        zone_results.append((zone_name, indexable_urls, hit_counts))

    if not zone_results:
        print("No zones with sitemaps configured. Nothing to report.", file=sys.stderr)
        sys.exit(1)

    yesterday = datetime.now().date() - timedelta(days=1)
    output_dir = Path.home() / "Downloads"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / f"bothunter_coverage_report_{yesterday.isoformat()}.csv"

    generate_report(zone_results, output_path)


def main():
    parser = argparse.ArgumentParser(
        prog="bothunter",
        description="BotHunter - AI bot crawl report",
        epilog="""examples:
  bothunter coverage-report --period day       Yesterday's GPTBot sitemap coverage
  bothunter coverage-report --period week      Last 7 days coverage
  bothunter blocked-report --period day        Yesterday's blocking stats, all zones
  bothunter blocked-report --zone sk           Yesterday's blocking stats, SK only
  bothunter blocked-report --period month      Last 30 days blocking stats
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # coverage-report subcommand (GPTBot sitemap coverage report)
    report_parser = subparsers.add_parser(
        "coverage-report",
        help="GPTBot sitemap coverage report — checks which indexable URLs were hit",
        epilog="""examples:
  bothunter coverage-report                  Yesterday, all zones
  bothunter coverage-report --zone sk        Yesterday, SK zone only
  bothunter coverage-report --period week    Last 7 days, all zones
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    report_parser.add_argument(
        "--period",
        choices=["day", "week", "month"],
        default="day",
        help="day = yesterday (default), week = last 7 days, month = last 30 days",
    )
    report_parser.add_argument(
        "--zone",
        default=None,
        help="Run only for a single zone (e.g. --zone sk). Must match a CLOUDFLARE_ZONE_ID_* suffix.",
    )

    # blocked-report subcommand (per-bot blocking report)
    blocked_parser = subparsers.add_parser(
        "blocked-report",
        help="Per-bot blocking report — total vs blocked (403) traffic per bot per URL",
        epilog="""examples:
  bothunter blocked-report                           Yesterday, all zones
  bothunter blocked-report --zone sk                 Yesterday, SK zone only
  bothunter blocked-report --period week --zone cz   Last 7 days, CZ zone only
  bothunter blocked-report --period month --delay 1  Last 30 days, all zones
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    blocked_parser.add_argument(
        "--period",
        choices=["day", "week", "month"],
        default="day",
        help="day = yesterday (default), week = last 7 days, month = last 30 days",
    )
    blocked_parser.add_argument(
        "--bots-file",
        default="bots.json",
        help="Path to bot configuration JSON file (default: bots.json)",
    )
    blocked_parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between paginated API requests (default: 1.0)",
    )
    blocked_parser.add_argument(
        "--zone",
        default=None,
        help="Run only for a single zone (e.g. --zone sk). Must match a CLOUDFLARE_ZONE_ID_* suffix.",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    load_dotenv()  # searches .env in CWD and parent dirs
    auth_email = os.getenv("CLOUDFLARE_X-AUTH-EMAIL")
    auth_key = os.getenv("CLOUDFLARE_X-AUTH-KEY")

    if not auth_email or not auth_key:
        print(
            "Error: CLOUDFLARE_X-AUTH-EMAIL and CLOUDFLARE_X-AUTH-KEY must be set in .env",
            file=sys.stderr,
        )
        sys.exit(1)

    zones = load_zones()

    yesterday = datetime.now().date() - timedelta(days=1)
    if args.period == "day":
        date_start = yesterday.isoformat()
        date_end = yesterday.isoformat()
    elif args.period == "week":
        date_start = (yesterday - timedelta(days=6)).isoformat()
        date_end = yesterday.isoformat()
    else:
        date_start = (yesterday - timedelta(days=29)).isoformat()
        date_end = yesterday.isoformat()

    if args.zone:
        target = args.zone.lower()
        zones = [z for z in zones if z["name"] == target]
        if not zones:
            all_zones = load_zones()
            print(f"Error: Zone '{target}' not found. Available zones: {', '.join(z['name'] for z in all_zones)}", file=sys.stderr)
            sys.exit(1)

    if args.command == "coverage-report":
        run_report(auth_email, auth_key, zones, date_start, date_end)

    elif args.command == "blocked-report":
        bots_file = Path(args.bots_file)
        if not bots_file.is_absolute() and not bots_file.exists():
            # Fall back to package-bundled bots.json
            bots_file = PACKAGE_DIR / bots_file
        bots_config = load_bots_config(bots_file)
        run_blocked_report(
            auth_email, auth_key, zones, date_start, date_end,
            bots_config, args.delay,
        )


if __name__ == "__main__":
    main()
