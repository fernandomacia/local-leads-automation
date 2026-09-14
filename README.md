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
| `seo_issues` | Web | Issue key → Spanish label (see below) |
| `compliance_issues` | Web | Legal compliance issue key → Spanish label (see below) |
| `compliance_details` | Web | Evidence per legal document: status, URL found, language, method |
| `maps_issues` | Maps | Listing gap key → Spanish label (see below) |
| `email_subject` | AI | Generated email subject line |
| `email_body` | AI | Generated email body (ready to send) |
| `phone_script` | AI | Generated phone script for the sales call |

### SEO issues detected

| Issue | Description |
|---|---|
| `no_https` | Site not served over HTTPS |
| `invalid_ssl` | SSL certificate expired or invalid (site loaded only with verification disabled) |
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

### Legal compliance issues detected

Reported separately from `seo_issues` and excluded from `seo_score`: these are
unmet legal obligations, not quality signals. Detection is deliberately
conservative — a false positive is told to a business owner as "you are breaking
the law", so anything ambiguous resolves to compliant.

| Issue | Description |
|---|---|
| `no_cookie_banner` | Loads trackers with no consent banner. Sites using only technical cookies are exempt and never reported |
| `cookie_banner_without_reject` | The banner offers "Accept" but no reject control in the first layer (AEPD cookie guide, 2023). Only reported when both controls are visible in the HTML |
| `legal_notice_broken` / `privacy_policy_broken` / `cookie_policy_broken` | The document is linked but the link 404s |
| `legal_notice_unlinked` / `privacy_policy_unlinked` / `cookie_policy_unlinked` | The page exists at a known path but nothing links to it — the law requires direct, permanent access |
| `legal_notice_incomplete` / `privacy_policy_incomplete` / `cookie_policy_incomplete` | The page exists but omits mandatory content. The label names exactly what is missing |
| `no_cookie_policy` | No link to a cookie policy page. Gated on trackers, like the banner; a banner's own "Accept" anchor does not count |
| `no_legal_notice` | No legal notice linked (LSSI art. 10) |
| `no_privacy_policy` | No privacy policy linked (GDPR art. 13) |
| `form_without_consent` | A contact form collects personal data with no consent checkbox and no privacy link |
| `form_consent_link_only` | The form links the privacy policy but never asks for consent (GDPR art. 7) |
| `form_consent_prechecked` | The consent checkbox arrives pre-ticked — void consent (CJEU C-673/17, *Planet49*) |

Legal documents are matched in **28 languages**, on both the visible link text
and the URL slug, because a Spanish business with an English or German site
would otherwise be reported as having no legal texts at all. Matching is
word-bounded and slugs are compared segment by segment, so `/asesoria-legal` is
not mistaken for a legal notice.

Each document is verified, not just looked for: the link is followed with a GET
(never HEAD — a large share of WordPress installs answer 405), unlinked
documents are probed at their conventional paths, and the page that comes back
is checked for the content the law requires — NIF/CIF, registered address and
contact for the legal notice (LSSI art. 10); controller, purpose, legal basis,
rights, retention and supervisory authority for the privacy policy (GDPR
art. 13); cookie categories and durations for the cookie policy. Regulated
professions are additionally required to state their bar association and
membership number (LSSI art. 10.1.c), decided from the search term the lead came
from.

Every URL is checked against the SSRF guard before it is requested — including
the ones built from hrefs on the analyzed page, which is untrusted input — and
probing a document stops after two consecutive network failures, since a host
that stopped answering will not answer the remaining paths either.

Verification is capped at `MAX_COMPLIANCE_REQUESTS` (12) per lead, shared evenly
between the documents that need probing. When the budget runs out the remaining
documents are reported as `unknown` and produce **no finding**: not knowing is
not evidence of breaching. Sites whose footer is built by JavaScript — the main
source of false "no legal notice" findings — are re-fetched once with Chromium
before any of this (`COMPLIANCE_RENDER_FALLBACK`), and the rendered DOM then
feeds every check, the cookie banner included.

`compliance_details` carries the evidence behind each document finding:

```json
{
  "legal_notice":   {"status": "incomplete", "found_url": "https://x.es/aviso-legal",
                     "method": "link", "language": "es", "validated": true,
                     "checked_urls": ["https://x.es/aviso-legal"], "missing_items": ["tax_id"]},
  "privacy_policy": {"status": "unlinked", "found_url": "https://x.es/privacidad",
                     "method": "probe", "checked_urls": ["https://x.es/politica-de-privacidad",
                                                         "https://x.es/privacidad"]},
  "cookie_policy":  {"status": "not_applicable", "reason": "no_trackers"}
}
```

Statuses: `ok`, `incomplete`, `broken_link`, `unlinked`, `missing`,
`not_applicable`, `unknown`.

### Google Maps listing issues detected

Derived from the Maps listing itself, not from fetching the website, so they are
known even when the site is unreachable. An empty `{}` means the listing is
complete — unlike NULL, which means the worker never processed the lead.

| Issue | Description |
|---|---|
| `no_website` | The Maps listing has no website |
| `no_phone` | The Maps listing has no phone number |
| `no_address` | The Maps listing has no street address |

---

## Project Structure

```
worker.py                    # Daemon: polls SegurSEO-API job queues, drives scraping/analysis
scraper/
  maps_scraper.py            # Scrapes businesses from Google Maps
  web_analyzer.py            # CMS detection, contacts, SEO scoring
  net_guard.py               # SSRF guard shared by every fetch and the browser
  compliance/                # Legal compliance audit (LSSI, GDPR, cookies)
    __init__.py              # detect_compliance(): orchestration, issue labels
    vocabulary.py            # Multilingual lexicon and URL slugs
    matching.py              # Normalization, word-boundary and slug matching
    cmp.py                   # Consent platforms, banners, reject control
    trackers.py              # Resources that create a consent obligation
    legal_pages.py           # Document resolution, verification, request budget
    page_content.py          # Mandatory content (LSSI art. 10, GDPR art. 13)
    rendering.py             # Chromium fallback for JS-built footers
    forms.py                 # Form consent
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
