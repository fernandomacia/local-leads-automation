# CLAUDE.md — local-leads-automation

## Project Context

Lead generation tool for a web developer selling SEO repair services and custom WordPress themes. The goal is to find local businesses with poor websites and reach out with personalized messages.

**User profile:** Experienced web developer, comfortable with Python and automation. Not an expert in scraping or AI, but learns quickly.

## Target Pipeline

```
Google Maps → extract businesses
           → detect WordPress / analyze web quality
           → save email / instagram / website
           → generate personalized AI message
           → assisted manual outreach
```

## Module Architecture

```
worker.py                    # Daemon: polls SegurSEO-API job queues, drives scraping/analysis
scraper/
  maps_scraper.py            # Extracts lead, website, phone, address from Google Maps
                              # scrape() for ad-hoc use; scrape_incrementally() for worker.py
  web_analyzer.py            # CMS detection, email/socials extraction, SEO scoring, is_contactable()
  net_guard.py               # SSRF guard shared by the HTTP fetches and the browser fallback
  compliance/                # Legal audit package — detect_compliance() returns issues + details
    vocabulary.py            # Multilingual lexicon + URL slugs (data only)
    matching.py              # Normalization, word-boundary matching, slug/path matching, site language
    cmp.py                   # Consent platforms, hand-rolled banners, reject control
    trackers.py              # Signals that create a consent obligation
    legal_pages.py           # Document resolution + verification (GET, probing, RequestBudget)
    page_content.py          # Mandatory content: NIF/CIF, LSSI art. 10, RGPD art. 13
    rendering.py             # Playwright fallback for footers built client-side
    forms.py                 # Form consent (missing, link-only, pre-ticked)
ai/
  message_generator.py       # Generates personalized outreach emails via OpenRouter API
api/
  client.py                  # SegurSEO-API job-queue client: claim/report/complete/fail endpoints
config.py                    # Constants and configuration (scraper, OpenRouter, sender identity, API worker)
```

## Tech Stack

- **Python 3.13** with venv
- **Playwright** — Google Maps scraping (real browser to avoid blocks)
- **requests + BeautifulSoup** — lead website analysis
- **requests + OpenRouter API** — message generation via DeepSeek (`deepseek/deepseek-chat`)
- **python-dotenv** — environment variable management (`.env`)

---

## Working Standards

### Language
- Claude communicates with the user **in Spanish**
- `CLAUDE.md` and `README.md` are written **in English**
- All internal code documentation (docstrings, comments) is written **in English**

### Code Comments & Documentation
- **Language:** English
- **Style:** Concise, clear, professional, and modern. Avoid verbosity and redundancy. Document the *why*, not the *what*.
- **Standard:** Google Style Docstrings for Python
- Only document functions with non-trivial logic — trivial ones need no docstring
- Inline comments only when the *why* is non-obvious; never explain the *what*

```python
def analyze_website(url: str) -> dict:
    """Fetch and analyze a website for WordPress signals and SEO issues.

    Args:
        url: The website URL to analyze.

    Returns:
        A dict with keys: is_wordpress, email, instagram, seo_score.
    """
```

### Code
- Efficient, modern, and professional — leverage Python 3.10+ features
- Not verbose: prefer concise expressions over long blocks
- No unnecessary defensive error handling — errors should be visible for debugging
- No premature abstractions — if something is used once, a class is not needed
- Each module does one thing; `worker.py` orchestrates

---

## Development Practices

### Before Starting Implementation
- **Understand the context** — Read existing code structure
- **Plan the approach** — Discuss architecture before coding
- **Follow patterns** — Use established conventions from the project
- **Security first** — Always sanitize/escape user input

### When Creating Features
- ✅ Create separate, modular files
- ✅ Use meaningful class/function names
- ✅ Add error handling (try-catch, validation)
- ✅ Document with concise comments
- ✅ Make code reusable and maintainable

### Checklist for Code Review
- [ ] Code is clean and professional
- [ ] English comments are concise and elegant
- [ ] Error handling is in place
- [ ] Files are properly organized following the feature structure
- [ ] Functions have a single responsibility

### Work Sessions
- Build in phases: make it work first, then polish
- Current phase: **Phase 7 complete — compliance package with real page verification**
- At the end of each phase, update this file with lessons learned

### Scraping
- Use `time.sleep()` with realistic values (3–6s between actions), never less
- Pass `max_results=N` to `scrape()` or `scrape_incrementally()` to limit results during testing
- `scrape_incrementally()` opens each listing in its own tab (`context.new_page()`)
  but shares one `browser.new_context()` with the results-list tab — separate
  contexts (e.g. `browser.new_page()`) don't share consent cookies, so every
  detail tab would hang on Google's consent screen and time out
- `MAX_IDLE_SCROLLS` bounds `scrape_incrementally()`: it stops after that many
  consecutive scroll waves with no new hrefs at all (end of feed), so an
  exhausted search doesn't scroll forever. Known (skipped) leads reset the
  counter — only a truly empty scroll wave counts as idle.

### Reporting to the API

- **A reading is sent where it is taken.** `maps_issues` goes with the ingest batch,
  while the Maps card is in front of the scraper, and no later step may write it. Reported
  with the analysis instead — which is what this worker did until 2.4.0 — the one finding
  it exists for was the one it lost: an agent supplies a website through the panel for a
  business whose card had none, the worker then reads a job *with* a website, and "no
  website on the listing" silently becomes "the listing was complete". Compute it from the
  scraped card (`maps_card_issues`), never from the job the API hands back.
- **The API drops a key it does not accept rather than refusing it.** That is deliberate
  on its side — a 422 on the analysis report makes this worker answer `failed: true` and
  abandon the lead — but it means a misplaced field looks like it worked and writes
  nothing. Before adding a field to either payload, check which endpoint accepts it.
- **`{}` and `None` are different answers.** An empty dict is a finding: the card was
  complete, the site was audited and clean. `None` means no worker has reported yet. Never
  omit a key to mean "nothing found", and never send `{}` for something that was not
  checked.
- **The version is the contract.** `APP_VERSION` is what the API's Compatibility table
  keys its worker requirements to, so a change to what this worker sends or reads bumps
  it in the same commit.

### Compliance auditing
- Bias is asymmetric on purpose: a false positive is read out loud to a business
  owner as "you are breaking the law". Anything ambiguous resolves to compliant,
  and a check that cannot see enough reports `unknown`, never a finding.
- Never match legal terms with `in`: `"legal" in "/asesoria-legal"` is true and
  wrong. Use `matching.contains_term` (word boundaries) and `matches_slug`
  (path segments), both on normalized text.
- The lexicon covers 28 languages because a Spanish business with an English or
  German site would otherwise be reported as having no legal texts at all.
- Verification costs requests against sites that never asked to be scanned:
  `MAX_COMPLIANCE_REQUESTS` (12/lead) is a hard cap, split evenly between the
  documents that need probing so the last one is still checked.
- The Playwright fallback only fires when *no* legal link is found in any
  language, and its output is discarded if the DOM comes back under 500 chars —
  an empty render would turn every check into a finding.
- Every URL built from an href on the analyzed page goes through
  `net_guard.host_ok` before it is requested, the browser fallback included: a
  link on an untrusted page is an untrusted URL. Links to a host known to be
  internal are dropped at resolution, so the address never reaches the panel
  either. A host that merely fails to resolve is *not* internal — dropping those
  would turn a transient DNS failure into a "document never published" finding.
- `run_analysis_job` returns whether the audit needed a browser, and the worker
  reports the tally per batch. That number, not the request count, is what sizes
  the host: a lead that renders costs a Chromium process.
- Adding an issue key means declaring it in the API's `ComplianceIssue` enum,
  which serves the panel's filter dropdown through `GET /compliance-issues`;
  labels themselves need no change on either side. Statuses are stricter: the
  API validates `compliance_details.*.status` against `ComplianceStatus`, so one
  invented here is a 422 and a lead reported as failed.
- After changing either vocabulary, regenerate the contract dump and commit it on
  the API side, where a test compares it against the enums:
  `python -m scraper.compliance --dump-keys > compliance-keys.json`. Nothing
  prevents divergence at runtime; this only makes it fail in CI instead of in
  production.
- `compliance_details` is sent on `PATCH /leads/{id}/analysis` alongside
  `compliance_issues` — the API must accept the field or it will 422.
- The site's declared language travels as `compliance_language`, apart from the
  per-document `language`, which is whichever lexicon matched that link. The API
  validates it as `^[a-z]{2,8}$`, so `detect_language` discards anything that is
  not a language tag: a theme's unrendered `{{ site.lang }}` would otherwise cost
  the lead its whole analysis, since the worker answers an HTTP error with
  `failed: true`.

### Outreach
- Final sending is semi-manual (not mass automated) to comply with GDPR
- AI-generated messages are drafts to review before sending

---

## Commit Conventions

Follow **Conventional Commits** format for all commit messages, always ready to copy-paste:

```
type(scope): Short description

- Bullet point 1 describing the main change
- Bullet point 2 (if needed)
```

Also list the files to include in the commit:

```
Files to include:

scraper/maps_scraper.py
```

### Types
| Type | Use |
|------|-----|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation updates |
| `style` | Code formatting, missing semicolons, etc. |
| `refactor` | Code restructuring without feature changes |
| `perf` | Performance improvements |
| `test` | Tests additions/updates |
| `chore` | Build config, dependencies, etc. |

### Commit Workflow

When asked for commits, **NEVER execute commits automatically**. Instead:

1. **Review Changes** — List all modified and new files, identify their scopes
2. **Analyze Strategy** — Determine if changes form a cohesive feature or separate concerns; order commits by least coupling first
3. **Process one commit at a time:**
   - Present the commit (message + file list, ready to copy-paste)
   - Wait for confirmation before moving to the next commit

---

## Project Phases

- [x] Phase 0: Basic Maps scraper prototype
- [x] Phase 1: Robust Maps scraper with scroll, more fields, rate limiting
- [x] Phase 2: Web analyzer (CMS detection, email/socials extraction, SEO scoring)
- [x] Phase 3: Message generation with local LLM (Qwen2.5-7B, 4-bit), full pipeline
- [x] Phase 4.1: Switched message generation from local LLM to OpenRouter API (DeepSeek)
- [x] Phase 4: CLI (`--profession`, `--city`, `--max`, `--no-headless`) + Streamlit dashboard + API client + JSON output
- [x] Phase 5: SegurSEO-API job-queue integration — `worker.py` daemon polling two queues (Maps discovery, lead analysis); `api/client.py` rewritten around the 6-endpoint contract; `scrape_incrementally()` added to `maps_scraper.py` for batched, domain-deduplicated discovery.
- [x] Phase 6: Removed `main.py`, `app.py`, `data/` and Streamlit/pandas dependencies — pipeline is now driven exclusively by the SegurSEO-API job queue via Angular + Laravel.
- [x] Phase 7: `scraper/cookie_detection.py` replaced by the `scraper/compliance/` package — 28-language lexicon, word-boundary and slug matching, reject-control detection, extended trackers, form-consent findings (7a); then real page verification with a request budget, mandatory-content validation, and a Chromium fallback for JS-built footers (7b).
