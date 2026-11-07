# 0024 - "Process all": fetched pages go onto the waiting list

Status: Accepted - 2026-10-28

## Context

ADR-0023 showed that the page of a collected link can be fetched and cut down to what
a hand copy would be, for every platform that has a parser, and stored in the
experimental `job_listings_test`. The next step is to use it: a button on
`/workspace/urls` that does for every waiting link what pasting its HTML does by hand,
so nobody has to open each link and copy it.

Pasting by hand creates a raw listing, which then waits to be graded (ADR-0016).
Grading takes the GPU for minutes per listing and is on demand, never a side effect of
saving HTML.

## Decision

- **"Process all (N)"** on `/workspace/urls` fetches the page of every live collected
  link that has no raw listing and is on a platform with a fetcher, and stores each as
  a **raw listing** - the same row a paste makes. It does **not** grade anything: the
  pages join the waiting list and are graded with the Grade buttons or a grading batch.
  `N` is `fetchable` in the list endpoint; links on a site without a parser (they stay
  in the list, as before) are not counted and not touched.
- **What is stored, per outcome** (`listing_fetch.store_as_raw`):
  - page fetched: a raw listing, waiting;
  - the platform says the posting is gone (404, or a Greenhouse/Breezy removal answer):
    a raw listing marked `not_found`, so it leaves the list for good, like a graded
    "job not found";
  - blocked (403/429/5xx), a timeout, or a page without the posting: **nothing is
    stored**. The link stays without HTML and the next click tries it again; the run
    reports it and why;
  - a raw listing for the link appeared meanwhile: left alone.
- **A durable workflow on its own queue.** `fetch_listings_workflow(urls)` on queue
  `listing-fetch` with `worker_concurrency=1` and `global_concurrency=1`: one run at a
  time, so the 1-3 s pause between requests (ADR-0023) holds. It is one checkpointed
  step per link, so a restart resumes at the next link instead of asking the servers
  again. A step that raises is counted as "to retry" and the run goes on. The pause is
  asked for by the workflow only after a link that made a request, and `paced` applies
  it to every further request inside a link (Workable makes up to three).
- **Progress** is a DBOS event (`progress`: total, done, added, gone, retry, exists,
  the link in progress, the last 15 problems), read by `GET .../process-all/{id}` with
  the run's state. `GET .../process-all` returns the unfinished run, so a reloaded page
  follows it again. `POST .../process-all/{id}/cancel` stops after the current link;
  pages already stored stay. A second start while a run is unfinished is a 409.
- **At most 500 links per click** (`MAX_PER_RUN`), oldest first; at the 1-3 s pace that
  is about half an hour. The rest are taken by the next click.
- **`job_listings_test` and `scripts/fetch_listings.py` stay** as the place to try a
  handler on a platform without writing to the real flow. The button does not write there.
- A new workflow changes no existing workflow's steps, so `APPLICATION_VERSION` is
  unchanged (as in ADR-0014).

## Consequences

- One click can put a few hundred pages on the waiting list. Grading them is a separate,
  deliberate step (a batch is capped at 100).
- A removed posting costs one request and one small row (a sentence, not the platform's
  whole 404 page: Lever's is ~300 KB), and is never asked about again.
- Run on all 11 never-pasted Lever links of the collected set plus a nonexistent one, in
  a scratch setup: 2 added, 1 gone, the unsupported site untouched; a second click while
  running was a 409 and a click with nothing left a 400.
- The app has to be restarted to get the new endpoints and queue.
