# local-leads-automation

Lead generation tool for web developers. Extracts local businesses from Google Maps, analyzes their website quality, and generates a personalized phone argumentario using an LLM via OpenRouter.

**Use case:** Find businesses with poor websites (insecure, no SEO, outdated) and contact them offering improvement services.

---

## Pipeline

```
SegurSEO Platform (Angular) → SegurSEO-API (Laravel) queues the work
  → claim a search and scrape Google Maps (name, website, phone, address, city, province)
  → report each batch, with what the Maps card was missing
  → claim a lead and analyse its site (CMS, email, socials, SEO score, legal compliance)
  → generate the phone argumentario (OpenRouter LLM)
  → report the analysis back
  → assisted manual outreach, from the panel
```

**Which leads are worth analysing is the API's decision, not this worker's.** It analyses
whatever `GET /leads/next` hands it. A lead with no website and no way to reach it is
failed at ingest on the API side, where "a way to reach it" means a phone number or an
email — a social profile is not one. This README used to describe the filter as the
worker's and as "email or any social network", and it was neither.

---

## Requirements

- Python 3.13+
- OpenRouter API key (for message generation)
- SegurSEO-API, with a worker token issued for that environment. Which versions go
  together is recorded in the Compatibility table in `SegurSEO-API/README.md`, keyed to
  `APP_VERSION`, and it names no version below which this worker refuses to run: every
  pair it lists for the worker turns a feature off rather than stopping the run
- **13.3.0 is the pair for this worker, and a soft one.** Against 13.2.x nothing fails and
  no host is taken for a domain — the empty `website` deduplicates the lead by name on
  either version, and `social_as_website` is stored, since `maps_issues` has carried its
  keys through unvalidated since 3.0.0. What goes dark is the social profile:
  `social_networks` has no rule at ingest before 13.3.0, so it is dropped, and for a
  business with no site of its own the analysis has no page to read the profile from
  either — the only address that business has reaches nobody

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

On start the worker asks which API to use: `.env` (local) or `.env.production`.
Set `WORKER_ENV_FILE` to skip the question, which an unattended run must do.
`.env.production` must be a full copy of `.env`, not just the changed lines: a
terminal that already exported the local values keeps any variable it lacks.

### Stopping it

**Ctrl+C**, in the terminal it runs in. The job in hand is handed back to the queue —
the search becomes claimable again at once and the lead keeps the attempt its claim
charged — and what the run got through is still reported.

`kill` is not the same thing. SIGTERM, which is also what `systemctl stop` sends, kills
the process without that release, so the job it was holding stays claimed until the
API's `scraper:recover-stale-jobs` clears it — up to `SCRAPER_CLAIM_TIMEOUT_MINUTES`
(30 by default) later.

**Each search is finished before the next one starts.** Analyses are claimed ahead of
searches, so a search is scraped, analysed to the end and closed off — the lifecycle the
API itself describes — rather than every search being scraped first and their leads
analysed as one stream. The cost is that a queued search is not discovered until the one
before it is done.

Each search prints three lines, under a rule of its own. While its analyses run the
third line is a bar rewritten in place with the lead in hand, replaced by the count when
the queue empties:

```
──────────────────────────────────────────────────────────────────────────
[>] Search: abogado en elche
[+] Search done: 20 leads
[>] Analysing [███████░░░░░] 12/20 — Pellicer & Heredia Abogados…
[+] Analysis done: 20 analysed
──────────────────────────────────────────────────────────────────────────
[>] Search: abogado en aspe
[+] Search done: 0 leads
```

If OpenRouter rate-limits the run, the lead in hand goes back to the queue and the
worker says so and waits a minute — `[!] Rate limited — pausing for a minute` — rather
than reporting a lead as failed over a limit that lifts on its own.

A search that found nothing stops at two lines: there is nothing to analyse. A failed
lead is named beside the count — `18 analysed, 2 failed` — and Ctrl+C still prints the
count for what the run got through.

Leads the API hands back from an earlier run, which is what a restart mid-analysis
produces, have no Search line of this round to close: each is reported on its own line,
titled after the search that found it (`[+] Analysis done — abogado en elche: 12
analysed`), since nothing above it says which search that was.

Outside a terminal the bar is dropped and the rest is printed as written: journald does
not honour the carriage return the counter depends on.

**Ctrl+C hands the job in hand back to the queue** (`release_search_job` /
`release_analysis_job`), so the search is claimable again immediately and the lead keeps
the attempt its claim charged. Without that the API only clears the claim through
`scraper:recover-stale-jobs`, up to `SCRAPER_CLAIM_TIMEOUT_MINUTES` later — so stopping
the worker to change something meant waiting half an hour to run the same search again.
Requires API ≥ 3.1.0.

---

## Output fields

| Field | Source | Description |
|---|---|---|
| `lead` | Maps | Business name |
| `website` | Maps | The site itself, reduced from the card's link: query, fragment and path dropped, except on hosts that sell paths rather than subdomains |
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
| `compliance_language` | Web | Language the site declares (`html[lang]`, `og:locale`, `hreflang`) |
| `maps_issues` | Maps | Listing gap key → Spanish label (see below) |
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

The API validates the status of every document against its own enum, so the two
vocabularies have to agree. They are written in different languages and cannot
share a definition, so the worker dumps its own:

```bash
python -m scraper.compliance --dump-keys > compliance-keys.json
```

Commit that file on the API side, where a test compares it against
`ComplianceIssue` and `ComplianceStatus`. It does not prevent divergence — it
makes it fail in CI rather than as a 422 with every lead reported as failed.

The `language` inside each entry is the lexicon that matched that document's link,
which on a bilingual site is not the language of the site. The site's own
declaration travels apart, as `compliance_language`: it is what the panel labels
the lead with and what the agent calls in.

### Google Maps listing issues detected

Derived from the Maps listing itself, not from fetching the website, so they are
known even when the site is unreachable. An empty `{}` means the listing is
complete — unlike NULL, which means the worker never processed the lead.

**Reported with the ingest batch, never with the analysis.** The card is only in front of
the scraper once, and an agent may add a website through the panel before the analysis
runs — at which point "no website on the listing" is no longer derivable from anything the
analysis can see. The API accepts the field on the ingest endpoint alone and drops it on the
other.

| Issue | Description |
|---|---|
| `no_website` | The Maps listing has no website |
| `no_phone` | The Maps listing has no phone number |
| `no_address` | The Maps listing has no street address |
| `social_as_website` | The listing's website button points at a social profile instead of at a site of the business's own. Its label names the network — "La ficha de Google Maps enlaza a LinkedIn en lugar de a su web" — and it **replaces `no_website`, never joins it**: the button is there and the destination is wrong, so the business may have a website all the same |

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
  message_generator.py       # Generates the phone argumentario via OpenRouter
api/
  client.py                  # SegurSEO-API job-queue client (used by worker.py)
config.py                    # Scraper, OpenRouter, sender, and API worker configuration
env_select.py                # Asks which API to use, before config.py loads anything
.env                         # Secrets (do not commit)
```

---

## Compatibility

This worker is one of four components around the API, which is where the compatible
combinations are recorded — the Compatibility table in `SegurSEO-API/README.md`, keyed to
`APP_VERSION`. Two things it says that matter here:

- **Deploy the API first.** Up to 2.8.x every version of this worker needed an API at least
  as new as itself; 2.9.0 is the first that degrades instead of requiring, and runs against
  13.2.x with one feature dark. Either way those API releases accepted the older input too,
  so the gap stays short and one-sided in that direction only.
- **`APP_VERSION` is the contract**, not a label: it is what that table keys its
  requirements to, so a change to what this worker sends or reads bumps it.

Version history lives in `CHANGELOG.md` and the git tags (`v<APP_VERSION>`) and, in more
detail than belongs here, in the project phases in `CLAUDE.md`.

---

## Notes

- Delays are set to 3–6 seconds between scraping actions to simulate human behavior.
- Message sending is **semi-manual** — AI-generated drafts are reviewed before sending, in compliance with GDPR.
- Message generation calls the OpenRouter API (`OPENROUTER_API_KEY` required in `.env`).
- Leads with no website and no phone or email never reach this worker: the API fails them
  at ingest, so no LLM call is spent on a pitch that could not be delivered.
