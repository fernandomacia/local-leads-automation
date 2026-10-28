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

### Added

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
  decides what that is. The cards, the OpenRouter calls and the log handler come next.
