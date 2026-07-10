# local-leads-automation

Lead generation tool for web developers. Extracts local businesses from Google Maps, analyzes their website quality, and generates personalized outreach emails using an LLM via OpenRouter.

**Use case:** Find businesses with poor websites (insecure, no SEO, outdated) and contact them offering improvement services.

---

## Pipeline

```
SegurSEO-API (Angular → Laravel)
  → scrape businesses from Google Maps (name, phone, address, city, province)
  → analyze website (CMS, email, social networks, SEO score)
  → filter contactable leads (email or any social network)
  → generate personalized outreach email (OpenRouter LLM)
  → report results back to the API
  → assisted manual outreach
```

---

## Requirements

- Python 3.13+
- OpenRouter API key (for message generation)
- SegurSEO-API running with a valid worker token

---

## Installation

```bash
git clone <repo-url>
cd local-leads-automation

python -m venv .venv
source .venv/bin/activate       # Linux/macOS
# .venv\Scripts\activate        # Windows

pip install -r requirements.txt

# Install Playwright browser
playwright install chromium

# Configure environment variables
cp .env.example .env
# Edit .env with your API credentials, OpenRouter key, and sender identity
```

---

## Usage

### Worker daemon

```bash
python worker.py
```

Runs continuously, polling SegurSEO-API for pending search and analysis jobs.
Requires `API_BASE_URL` and `API_TOKEN` in `.env`. Jobs are created via the
Angular frontend — results are reported straight to the API via
`report_leads()` / `report_analysis()`.

---

## Output fields

| Field | Source | Description |
|---|---|---|
| `lead` | Maps | Business name |
| `website` | Maps | Website hostname |
| `maps_url` | Maps | Google Maps listing URL |
| `phone` | Maps | Phone number |
| `address` | Maps | Street address |
| `zip_code` | Maps | Postal code |
| `city` | Maps | City (from address, or `**city**` if inferred from search) |
| `province` | Maps | Province |
| `cms` | Web | Detected CMS: `wordpress`, `wix`, `squarespace`, `shopify`, `unknown`, `unreachable` |
| `email` | Web | Contact email |
| `instagram` … `tiktok` | Web | Social media profile URLs |
| `seo_score` | Web | 0–100 (100 − 10 per issue found) |
| `seo_issues` | Web | Pipe-separated detected issues (see below) |
| `email_subject` | AI | Generated email subject line |
| `email_body` | AI | Generated email body (ready to send) |

### SEO issues detected

| Issue | Description |
|---|---|
| `no_https` | Site not served over HTTPS |
| `no_title` | Missing `<title>` tag |
| `no_meta_description` | Missing meta description |
| `no_h1` / `multiple_h1` | Missing or duplicate H1 heading |
| `no_viewport` | Not configured for mobile |
| `no_canonical` | Missing canonical URL |
| `no_lang` | Missing language declaration |
| `no_og_tags` | Missing Open Graph tags for social sharing |
| `no_structured_data` | Missing JSON-LD / schema.org markup |
| `no_alt_images` | Images without alt text |
| `no_analytics` | No Google Analytics or Tag Manager detected |
| `no_favicon` | Missing favicon |
| `no_sitemap` | No `/sitemap.xml` found |
| `no_robots` | No `/robots.txt` found |

---

## Project Structure

```
worker.py                    # Daemon: polls SegurSEO-API job queues, drives scraping/analysis
scraper/
  maps_scraper.py            # Scrapes businesses from Google Maps
  web_analyzer.py            # CMS detection, contacts, SEO scoring
ai/
  message_generator.py       # Generates outreach emails via OpenRouter
api/
  client.py                  # SegurSEO-API job-queue client (used by worker.py)
config.py                    # Scraper, OpenRouter, sender, and API worker configuration
.env                         # Secrets (do not commit)
```

---

## Development Phases

- [x] **Phase 0** — Basic prototype: extracts name and website from Google Maps
- [x] **Phase 1** — Robust Maps scraper: unlimited scroll, address/phone/location, rate limiting with jitter, exponential backoff
- [x] **Phase 2** — Web analyzer: CMS detection, email extraction, social networks, SEO scoring (14 checks)
- [x] **Phase 3** — Message generation: OpenRouter LLM (DeepSeek), contactable lead filtering, full pipeline
- [x] **Phase 4** — CLI flags, Streamlit dashboard, API client, JSON output, full refactor
- [x] **Phase 5** — SegurSEO-API job-queue integration: `worker.py` daemon, `scrape_incrementally()`, domain deduplication
- [x] **Phase 6** — Removed local pipeline (`main.py`, `app.py`, Streamlit/pandas) — driven exclusively by the API

---

## Notes

- Delays are set to 3–6 seconds between scraping actions to simulate human behavior.
- Message sending is **semi-manual** — AI-generated drafts are reviewed before sending, in compliance with GDPR.
- Message generation calls the OpenRouter API (`OPENROUTER_API_KEY` required in `.env`).
- Leads without any reachable contact channel (email or social network) are skipped in message generation.
