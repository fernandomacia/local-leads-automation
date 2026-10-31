# Changelog

All notable changes to this worker are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions are `APP_VERSION`, which is also what the API's Compatibility table keys its
worker requirements to.

> **This file starts here, and does not reconstruct what came before.** The tags stop at
> `v2.3.0`, so the only record of 2.4.0 through 2.8.x is the commit messages and the project
> phases in `CLAUDE.md`. Rebuilding those entries from them now would read tidier than the
> history actually was, which is the one thing a changelog cannot afford.

## [2.10.0] - 2026-10-31

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

  **One file per API, not just per machine.** Each API holds the audit of the worker that
  reports to it, so a local run is recorded locally and a production run in production — and
  the fragments of a run against one API can only ever be sent to that one, because a search
  attempt names a `lead_search_id` the other has never heard of. The name says which:
  `outbox-127.0.0.1-8000.sqlite`, `outbox-platform.segurseo.com.sqlite`. The old single
  `outbox.sqlite` is simply no longer read; it is neither migrated nor deleted, because
  nothing can say which API its fragments were written against, which is the whole reason
  this changed.

- **The audit is sent to the API.** A daemon thread drains the local outbox to
  `POST /api/scraper/audit/fragments`, in `seq` order, with the same bearer token as the
  rest of the worker's calls. **Requires SegurSEO-API 13.4.0 or newer**, which is where that
  endpoint exists; against an older API the request 404s and the fragments stay pending,
  which is exactly what the outbox is for.

  Batches are capped at 200 fragments and 450 KB — under the API's 512 KB, because the
  request that arrives also carries the envelope and the headers, so sending right up to
  the limit would earn a 413 for a batch that looked fine from here. A fragment over the
  limit on its own can never be sent and is marked failed locally with that reason.

  **Nothing is marked unless the API said something about that fragment.** A 401, a 422, a
  429, a 500, a timeout, a body the worker cannot read and a `seq` the response left out are
  all "ask again later": the fragments stay pending and travel with the next run. Only an
  `accepted` or a `rejected` takes one out of the queue, and a rejection keeps its reason in
  the outbox. A 401 or a 422 waits five minutes and says so once per streak rather than once
  per attempt — the first needs a token fixed and the second is a bug in this worker, and
  neither is helped by hammering the endpoint.

  `stop()` joins the thread and then empties what is left itself, so two threads are never
  draining at once, and reports what is still pending — which is the number the worker
  already printed on its way out.

- **A test module carrying the API's audit contract.** The tables, their columns, the types,
  the values each CHECK allows and what every row cannot be created without, written out on
  this side and held against every fragment the recorder emits. The API answers a fragment
  it cannot accept with a `rejected` inside a 200, recorded in a local outbox nothing reads,
  so without this a recorder that sent `"3"` where an integer goes would lose that row
  silently, hours after the run that produced it.

### Changed


- **The fragments match the API's column names**: `api_search_id` is `lead_search_id` and
  `api_lead_id` is `lead_id`, on the tables that carry them.
- **A recorded card now carries its `maps_url`**, and it is not one more detail among the
  rest: it is the only thing tying a card to the lead it became, because the card is
  recorded before that lead exists and carries no lead id. The API matches on it to empty a
  card's content when its lead is deleted, so a card stored without it was personal data
  that erasure could not reach.
- **Every value is of the type the API types its column as.** `"3"` is not an integer there
  and `1` is not a boolean, and three of the sources the recorder reads are outside this
  worker's control: the job dicts the API hands back, the scraped card, and OpenRouter's
  `usage` block, which sends token counts as JSON numbers and has been seen to send `cost`
  as a string. Each one is coerced on the way in, and a value that cannot be read is dropped
  on its own rather than costing the fragment that carries it.
- **A job whose id is not a UUID records nothing for that row.** `search_attempts.lead_search_id`
  and `analyses.lead_id` are resolved by the API against its own rows, so such a row can
  only ever come back rejected; queueing one would fill the outbox with fragments that are
  refused one request at a time.

