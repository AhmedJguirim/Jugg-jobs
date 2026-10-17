# 0016 - Grade listings on demand: a waiting list, single and bulk

Status: Accepted - 2026-10-17

## Context

ADR-0015 started grading as soon as a raw listing was saved. Grading takes the
GPU for minutes (about nine per listing when the model has to run on the CPU),
so saving a page committed the machine to work the user had not asked for yet,
and the form page listed every stored listing, graded or not.

The user wants to save pages quickly, then choose: grade one listing, or queue up
to 100 and leave. The GPU temperature pause (ADR-0011) must apply, as it does for
the company batch (ADR-0010).

## Decision

- **Saving only stores.** `POST /api/workspace/raw-job-listings` no longer starts
  a workflow. A URL on a site with no parser (Nationale Vacaturebank included)
  is stored with `status = unsupported`.
- **The raw listing records where it stands.** New columns on
  `raw_job_listings`: `status`, `job_id`, `processed_at`. NULL means waiting;
  `graded` (with the job), `not_found`, `unparseable`, `unsupported` are the
  others. The workflow's last step (`mark_step`) writes it. A run that could not
  finish (model down, unreadable reply) writes nothing, so the listing stays
  waiting and can be tried again. `unparseable` also stays on the list, labelled
  "Retry", because a parser fix can make it work. The migration backfills
  listings that already produced a job (from `jobs.raw.raw_listing_id`) and ones
  on unsupported sites.
- **The list under the form is the waiting list**, oldest first, so "the first N"
  is the top. Each row has a Grade button and shows its run's progress line.
- **Bulk grading mirrors the company batch.** `POST /api/workspace/grading-batch`
  takes the first `count` (1-100) waiting listings and starts
  `grade_listings_workflow`, which enqueues one `process_listing_workflow` per
  listing on a `listing-grade` queue (two at a time) and waits. Each listing's
  grading is still the single `llm` queue slot, preceded by the same temperature
  check and cooldown, so a hot GPU pauses a batch exactly as it pauses a
  company scrape. Cancelling uses `cancel_children=True`. The page shows a
  progress bar and one line per listing, remembers the batch in `localStorage` so
  a reload resumes it without chiming again, and plays the same finish sound
  (shared mute preference).
- **Progress needs no table and no events.** Child ids are derived
  (`{batch}-listing-{id}`, and `{run}-grade` for the GPU step), so the status
  endpoint reads the run's DBOS status and the grading step's stage event
  ("waiting for the GPU", "GPU at 85C - cooling down for 3 min", "grading").
  `app/services/listing_progress.py` turns them into text.

## Consequences

- `APPLICATION_VERSION` is 0.5.0: the listing workflow gained a step. Nothing was
  in flight when it was bumped.
- Re-grading a listing (the Retry button, or running it again) is safe: the job
  is an upsert, and a current grade skips the model.
- Two clicks on one listing's Grade button are prevented in the page, not on the
  server; a second tab could start a duplicate run. It would upsert the same job.
- `GET /api/workspace/raw-job-listings/process/{id}` (poll one run) is replaced
  by `GET /api/workspace/grading/{id}`.
