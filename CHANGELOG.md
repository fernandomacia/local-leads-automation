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
