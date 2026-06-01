# local-leads-automation

Lead generation tool for web developers. Extracts local businesses from Google Maps, analyzes their website quality, and generates personalized outreach messages using AI.

**Use case:** Find businesses with poor websites (slow, no SEO, outdated WordPress) and contact them offering improvement services.

---

## Pipeline

```
Google Maps
  → extract businesses (name, phone, address, city, province)
  → analyze website (CMS detection, email, social networks, SEO score)
  → save to leads.csv
  → generate personalized message with Claude  [Phase 3]
  → assisted manual outreach                   [Phase 4]
```

---

## Requirements

- Python 3.13+
- Anthropic API key (for message generation, Phase 3)

---

## Installation

```bash
# Clone the repository
git clone <repo-url>
cd local-leads-automation

# Create virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate       # Linux/macOS
# .venv\Scripts\activate        # Windows

pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

### Environment variables

Create a `.env` file at the project root:

```env
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Usage

```bash
python main.py
```

Edit `config.py` to set the target profession and city before running.

Results are saved to:
- `data/leads_raw.csv` — raw Maps data (name, phone, address, website)
- `data/leads.csv` — enriched leads (Maps + web analysis)

---

## Output fields

| Field | Source | Description |
|---|---|---|
| `name` | Maps | Business name |
| `website` | Maps | Website URL |
| `phone` | Maps | Phone number |
| `address` | Maps | Street address |
| `zip_code` | Maps | Postal code |
| `city` | Maps | City (from address, or `** city **` if inferred from search) |
| `province` | Maps | Province |
| `cms` | Web | Detected CMS: `wordpress`, `wix`, `squarespace`, `shopify`, `unknown`, `unreachable` |
| `email` | Web | Contact email |
| `instagram` … `tiktok` | Web | Social media URLs |
| `seo_score` | Web | 0–100 (lower = more issues) |
| `seo_issues` | Web | Pipe-separated list of detected issues |

---

## Project Structure

```
main.py                      # Pipeline orchestrator
scraper/
  maps_scraper.py            # Extracts businesses from Google Maps
  web_analyzer.py            # CMS detection, contacts, SEO scoring
ai/
  message_generator.py       # Generates personalized messages with Claude
data/
  leads_raw.csv              # Phase 1 output (Maps data)
  leads.csv                  # Phase 2 output (enriched)
config.py                    # Search parameters and scraping configuration
.env                         # API keys (do not commit)
```

---

## Development Phases

- [x] **Phase 0** — Basic prototype: extracts name and website from Google Maps
- [x] **Phase 1** — Robust Maps scraper: unlimited scroll, address/phone/location fields, rate limiting with jitter, exponential backoff
- [x] **Phase 2** — Web analyzer: CMS detection, email extraction, social networks, SEO scoring
- [ ] **Phase 3** — Message generation with Claude API
- [ ] **Phase 4** — CLI to run the full pipeline

---

## Notes

- The scraper runs headless by default (`HEADLESS = True` in `config.py`). Set to `False` to watch the browser during debugging.
- Delays are set to 3–6 seconds between actions to simulate human behavior.
- Message sending is **semi-manual** — AI-generated drafts are reviewed before sending, in compliance with GDPR.
