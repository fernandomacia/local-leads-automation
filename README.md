# local-leads-automation

Lead generation tool for web developers. Extracts local businesses from Google Maps, analyzes their website quality, and generates personalized outreach messages using AI.

**Use case:** Find businesses with poor websites (slow, no SEO, outdated WordPress) and contact them offering improvement services.

---

## Pipeline

```
Google Maps
  → extract businesses (name, website, phone)
  → analyze website (WordPress, email, Instagram, SEO score)
  → save to leads.csv
  → generate personalized message with Claude
  → assisted manual outreach
```

---

## Requirements

- Python 3.13+
- Anthropic API key (for message generation)

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
# Run the Google Maps scraper (Phase 0/1)
python main.py
```

Results are saved to `data/leads.csv`.

---

## Project Structure

```
main.py                      # Pipeline orchestrator
scraper/
  maps_scraper.py            # Extracts businesses from Google Maps
  web_analyzer.py            # Detects WordPress, extracts contacts, SEO score
ai/
  message_generator.py       # Generates personalized messages with Claude
data/
  leads.csv                  # Main output
config.py                    # Queries, limits, general configuration
.env                         # API keys (do not commit)
```

---

## Development Phases

- [x] **Phase 0** — Basic prototype: extracts name and website from Google Maps
- [ ] **Phase 1** — Robust Maps scraper: scroll, more fields, rate limiting
- [ ] **Phase 2** — Web analyzer: WordPress detection, email/Instagram, SEO score
- [ ] **Phase 3** — Message generation with Claude API
- [ ] **Phase 4** — CLI to run the full pipeline

---

## Notes

- The scraper uses Playwright with `headless=False` by default to facilitate debugging.
- Sleeps are set to 3–6 seconds between actions to simulate human behavior.
- Message sending is **semi-manual** — AI-generated drafts are reviewed before sending, in compliance with GDPR.
- Do not run more than 20–30 results during testing.
