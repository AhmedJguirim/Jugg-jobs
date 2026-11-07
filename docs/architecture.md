# Architecture

Living document. Update it in the same change that alters the structure it
describes; record *why* a decision was made in `docs/adr/` and link it here.

## What this application is

Jugg Jobs is a job search tool that runs on one machine. It collects links to postings on
Ashby, Lever, Greenhouse, Workable and Breezy HR, fetches each posting, grades it
against a candidate profile with a small local model (`qwen3.5:4b` on Ollama), and
shows the result on a board sorted by fit, with eligibility reported separately.

DBOS makes the multi-step work durable: fetching hundreds of pages or grading a
batch for an hour survives a crash or restart. PostgreSQL holds everything.

The project began as a tool for Dutch job boards; that pipeline was removed when
it was published (ADR-0025). Some table and column names still carry that
history.

## The flow

```
 Google results ──paste──> manual_job_listing_urls      /workspace/urls
                                   │
                          "Process all" (fetch)          listing-fetch queue, 1 at a time
                                   v
                           raw_job_listings              /workspace  (the waiting list)
                                   │
                         Grade / Bulk grade              listing-grade queue, 2 at a time
                                   v
              parse -> prepare -> grade (GPU) -> persist   llm queue, 1 at a time
                                   v
             jobs + job_translations + job_assessments   /board
```

Nothing is graded as a side effect: fetching only stores pages, and grading
starts from a button (ADR-0016). A posting reaches `jobs` only once it has a
grade, so the board never shows a half-processed row (ADR-0015).

## Layers

Dependencies point one way only: outer layers may import inner ones, never the
reverse.

```
  main.py                    entry point: page routes + DBOS launch
      |
      v
  app/api/                   HTTP API behind the pages
  app/workflows/             DBOS workflows, steps and queue registration
  app/services/              flows: order several repository/client calls
  app/repositories/          all SQL; explicit Session, flush() but never commit()
  app/integrations/          outbound clients: Ollama, the posting pages, nvidia-smi
  app/parsers/               pure: a page's HTML in, a JobCreate out
      |
      v
  app/llm/                   profiles, scoring rules, the grader and its prompts
  app/text/                  HTML sanitising, a small DOM, Google-results parsing
  app/schemas/               pydantic DTOs
  app/db/                    engine, session, models
      |
      v
  app/config.py              settings from the environment / .env
```

## Module map

| Path | Responsibility |
|---|---|
| `main.py` | FastAPI app, page routes, DBOS config, queue registration after launch |
| `app/api/workspace.py` | Links (extract from pasted results, remove, *Process all*), raw listings (save, list, remove, clean up), grading (one, a batch, progress, cancel) |
| `app/api/board.py` | The board's data, one posting's detail, the applied/archived marks |
| `app/workflows/llm_queue.py` | The single-slot `llm` queue, the GPU cooldown, the model health check, `APPLICATION_VERSION` |
| `app/workflows/listing_fetch.py` | `fetch_listings_workflow`: one checkpointed step per link, on the `listing-fetch` queue |
| `app/workflows/listing_grading.py` | `process_listing_workflow` (one listing), `grade_listings_workflow` (a batch), `grade_listing_workflow` (the GPU step) |
| `app/workflows/listing_processing.py` | The load and parse steps the grading workflow starts with |
| `app/services/listing_fetch.py` | Fetch one link, cut the page down to the posting, store it as a raw listing |
| `app/services/listing_grading.py` | Prepare the text, call the grader, create the job with its text and grade |
| `app/services/listing_progress.py` | DBOS status + stage events -> one readable progress line |
| `app/integrations/listing_pages/` | Per platform: which address to request and which element to keep; Ashby and Workable pages are rebuilt from the JSON they embed |
| `app/integrations/ollama/` | The only code that occupies the GPU |
| `app/integrations/gpu/` | GPU temperature from `nvidia-smi` (None when it is missing) |
| `app/parsers/` | Platform from the URL, then one parser per platform returning `JobCreate`; `common.py` holds what they share |
| `app/llm/global_grader.py` | The grader: the model reports restrictions with quotes, code checks the quotes and decides |
| `app/llm/global_prompts.py` | The grading and JSON-repair prompts, and their version |
| `app/llm/scoring.py` | Rules decided in code: stack tier and score bands, permit evidence, work authorisation, tolerant JSON parsing |
| `app/llm/geo.py` | Countries, regions and nationalities, for "hires only in X" |
| `app/llm/profile.py` | Loads `profiles/<name>.json` |
| `app/text/sanitize.py` | HTML -> clean text for the model, plus its hash |
| `app/text/dom.py` | A small stdlib HTML tree used by the parsers and the fetcher |
| `app/text/search_results.py` | Result links out of a pasted Google `<div id="search">` |
| `html/` | The pages: Tailwind + Alpine from CDNs, no build step. `_topbar.html` is injected into each |
| `scripts/process_listing.py` | Grade raw listings from the command line |
| `scripts/clear_stuck.py` | Cancel workflows stranded by an older `APPLICATION_VERSION` |
| `tools/cmdlog.py` | Append-only audit log of shell commands run by an AI agent (ADR-0005) |

## Data model

### `manual_job_listing_urls`

Links extracted from pasted Google results (ADR-0013): `url` (unique),
`scraped_at`, `removed_at` and timestamps. A removed link keeps its row so the
same search cannot add it back (ADR-0017).

### `raw_job_listings`

One posting page as stored, by *Process all* or pasted by hand: `url`,
`html_body`, and where it stands (ADR-0016) - `status` NULL means waiting to be
graded; otherwise `graded`, `not_found`, `unparseable`, `unsupported` or
`removed`. `job_id` points at the job made from it. The **Cleanup** button
empties the HTML of graded listings to save space (ADR-0018).

### `jobs` and its satellites

```
  companies <---- jobs ----> locations
                   |-- job_categories / job_industries / job_education_levels /
                   |   job_contract_types       (small vocabularies)
                   '-- job_dco_titles
```

`(source, external_id)` is the natural key, so grading the same posting twice
updates one row. `source` is the platform (`ashbyhq`, `lever`, ...). The
vocabulary tables and the `dco_title` columns were designed around the Dutch job
board's data (ADR-0006) and are mostly empty for worldwide postings. `applied_at`
and `archived_at` are the user's own marks (ADR-0009).

### `job_assessments` and `job_translations`

Kept apart from `jobs` because they are expensive to produce and carry their own
provenance: `source_hash` (of the cleaned text), `model` and `prompt_version` on
every row. A posting whose cleaned text and prompt version are unchanged is never
graded again.

`job_assessments` is keyed `(job_id, profile_name)`, so profiles never mix. It
keeps **fit** and **eligibility** apart (ADR-0008): `fit_score` rates the work
only; `eligibility`, `blocked`, `requires_existing_permit`, `offers_sponsorship`
and `red_flags` carry the barriers. "A great role you cannot take" stays
distinguishable from "a role in the wrong field".

`job_translations` holds the cleaned text the grade was made from, which the
board's drawer shows. For worldwide postings it is marked `skipped` - nothing
was translated - and the name is a leftover of the Dutch pipeline.

### `jobs_test`, `job_listings_test`, `companies`

Kept, not written by anything any more (ADR-0025). `jobs_test` was where new
parsers were checked before being hooked in (ADR-0014); `job_listings_test` held
the fetcher experiment (ADR-0023). `companies` is still where a job's employer
lives; its job-board link columns are from the Dutch seed data.

## How a posting is graded

The model is asked only for judgement. Anything checkable is computed (ADR-0008,
ADR-0015):

```
  classify_stack()            <- code: which tier of the profile's skills is this
        |                        posting's stack? weighted keyword match, title 10x
        v
  band = STACK_BANDS[tier]    <- core 75-95 | proficient 55-75 | familiar 30-55
        |                        unrelated 10-30 | non_technical 0-10
        v
  the model scores inside the band and reports restrictions, each with a quote
        v
  code checks every quote against the text, then the restriction against the
  profile, and clamps the score into the band
```

The tier is code because, asked to pick it, the 4B model kept calling a Python
posting "unrelated" on a profile listing Python as familiar. Restrictions need
quotes because the model infers them from how a job "feels": it once "quoted" a
real sentence that said nothing about permits as proof that a permit was
required. A quote must appear in the advert **and** say what the claim needs.

Eligibility is a traffic light, never part of the score:

| Tier | Meaning |
|---|---|
| `blocked` (red) | the advert rules the candidate out: a language they lack, a country they are not in, a permit or nationality they do not hold |
| `relocation` (orange) | reachable with a move or a visa conversation |
| `remote_ok` (green) | doable from where the candidate already is |

An unreadable reply costs one grade, not the posting: truncated JSON is closed
and its finished fields kept (`fit_score` and `verdict` come first), one repair
call is tried, and if that fails the listing stays on the waiting list.

## The GPU constraint

There is one GPU. Every model call is a workflow on the `llm` queue, registered
with `worker_concurrency=1` **and** `global_concurrency=1`. The second limit is
enforced in the DBOS system database, so a second process on the same state
still cannot put two jobs on the card. Verified by enqueueing six jobs at once
and observing peak concurrency 1.

Fetching runs on its own `listing-fetch` queue (one at a time, so the 1-3 s
random pause between requests means something), and grading batches on
`listing-grade` (two at a time: one on the GPU, the next being read). Neither
waits on the other.

**The queue pauses when the GPU runs hot.** Before each grade,
`grade_listing_workflow` reads the temperature with `nvidia-smi`; above
`GPU_MAX_TEMP_C` (80°C) it `DBOS.sleep`s for `GPU_COOLDOWN_SECONDS` (3 min),
re-checks, and gives up after `GPU_MAX_COOLDOWNS` (5) pauses. It holds the `llm`
slot while it waits. Without `nvidia-smi` the check never pauses (ADR-0011).

**`register_queues()` must be called after `DBOS.launch()`**: it writes the
limits to the system database and raises if called earlier.

**Bump `APPLICATION_VERSION` (`app/workflows/llm_queue.py`) when a workflow's
step sequence changes** - a `DBOS.set_event` counts as a step. DBOS replays a
recovered workflow against the current code and aborts with
`DBOSUnexpectedStepError` when the recorded steps no longer line up.

**A version bump can wedge the queue.** DBOS only recovers workflows of the
current version, so a job left PENDING by the old one is never resumed while
still counting against `global_concurrency`. On a single-slot queue, one orphan
stops everything. Symptom: listings waiting, no GPU activity, no errors.
`scripts/clear_stuck.py` cancels them; stop the app first.

## Conventions

- **Transactions belong to the caller.** Repositories `flush()` but never
  `commit()`, so several calls compose into one unit of work.
- **Get-or-create runs inside a SAVEPOINT** (`app/repositories/job.py`), so two
  workers racing to create the same company or location do not deadlock or
  violate a unique constraint.
- **Domain errors, not ORM errors.** Repositories raise `JobNotFound`,
  `RawJobListingNotFound`; callers never catch `IntegrityError`.
- **Schema changes only via Alembic.** `tests/unit/test_migrations.py` fails if
  models and migrations drift.
- **Plain dicts cross workflow-step boundaries**, not pydantic models, so
  checkpointed values stay portable.
- **A page that cannot be used is a result, not an exception.** A removed posting
  or an unsupported site is a returned status; raising would make DBOS retry a
  deterministic outcome.
- **Prompts are versioned.** Bump `GLOBAL_ASSESSMENT_PROMPT_VERSION` when a
  prompt changes; that is what marks existing grades stale.
- **Descriptions are sanitised before the model sees them**: double-encoded
  entities are unescaped until stable and repeated boilerplate lines dropped.

## Testing

| Suite | Command | Needs |
|---|---|---|
| Unit | `pytest tests/unit -q` | PostgreSQL (`jugg_jobs_test`) |
| E2E | `pytest -m e2e -q` | the app on :8000 (started if not running) + Chromium |

Unit tests run against real PostgreSQL rather than SQLite, because the
repositories rely on `ON CONFLICT` and `ILIKE`. Each test runs inside a
transaction that is rolled back (ADR-0004); the schema is dropped and recreated
once per session. No unit test makes a network request or calls the model.

## Known gaps

- No authentication; the app listens on all interfaces.
- Links are collected by hand from Google results; there is no search API.
- `scripts/process_listing.py` uses its own DBOS state, so the one-GPU limit does
  not span it and a running app. Setting `DBOS_SYSTEM_DATABASE_URL` for both
  would close this.
- Grading quality is bounded by a 4B model. Quote checks, JSON mode and field
  coercion contain it; a larger model is a settings change (`LLM_MODEL`).
