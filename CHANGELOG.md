# Changelog

All notable changes to this worker are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions are `APP_VERSION`, which is also what the API's Compatibility table keys its
worker requirements to.

> **This file starts here, and does not reconstruct what came before.** The tags stop at
> `v2.3.0`, so the only record of 2.4.0 through 2.8.x is the commit messages and the project
> phases in `CLAUDE.md`. Rebuilding those entries from them now would read tidier than the
> history actually was, which is the one thing a changelog cannot afford.

## [Unreleased]

### Changed

- **`maps_card_issues()` and the domain normalisation are leaf modules now**
  (`scraper/card_issues.py`, `scraper/domains.py`), so the audit can record a card's
  domain and its findings without Chromium's package being a requirement of recording
  anything. Both were private to the Maps scraper, which imports Playwright at module
  scope; the functions are unchanged, and a test imports `audit.recorder` in a clean
  interpreter to keep it that way.
- **A card's findings are recorded with it** rather than left for the ingest alone.
  `maps_card_issues()` is pure — a dict in, a dict out — so the same call fills the API's
  payload and the card's audit row from the same scraped card, and `cards.maps_issues` is
  the one reading of a card that `purge_audit_content()` leaves standing after 90 days.

### Added

- **Every call to OpenRouter is recorded, with the prompt sent and the answer received in
  full.** One row per call: the model, the temperature and the token ceiling actually sent,
  one entry per HTTP attempt with its status, its latency and the wait applied after it,
  the final status, the total latency and the tokens spent. Written from a `finally`, so a
  call that timed out or was refused leaves the same trace as one that answered, and the
  exception reaches the worker untouched — it is what decides whether the lead goes back
  to the queue. The system prompt travels with its SHA-256, which outlives it: the text is
  cleared after the retention window and the hash still answers which version of the
  prompt produced a given pitch. Nothing is recorded outside an analysis, and the request
  headers never reach the recorder, because the API key is in them.
- **Local audit outbox (SQLite, per machine) for the shared audit store.** One file per
  machine, outside the repository. `put()` stores one fragment of an audit row — the columns
  known at that moment — and never raises: it is called from inside a search and an analysis,
  where an audit failure must never turn into a lead reported as failed. Fragments drain in
  the order they were written, so a row's first fragment, the one carrying its required
  columns, always reaches Neon before the later ones that complete it.
- **The outbox drains into the shared store from a background thread in the worker.** With no
  `AUDIT_DATABASE_URL` it does nothing and everything stays in the local file. Each fragment
  updates the columns it carries and inserts the row only if it was not there yet: Postgres
  checks `NOT NULL` on the row it is asked to insert before it looks for a conflict, so a
  plain upsert refused every fragment that *completed* a row rather than opening it. An
  outage marks nothing and retries the batch after a growing pause; a refusal replays the
  batch one fragment at a time under savepoints, so the one fragment Neon will never take is
  set aside with its error instead of holding back every fragment written after it.
- **The worker records what it did: runs, search attempts, analyses, and the pauses it waits
  out.** A run says which machine and environment it ran on and whether it stopped or
  crashed; a search attempt says how it ended and how many cards it saw against how many
  leads were reported and how many the API actually inserted; an analysis says its outcome,
  how long it took, the error that ended it and the payload the API was sent — which on a
  refused report is the one thing worth having afterwards. `worker.py` names no table and no
  column: it says a search was released because the API went out of reach, and `recorder`
  decides what that is. The OpenRouter calls and the log handler come next.
- **Every Google Maps card the scrape examines is recorded, the kept ones and the dropped
  ones.** A kept card holds the lead as the scraper produced it, with its domain normalised
  the way the API answers `POST /domains/check` so the two can be compared at all; a card
  dropped for a domain already in the system holds only that domain, because the question
  is answered from the listing, before the card's own tab is opened. The dropped ones are
  what this is for: they never reach the worker, so nothing afterwards could explain a
  search that looked at forty businesses and reported four.
