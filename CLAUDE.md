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
                              # scrape_incrementally(): one card at a time, for worker.py
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
  message_generator.py       # Generates the phone argumentario via OpenRouter API
api/
  client.py                  # SegurSEO-API job-queue client: every call through one _request()
audit/
  migrations/                # SQL for the shared audit store (Neon Postgres)
  outbox.py                  # Local SQLite buffer of audit fragments; never raises on put()
config.py                    # Constants and configuration (scraper, OpenRouter, sender identity, API worker)
env_select.py                # Asks which env file to load, before config.py reads one
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
- Pass `max_results=N` to `scrape_incrementally()` to limit results during testing
- `scrape_incrementally()` opens each listing in its own tab (`context.new_page()`)
  but shares one `browser.new_context()` with the results-list tab — separate
  contexts (e.g. `browser.new_page()`) don't share consent cookies, so every
  detail tab would hang on Google's consent screen and time out
- **"Is this business already ours?" is asked per card, not per batch.**
  `scrape_incrementally()` takes an `is_known` predicate rather than a set of domains,
  because a set can only hold what the caller already knew: the batched version answered
  after a lote had been reported, so the first `BATCH_SIZE` businesses went unchecked and a
  `max_results` sample — which is set at or below that by definition — could spend its whole
  cap on businesses the system had all along. What it saves is a slot of `max_results`, never
  the detail tab: the card has to be opened to learn the website a domain comes from
- A positive answer is cached and a negative one is not, so a second card sharing one
  website is skipped once the first has been reported. A failed check answers "not known":
  the API deduplicates at ingest anyway, and losing a search over an optimisation would not
- **The card's link is not the site.** What the owner typed into Google Business
  Profile is routinely a tracked landing page — one real lead arrived as
  `/en/hondon-de-las-nieves-lawyers?utm_source=Google&utm_medium=My%20Business`, and
  stored verbatim that is the page the analyzer audits: a law firm in Alicante scored on
  an English satellite page and reported as an English-language site. `canonical_website()`
  reduces it at extraction, so what is stored, audited and shown are the same thing.
  The exception is `_PATH_HOSTED_SITES`: on `wixsite.com` or `sites.google.com` the path
  *is* the customer's site, and trimming it would audit the builder's own homepage
- **A social profile in the website button is not a website.** Nobelia Textil's card
  linked LinkedIn; `canonical_website()` trimmed it to `es.linkedin.com`, it was stored
  as the website, and `maps_card_issues()` called the card complete — so the argumentario
  never said that Google sends the business's customers to LinkedIn instead of to its
  site, which is a first-rate line on the call. `_extract_business()` now routes it to
  `social_networks` with its path intact, leaves `website` empty, and reports
  `social_as_website`. That key and `no_website` are produced by one branch each, so a
  card can never carry both: the business may well have a site, and Nobelia did
- **The API keeps its own list of these hosts in `Domain::isShared()`, and the two must
  agree.** It needs them to stop deduplicating every business on a shared host into one,
  and this worker needs them to stop recording one as a website. `wixsite.com` is
  deliberately only on this side: there the *subdomain* names the business, so it is a
  perfectly good key for the API and nulling it would merge every free Wix site in Spain
- `MAX_IDLE_SCROLLS` bounds `scrape_incrementally()`: it stops after that many
  consecutive scroll waves with no new hrefs at all (end of feed), so an
  exhausted search doesn't scroll forever. Known (skipped) leads reset the
  counter — only a truly empty scroll wave counts as idle.

### Job order

- **A search is finished before the next one is claimed.** `main()` claims analyses
  first, so a search is scraped and then analysed to the end, which is the lifecycle the
  API already describes: pending, scraping, analyzing, completed. Claiming searches first
  left two of them in `analyzing` at once and their leads analysed as one
  indistinguishable stream.
- **What it costs is discovery latency.** Queue three searches and the third is not
  scraped until the first two are analysed, so its leads do not exist in the panel until
  then — where claiming searches first had every lead discovered within minutes and
  analysed later. The total work is the same; only the order changes.
- **Analyses left over from an earlier run are claimed before the first search**, which
  is what the operator sees after restarting the worker mid-run. Those leads name their
  own search in the payload they arrive with, so their line is titled after it rather
  than being lumped together.

### Terminal output

- **A 429 is not a failed lead.** OpenRouter's rate limit reaches the worker only once
  the generator's own four retries have given up, and answering it the way every other
  HTTP error is answered — `failed: True` — retired leads for good over a limit that had
  lifted a minute later. It releases the lead and pauses the loop instead; 402 is handled
  the same way, plus the payment warning to the search.
- **Log records clear the counter before they print.** Logging writes to stderr and the
  counter to stdout, so on a terminal a warning arrived on the same row as the lead being
  analysed, starting at whatever column the counter had padded to.
  `_ProgressAwareHandler` is installed by `basicConfig` for that alone.
- **A rule heads each block of work**, printed whether or not this is a terminal: a run
  piped to a file is read the same way, and the rule is what tells one search's lines
  from the next's. A run of leads left queued by an earlier run gets one too — nothing
  else heads it, since no search of this round found them.
- **The startup lines answer "what is this talking to" and "how do I stop it".** The
  Ctrl+C hint is printed only on a terminal: `systemctl stop` arrives as SIGTERM, which
  kills the process without the release Ctrl+C performs, so there the advice would be
  wrong and unfollowable. Nothing handles SIGTERM today — a job held when it arrives
  waits for the API's stale-claim recovery.
- **Three lines per search, not one per lead.** Search, Search done, Analysis done. The
  third is the counter rewritten in place while the run goes, replaced by
  `_analysis_done_lines()` when the queue empties, so fifty leads leave three lines
  behind. `run_analysis_job` answers whether the lead was analysed and the caller counts
  it, rather than printing anything itself.
- **The counts reset when a search is claimed**, whether or not anything was analysed: a
  search that found no leads produces no analyses, and its row used to linger and be
  counted under the next search's line.
- **A run covering more than one search names each.** That is what a restart
  mid-analysis produces, and those leads have no Search line of this round above them to
  say which search the count belongs to.
- **Only what the operator acts on.** The lines answer "which search, how many leads,
  how many analysed". Average score, finding counts and the browser tally were removed:
  the first two are in the panel per lead, and the third measured a cost of running the
  worker rather than anything about the leads.
- **Every in-place write pads to `_LINE_WIDTH`.** `\r` only moves the cursor, so a lead
  with a short name following one with a long name left the tail of the longer on screen.
- **Nothing in the progress line may raise.** It is printed from inside the analysis, so
  an exception there is caught by that handler and reported to the API as a lead that
  failed to analyse — which is how a missing denominator or a terminal that cannot encode
  a block character would cost a lead. `_analysis_progress` falls back to a bare counter
  when there is no usable total, and the bar glyphs fall back to ASCII.
- **The analysis queue is global, so the total is a hint.** It comes from the search just
  scraped; leads left by an earlier run have none, and a longer queue runs past it. Both
  drop the bar rather than draw a wrong one.

### Configuration

- **The env file is chosen before anything imports `config.py`**, which reads it at import
  time. `worker.py` asks under `if __name__ == "__main__"` so importing it — as the tests
  do — never prompts, and `WORKER_ENV_FILE` skips the question for an unattended run. A
  relative name resolves against the repo; an absolute one is taken as given, so a service
  unit can keep its file outside the checkout.
- **`.env.production` is a full copy, not a diff of `.env`.** Only one file is loaded, and
  `override=True` means a variable it lacks keeps whatever the shell already exported —
  or nothing.
- **A credential that is only needed later is still checked at startup.** OpenRouter
  answers a missing key with 401, and 401 is neither the 402 nor the 429 the worker waits
  out: every lead of the search is reported `failed: True` and retired for good, one at
  a time, until the search ends with nothing analysed. `config.py` refuses to import
  instead, naming the file that is short a line. Anything else the pipeline cannot run
  without belongs in that same guard.

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
- **A claim abandoned for a reason that is not the job's fault is released, never left to
  recovery.** Ctrl+C and an OpenRouter 402 or 429 all say nothing about the lead, so they
  call `release_analysis_job` / `release_search_job` and the work is claimable at once;
  the API's `scraper:recover-stale-jobs` is the backstop for a worker that *died*, and it
  costs `SCRAPER_CLAIM_TIMEOUT_MINUTES`. A genuine analysis failure is the opposite — it
  has used one of the lead's three attempts and must keep it. Note that `KeyboardInterrupt`
  is a `BaseException`: an `except Exception` will not see it.
- **The interrupt handler wraps the waiting, not just the working.** `main()`'s sleeps —
  the poll, and the longer pauses a rate limit and a payment error wait out — are where
  an idle worker spends almost every second of its life, so that is where Ctrl+C lands.
  With the handler inside the loop they sat outside it and an interrupt answered with a
  traceback more often than not; it now encloses the whole loop.
- **The same line divides a search that failed from one that was interrupted.** Losing the
  API mid-scrape — `ConnectionError`, `Timeout` — says nothing about the search, and
  recording a failure would mean calling the API that just could not be reached, so the
  search is released. Everything else is the scrape itself: a selector that no longer
  matches, a consent screen that changed, a payload the API refused. Those fail the search
  on purpose, because the message reaches the panel where somebody sees it, and a job that
  silently bounces hides a broken scraper behind a queue that never empties.
- **A 5xx is retried in the client, and never released.** `_request()` tries a 500, 502, 503
  or 504 twice more and then hands the failure up. The asymmetry with a lost connection is
  deliberate: losing the connection throttles itself, because claiming a job needs the API
  too, whereas a 500 leaves the API perfectly able to hand the same search out again — so a
  deterministic one, the kind a particular lead triggers, would bounce that job for ever and
  re-scrape Maps on every round. Bounded retries let a passing fault through and leave a
  real one as a failed search somebody can see. Every call is safe to repeat; the reasoning
  per endpoint is in that docstring, including the one exception, a lost claim.
- **Trim to the API's limits at the boundary, in `_FIELD_LIMITS`.** One value over its
  column is a 422 on the whole request, and neither endpoint forgives one: the ingest marks
  the entire search failed, and a refused report used to retire the lead. Prose and URLs are
  cut — a shorter address or pitch is degraded but usable — while an identifier is dropped,
  because half a phone number or a truncated email is not a shorter answer, it is a wrong
  one someone will dial or write to. The same applies to `error_message`: a 422 on the *fail*
  call leaves the search looking like a live run.
- **Releasing a claim refunds the attempt, so only release when the next try can succeed.**
  A shutdown, a 402 or a 429 will: the work is claimable again at once, and the loop waits
  the limit out — `_PAYMENT_PAUSE` for credit, which is topped up by hand, and the shorter
  `_RATE_LIMIT_PAUSE` for a limit that lifts on its own. A payload the API refused
  will not — the re-claim gets refused identically and each round burns an LLM call, for
  ever. There the claim is left standing (`ReportRejected`), and stale-claim recovery plus
  the three-attempt ceiling bound it while the log names the refused fields.
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
- A lead whose footer is built client-side costs a Chromium process, and that, not the
  request count, is what sizes a host running this worker. It is no longer reported in
  the terminal — it said nothing about the lead — so measuring it again means counting
  `compliance_rendered`, which every analysis still carries.
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
- The pitch is made on the call. The generator produces the argumentario and nothing
  else: a drafted email was one more text to review before sending, for a channel the
  agent was not using
- Contact is semi-manual (not mass automated) to comply with GDPR
- The argumentario is a draft to read and adapt, never a script to recite

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
