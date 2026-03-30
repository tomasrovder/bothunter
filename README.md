# BotHunter

AI bot crawl report CLI — coverage and blocking analysis via Cloudflare Analytics.

## Installation

```bash
# Install pipx (if not installed)
brew install pipx
pipx ensurepath

# Install BotHunter
pipx install git+https://github.com/tomasrovder/bothunter.git
```

To install from a local clone:

```bash
git clone https://github.com/tomasrovder/bothunter.git
cd bothunter
pipx install .
```

## Configuration

Create a `.env` file in the directory where you'll run the tool:

```env
CLOUDFLARE_X-AUTH-EMAIL=your_email_here
CLOUDFLARE_X-AUTH-KEY=your_api_key_here

# Add one CLOUDFLARE_ZONE_ID_<SUFFIX> per zone
CLOUDFLARE_ZONE_ID_SK=your_zone_id
CLOUDFLARE_ZONE_SITEMAP_SK=https://example.com/sitemap.xml

CLOUDFLARE_ZONE_ID_CZ=your_zone_id
CLOUDFLARE_ZONE_SITEMAP_CZ=https://example.cz/sitemap.xml
```

The suffix (e.g. `SK`, `CZ`) becomes the zone label in reports. Sitemaps are only needed for `coverage-report`.

## Usage

```bash
# GPTBot sitemap coverage (yesterday, all zones)
bothunter coverage-report

# Coverage for a single zone, last 7 days
bothunter coverage-report --zone sk --period week

# Bot blocking stats (yesterday, all zones)
bothunter blocked-report

# Blocking stats for a single zone, last 30 days
bothunter blocked-report --zone sk --period month --delay 0.5
```

### Commands

| Command | Description |
|---|---|
| `coverage-report` | GPTBot sitemap coverage — checks which indexable URLs were hit |
| `blocked-report` | Per-bot blocking report — total vs blocked (403) traffic per bot per URL |

### Options

| Option | Values | Default | Description |
|---|---|---|---|
| `--period` | `day`, `week`, `month` | `day` | Time range: yesterday, last 7 days, or last 30 days |
| `--zone` | zone suffix | all zones | Run for a single zone (e.g. `--zone sk`) |
| `--delay` | seconds | `1.0` | Delay between API requests (blocked-report only) |
| `--bots-file` | path | `bots.json` | Custom bot config file (blocked-report only) |

## Tracked Bots

The `blocked-report` tracks these bots by default:

| Bot | Service |
|---|---|
| GPTBot | OpenAI (training) |
| ChatGPT-User | ChatGPT |
| ClaudeBot | Anthropic (training) |
| Claude-User | Claude |
| anthropic-ai | Anthropic |
| Google-Extended | Gemini (training) |
| Googlebot | Google Search |
| Google-Agent | Google Agent |
| AdsBot-Google | Google Ads |
| bingbot | Bing / Copilot |
| Applebot | Apple |
| Applebot-Extended | Apple (extended) |
| Bytespider | TikTok / ByteDance |
| SemrushBot | Semrush |
| DotBot | Moz |
| PerplexityBot | Perplexity AI |
| CCBot | Common Crawl |

Custom bots can be defined in a `bots.json` file (see `--bots-file`).

## Output

Reports are saved as CSV files to `~/Downloads/`:
- `bothunter_coverage_report_YYYY-MM-DD.csv`
- `bothunter_blocked_report_YYYY-MM-DD.csv`
