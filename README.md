# local-leads-automation

Lead generation tool for web developers. Extracts local businesses from Google Maps, analyzes their website quality, and generates personalized outreach emails using a local LLM.

**Use case:** Find businesses with poor websites (insecure, no SEO, outdated) and contact them offering improvement services.

---

## Pipeline

```
Google Maps
  → scrape businesses (name, phone, address, city, province)
  → analyze website (CMS, email, social networks, SEO score)
  → filter contactable leads (email, phone, or any social network)
  → generate personalized outreach email (local LLM)
  → save to leads.csv
  → assisted manual outreach
```

---

## Requirements

- Python 3.13+
- NVIDIA GPU with 8GB+ VRAM (for local LLM inference, 4-bit quantized)

---

## Installation

```bash
git clone <repo-url>
cd local-leads-automation

python -m venv .venv
source .venv/bin/activate       # Linux/macOS
# .venv\Scripts\activate        # Windows

pip install -r requirements.txt

# Install PyTorch with CUDA support (adjust cu128 to your CUDA version)
pip install torch --index-url https://download.pytorch.org/whl/cu128

# Install Playwright browser
playwright install chromium
```

---

## Usage

```bash
python main.py
```

Edit `config.py` to set the target profession, city, and sender identity before running.

Results are saved to `data/leads.csv` (single file, all phases merged).

---

## Output fields

| Field | Source | Description |
|---|---|---|
| `name` | Maps | Business name |
| `website` | Maps | Website URL |
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
| `subject` | AI | Generated email subject line |
| `body` | AI | Generated email body (ready to send) |

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
main.py                      # Pipeline orchestrator
scraper/
  maps_scraper.py            # Scrapes businesses from Google Maps
  web_analyzer.py            # CMS detection, contacts, SEO scoring
ai/
  message_generator.py       # Generates outreach emails via local LLM
data/
  leads.csv                  # Full pipeline output
config.py                    # Search parameters, LLM model, sender identity
.env                         # Secrets (do not commit)
```

---

## Development Phases

- [x] **Phase 0** — Basic prototype: extracts name and website from Google Maps
- [x] **Phase 1** — Robust Maps scraper: unlimited scroll, address/phone/location, rate limiting with jitter, exponential backoff
- [x] **Phase 2** — Web analyzer: CMS detection, email extraction, social networks, SEO scoring (14 checks)
- [x] **Phase 3** — Message generation: local LLM (Qwen2.5-7B, 4-bit), contactable lead filtering, single CSV output
- [ ] **Phase 4** — CLI: `--phase`, `--city`, `--profession`, `--max` flags

---

## Notes

- Delays are set to 3–6 seconds between scraping actions to simulate human behavior.
- Message sending is **semi-manual** — AI-generated drafts are reviewed before sending, in compliance with GDPR.
- The LLM runs locally (no API key required). First run downloads ~4GB of model weights.
- Leads without any reachable contact channel (email, phone, or social network) are skipped in Phase 3.
