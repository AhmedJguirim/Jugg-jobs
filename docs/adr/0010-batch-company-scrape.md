# 0010 - Batch company scrape with per-employer progress

Status: Accepted - 2026-09-28

## Context

Scraping employers one Scrape button at a time does not scale past a handful.
The user wanted to say "scrape the first 10 employers last scraped before
28/09/2026", cancel it if needed, hear the finish sound once when *all* of them
are done, and watch each employer's progress in the form
"company - vacancy 5/23 - translating the description".

Constraints carried over: each employer must run the *same* flow as the single
button (`scrape_company_workflow`, with its three caps), and the GPU rule is
untouchable - every model call still goes through the one-slot `llm` queue.

## Decision

**Selection is the listing.** `POST /api/companies/scrape-batch` takes a count
plus the same filters and sort as `GET /api/companies`, and scrapes the top
`count` rows of that listing. A new `scraped_before` filter (with
`include_never_scraped`, default on) makes "due for another look" a filter the
page already shows, so what gets scraped is exactly what is on screen. Capped
at 50 employers.

**One parent workflow, one child per employer.** `scrape_companies_workflow`
enqueues a `scrape_company_workflow` per employer on a new `company-scrape`
queue (`worker_concurrency=2`) and waits for all of them. Two at a time because
the GPU is the bottleneck: one employer is being graded while the next is
fetched and queued. A separate queue so batch scrapes waiting on the GPU never
hold the `job-ingest` page-fetch slots. Children get deterministic ids
(`{batch}-company-{id}`), so status needs nothing but the batch id.

**Progress is published as DBOS workflow events**, not a table:

- a company scrape sets `progress` as it moves: fetching page n/m, checking the
  model server, grading vacancy i/n (with the id of the vacancy it awaits);
- a vacancy's enrichment sets `stage`: translating, then grading.

`GET /api/companies/scrape-batch/{id}` reads those and composes one line per
employer (`app/services/scrape_progress.py`, DBOS-free and unit-tested). When the
awaited vacancy is still queued behind another employer's, the line says
"waiting for the GPU" - the honest reason for a long pause.

**Cancel reuses the existing endpoint.** `cancel_children=True` is recursive in
DBOS, so cancelling the batch stops waiting employers, running ones, and every
vacancy they queued for the GPU. Verified: nothing left PENDING or ENQUEUED.

**The page tracks one batch**, mirrors each employer's line into its list row
(so its own Scrape button cannot double-start it), plays the finish sound once
when the batch ends, and remembers the batch id in `localStorage` so a reload
picks the progress back up without chiming again.

## Consequences

- `set_event` calls change the step sequence of two workflows, so the DBOS
  application version went to 0.3.0. It now lives in one constant,
  `APPLICATION_VERSION` in `app/workflows/enrichment.py`, shared by `main.py`
  and `scripts/enrich_jobs.py`; the script had its own copy, and
  `--clear-stuck` cancels PENDING workflows of any version but its own, so a
  lagging copy would have cancelled healthy work.
- Progress for a vacancy is inferred from the one the company scrape is
  awaiting. Children finish in queue order, so this is accurate in practice;
  it is not a per-vacancy status table.
- Polling cost grows with batch size (a few status reads per employer every
  two seconds); fine at the 50 cap.
- A batch is not resumable from a different browser - only the one that
  started it (or reloaded it) knows the id.
