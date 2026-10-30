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

- **The worker keeps an audit trail of everything it does.** One row per run, per attempt at
  a search, per Google Maps card it looked at, per attempt at analysing a lead, per call to
  OpenRouter, and per warning or error it logged.

  A run says which machine it ran on and which version of the worker it was, when it
  started, when it was last alive, and whether it stopped or crashed — recorded before the
  worker says so on the terminal, so the two can never disagree. A search attempt says how
  it ended and how many cards it saw against how many leads were reported and how many the
  API actually inserted. A card says whether it was reported or dropped for a domain already
  in the system, with the lead as the scraper produced it; the findings about the listing
  outlive the rest of it, so they are what a long-term count of cards with no website is
  answered from. An analysis says its outcome, how long it took, the error that ended it and
  the payload the API was sent — which on a refused report is the one thing worth having. A
  call to OpenRouter keeps the prompt and the answer in full, every HTTP attempt with its
  status, its latency and the wait applied after it, the tokens it spent, and whether its
  answer could be used at all, which is not the same question as whether the call worked.

  `worker.py` names no table and no column: it says a search was released because the API
  went out of reach, and the recorder decides what that becomes. Warnings and errors need no
  cooperation at all — a handler on the root logger files them, and the worker's console
  output is unchanged. Nothing in the audit can break a run: every entry point swallows its
  own failures, because a lead reported as failed over an audit row would be a worse outcome
  than no audit at all.

- **A local outbox, one SQLite file per machine.** Every record is written there as a
  fragment of a row — the columns known at that moment — and the fragments that follow
  complete it. The outbox keeps what is pending, what has been sent, and what was refused
  along with the reason, so one bad fragment never holds back the ones written after it;
  records already sent are cleared after seven days and refused ones after thirty, because
  those are the ones somebody will want to read.

  **Nothing is sent anywhere yet.** The destination is SegurSEO-API, which has no endpoint
  for this: each API will hold the audit of the worker that reports to it, so a local run is
  recorded locally and a production run in production. Until that exists the fragments
  simply accumulate, which is why the outbox already draws the distinction an uploader
  needs.
