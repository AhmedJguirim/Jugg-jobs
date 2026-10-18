# Architecture

Living document. Update it in the same change that alters the structure it
describes; record *why* a decision was made in `docs/adr/` and link it here.

## What this application is

A job-hunting tool for the Dutch market. It tracks employers (`companies`) and
pulls vacancies from job boards - currently Nationale Vacaturebank (NVB) - using
durable workflows so a long multi-step fetch survives a crash or restart.

## Layers

Dependencies point in one direction only: outer layers may import inner ones,
never the reverse.

```
  main.py                    entrypoint: FastAPI routes + DBOS workflow wiring
      |
      v
  app/workflows/             DBOS workflows, steps and queue registration
  app/services/              flows: order multiple repository/client calls
  app/repositories/          all SQL for a table; returns ORM objects
  app/integrations/          outbound HTTP clients + their response models
      |
      v
  app/llm/                   prompts, candidate profiles, translate + grade
  app/text/                  HTML sanitising, language detection, chunking
  app/schemas/               pydantic DTOs: the application's public contract
  app/db/                    engine, session, models, declarative base
      |
      v
  app/config.py              settings from environment / .env
```

Supporting trees, outside the runtime path:

```
  alembic/        schema migrations (the only way the schema changes)
  scripts/        operational one-shots (ingest); invoked by humans/CI
  tools/          developer tooling, dependency-free (cmdlog)
  tests/unit/     fast tests against PostgreSQL
  tests/          e2e browser tests (Playwright), marked `e2e`
  docs/           this file + ADRs
```

## Module map

| Path | Responsibility |
|---|---|
| `main.py` | FastAPI app, DBOS workflows/steps/queues, HTTP routes |
| `app/config.py` | `Settings` from env/.env; builds the SQLAlchemy URL |
| `app/db/base.py` | `Base`, naming convention, `TimestampMixin` |
| `app/db/session.py` | Cached engine/sessionmaker, `session_scope()` |
| `app/db/models/` | ORM models; `__init__` is the registry Alembic reads |
| `app/schemas/` | DTOs for companies and vacancies + normalisation |
| `app/repositories/` | CRUD; raises domain errors, never commits |
| `app/services/` | Flow layer: job ingest, per-company scrape query resolution, batch progress lines |
| `app/api/board.py` | HTTP API behind the job board view; its one write sets applied/archived marks |
| `app/api/companies.py` | HTTP API behind the employers browser: listing, detail, single and batch scrapes |
| `html/board.html` | The job board: Tailwind + Alpine, served at `/board` |
| `html/companies.html` | The employers browser, served at `/companies` |
| `app/api/workspace.py` | HTTP API behind the workspace section: submit and list raw job listings (ADR-0012) |
| `app/llm/global_grader.py` | Grader for vacancies from any country: the model reports restrictions with quotes, code checks them against the profile (ADR-0015); `global_prompts.py`, `geo.py` support it |
| `app/services/listing_grading.py`, `app/workflows/listing_grading.py` | Grade a parsed listing on the `llm` queue, then create the job, its text and its grade together |
| `app/services/listing_progress.py` | One readable line per listing grading run, and a batch's counts |
| `app/parsers/` | Listing parsers: platform from the URL, one pure parser per platform returning `JobCreate` (ADR-0014). `common.py` holds what they share; `greenhouse.py` joined `ashbyhq.py` in the registry (ADR-0019); `lever.py` is written and checked in `jobs_test` but not yet hooked in (ADR-0020) |
| `app/text/dom.py` | Small stdlib HTML tree used by the parsers |
| `app/workflows/listing_processing.py` | `process_raw_listing_workflow`: raw listing -> parse -> `jobs_test` |
| `app/api/jobs_test.py` | Read-only API behind `/jobs-test`: list, and one record with its description |
| `html/jobs_test.html` | The parsed listings with a detail drawer, served at `/jobs-test` |
| `html/workspace.html` | Raw listings form and list, served at `/workspace` |
| `html/_topbar.html` | Section switch (Jobs / Workspace) and per-section nav, injected into every page by `main.py:render_page` |
| `app/workflows/` | DBOS workflows/steps; registers the `llm` and `job-ingest` queues |
| `app/llm/` | Versioned prompts, candidate profiles, translation and grading |
| `profiles/*.json` | Tiered candidate profiles; `LLM_DEFAULT_PROFILE` picks one |
| `app/text/` | HTML sanitising, language detection, chunking, content hashing |
| `app/integrations/ollama/` | The only code that occupies the GPU |
| `app/integrations/nvb/` | NVB client (`client.py`), models (`schema.py`), mapper (`mapper.py`) |
| `app/logging_setup.py` | File logger for development (`logs/dev.log`) |
| `scripts/ingest_companies.py` | Loads `companies.json` into `companies` |
| `scripts/ingest_jobs.py` | Ingests every result page for one or more queries |
| `scripts/enrich_jobs.py` | Queues translation/grading; also `--progress`, `--shortlist` |
| `profiles/*.json` | Candidate profiles a vacancy is scored against |
| `tools/cmdlog.py` | Append-only shell-command audit log with risk levels |

## Data model

### `companies`

The employers we monitor. Each `*_jobs_link` is a pre-built search URL for that
job board.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | integer | no | PK, identity; seed ids 1-2901 preserved |
| `name` | varchar(512) | no | **unique** - the upsert conflict target |
| `type` | varchar(128) | yes | absent from the seed data (ADR-0002) |
| `website` | varchar(512) | yes | absent from the seed data |
| `slug` | varchar(512) | yes | **unique** |
| `province` | varchar(128) | yes | indexed; null for 45 seed rows |
| `google_jobs_link` | text | yes | |
| `linkedin_jobs_link` | text | yes | |
| `indeed_jobs_link` | text | yes | |
| `glassdoor_jobs_link` | text | yes | |
| `nationalevacaturebank_jobs_link` | text | yes | |
| `created_at` | timestamptz | no | `server_default now()` |
| `updated_at` | timestamptz | no | `server_default now()`, `onupdate now()` |

Known data-quality issue: `province` actually holds **cities** in the seed data
("Amsterdam", "Eindhoven", "Delft" - 166 distinct values). The column name was
specified; the contents were not cleaned. See ADR-0002.


### Vacancies

Twelve tables in total. What gets its own table was decided by sampling 200
live vacancies rather than by uniform rule - see ADR-0006.

```
  companies <---- jobs ----> locations
                   |
                   |-- job_categories ---------> categories
                   |-- job_industries ---------> industries
                   |-- job_education_levels ---> education_levels
                   |-- job_contract_types -----> contract_types
                   |-- job_dco_titles           (ranked, scored)
```

- **`jobs`** - the vacancy. Everything 1:1 lives here as columns (salary band,
  working hours, apply details, contact person, street/postcode), plus
  `job_titles` as an array of free-text keywords and `raw` as the untouched
  source payload.
- **`locations`** - places, deduplicated. Cities repeat heavily across
  vacancies, so city/province filtering becomes an indexed join.
- **Four vocabularies** - small closed sets (16, 30, 5 and 5 distinct values).
  Each association row carries `is_primary`, recording the value the API
  reports in its singular field without duplicating it into a column.
- **`job_dco_titles`** - the classifier's ranked candidate occupation titles,
  each with a `similarity` score and a `generic_title` bucket. Rank 0 is
  mirrored onto `jobs.dco_title` so the common filter needs no join.

`(source, external_id)` is the natural key, which makes re-ingesting a search
result idempotent. Vacancies also fill in `companies.slug`, `type` and
`website` - the columns the seed file left empty.

The vacancy logo is deliberately not modelled or stored.


### Enrichment: `job_translations`, `job_assessments`

LLM-derived data lives apart from `jobs` because it is expensive (~16s of GPU
each) and has its own provenance: `source_hash`, `model` and `prompt_version` on
every row. That gives re-ingest safety (updating a vacancy never destroys GPU
work), an off switch (unchanged text is never re-translated), and cheap prompt
iteration (a new version re-runs only older rows).

`job_assessments` deliberately separates **role fit** from **eligibility**:

- `fit_score` (0-100) rates the work only.
- `blocked`, `dutch_required` and `red_flags` carry the barriers.

So a Python role requiring fluent Dutch scores 85 *and* `blocked=true` - "a great
job you cannot take", which is different and more useful than a low score. The
shortlist hides blocked rows by default. See ADR-0007.

### Workspace: `raw_job_listings`

Listings pasted in by hand (ADR-0012): `id`, `url` (text), `html_body` (text,
stored exactly as pasted) and `created_at`/`updated_at`. Not yet linked to
`jobs` or processed in any way.

### Workspace: `raw_job_listings`

`id`, `url`, `html_body` (as pasted; emptied for graded listings by the Cleanup button, ADR-0018), and where it stands (ADR-0016): `status` (NULL = waiting to be graded,
`graded`, `not_found`, `unparseable`, `unsupported`, `removed`), `job_id` (the job made from it) and `processed_at`.

### Workspace: `jobs_test`

Same columns, indexes and key as `jobs`, written by the listing parsers instead
(ADR-0014); not read by the board. The vocabulary links are not copied.

### Workspace: `manual_job_listing_urls`

Result links extracted from a pasted Google `<div id="search">` block (ADR-0013):
`url` (unique), `scraped_at` (null until scraped), `removed_at` (set when the user
removes the link, ADR-0017; the row stays so the link cannot be re-added) and timestamps. Parsed by
`app/text/search_results.py`; `/workspace/urls` is the page.

## The view

`/board` renders what the flows produced: every vacancy with its English title
and summary, its fit score, and the dealbreakers that block it. Tailwind and
Alpine come from CDNs and the page is a single static file - no build step, which
keeps it consistent with the existing `html/app.html`.

Three endpoints back it:

- `GET /api/board` - the whole corpus without the long description bodies, so the
  page filters and sorts client-side with no round trips, plus headline stats
  and facet values. The corpus is now ~5,900 vacancies, so the page **paginates
  client-side** (25/50/100/200 per page, default 50, remembered in
  `localStorage`; any filter change returns to page 1): the cost of that many
  cards is the DOM, not the payload. If the payload itself becomes the problem,
  the next step is moving the filters server-side as `/api/companies` does.
  The **Language** filter keeps vacancies by the language they are written in
  (`source_language` on the translation row: English, Dutch, or not stated).
- `GET /api/jobs/{id}` - one vacancy including both texts: the English
  translation and the *sanitised* original, which is what the model actually
  read, so a suspect translation can be checked against its input.
- `PATCH /api/jobs/{id}/marks` - mark a vacancy **applied** or **archived**
  (or clear either). Stored as `jobs.applied_at` / `jobs.archived_at`; the
  board hides marked vacancies by default and its **Show** filter brings back
  applied, archived or everything. Ingest never clears a mark. See ADR-0009.

`/companies` browses all 3017 employers. Unlike the board it paginates
**server-side**: 3000+ rows is past the point where shipping the table to the
browser is reasonable, so `/api/companies` takes `q`, `province`, `has_jobs`,
`scraped_before` (+ `include_never_scraped`), `order_by` (`name`, `vacancies`,
`fit`, `recent`, `newest`/`oldest` by creation date), `limit` and `offset`, and `/api/companies/{id}` returns one employer
with its vacancies.

The employer list has two distinct origins, which the UI labels:

- **seed** (2901 rows) - from `companies.json`, carrying the pre-built Google,
  LinkedIn, Indeed, Glassdoor and Nationale Vacaturebank search URLs but no
  vacancies of their own;
- **vacancy** (116 rows) - discovered while ingesting, carrying `type` and
  `slug` and having actual vacancies attached.

The page also carries a static glossary for the API's Dutch controlled
vocabularies (`Vast` -> Permanent, `Ervaren` -> Experienced, HBO/MBO annotated).
They are closed sets of five values, so a lookup is cheaper and more reliable
than a model call - and the point of the page is not having to read Dutch.

## How a vacancy is graded

Scoring is split between code and model, deliberately (ADR-0008):

```
  classify_stack()            <- code: which skill tier is this vacancy's stack?
        |                        weighted keyword match, title weighs 10x body
        v
  band = STACK_BANDS[tier]    <- core 75-95 | proficient 55-75 | familiar 30-55
        |                        unrelated 10-30 | non_technical 0-10
        v
  model scores inside the band, and the result is clamped into it
```

The tier is a lookup, so it is computed exactly; the model is left with the
judgement call. This came from a failure: asked to pick the tier itself, a 4B
kept calling a Python vacancy "unrelated" on a profile that lists Python under
familiar.

Eligibility is a traffic light, derived in code from what the advert states
(ADR-0008 amendment):

| Tier | Meaning | Set when |
|---|---|---|
| `blocked` (red) | the advert rules the candidate out | it demands an existing permit or residency, or fluent Dutch |
| `relocation` (orange) | reachable, needs a move or a visa conversation | on site or hybrid, advert silent on permits |
| `remote_ok` (green) | doable from where the candidate already is | fully remote with no country restriction |

A permit claim is only honoured when the model quotes the sentence and that
quote both appears in the advert **and** contains language about the right to
work. Asked for evidence, the model returned "Laravel developer wanted for our
Utrecht office." - a real sentence saying nothing about permits. Verbatim
matching alone let an inference through wearing a quotation as a disguise.

Eligibility never moves the score. `dutch_required`, `sponsorship_required`,
`seniority_fit` and `blocked` are recorded beside it, so "a great role you
cannot take" stays distinguishable from "a role in the wrong field".

### Scraping one employer

Every seed company carries a Nationale Vacaturebank search URL of the shape
`?query=<urlencoded>`. `scrape_company_workflow` pulls that query out (falling
back to the company name for employers discovered from a vacancy), walks every
result page, then enriches what it found - the same flow as a manual ingest,
only the query differs.

`companies.last_scraped_at` and `last_scrape_vacancies` record the outcome. They
are stamped **after the scrape but before enrichment**, because the scrape is
genuinely finished at that point and a later GPU failure should not make it look
as though we never looked. Storing the timestamp is what distinguishes "looked
and found nothing" from "never looked" - a distinction `jobs` alone cannot make.

The query is resolved in its own checkpointed step, so a flow that resumes after
someone edits the seed link continues with the term it started with rather than
silently switching searches mid-run.

**Three ceilings bound one click**: at most 20 pages walked, at most 50 new
vacancies stored, and at most 50 vacancies handed to the GPU.

The third is not implied by the second, which cost an afternoon to learn.
Re-scraping an employer whose vacancies are all already stored adds nothing new,
so the storage cap never fires - yet the scrape was still queueing every vacancy
it saw for enrichment. Where most of those were untranslated, one click meant
roughly 95 minutes of GPU. `select_for_enrichment` now asks the database which
vacancies actually need work, takes at most 50, and reports the rest as
deferred. It also avoids enqueueing hundreds of workflows that would each start
only to discover there is nothing to do.

**A scrape adds at most 50 new vacancies, and walks at most 20 pages.** The
vacancy cap is the one that matters, because a *new* vacancy is what costs GPU
time - an already-known one is re-seen constantly across searches and costs
nothing, since its content hash is unchanged and enrichment skips it. The cap is
exact rather than "50 rounded up to a page": `store_page` takes the remaining
budget and stops partway through a page, leaving the rest for a later scrape,
which is safe because ingest is keyed on `(source, external_id)`.

Pages are therefore walked **sequentially** in a company scrape. Three parallel
pages could each add a full page of vacancies before any of them was counted;
page fetches are sub-second and capped anyway, so stopping on the right page is
worth more than the parallelism.

**A scrape walks at most 20 pages.** The board searches full text, so an
employer whose name contains a common word matches much of the site: "7
Solutions" returns 287 vacancies over 12 pages, almost none of them that
employer's. The cap bounds the time, bandwidth and GPU one click can cost, and
the result reports `pages_available` and `truncated` so the UI can say the
search was broader than we walked.

**Several employers can be scraped as one batch.** `POST
/api/companies/scrape-batch` takes a count plus the listing's own filters and
sort, and scrapes the top of that list - e.g. the first 10 last scraped before a
date. `scrape_companies_workflow` enqueues one `scrape_company_workflow` per
employer on the `company-scrape` queue (two at a time: one being graded, the
next being fetched) and waits for all of them. Each scrape publishes a
`progress` event and each vacancy's enrichment a `stage` event;
`GET /api/companies/scrape-batch/{id}` turns them into one line per employer,
such as "vacancy 5/23 - translating the description" or "waiting for the GPU"
when another employer's vacancy holds it. Cancelling the batch cancels every
child recursively. See ADR-0010.

**A running scrape can be cancelled** from the company drawer, which calls
`DBOS.cancel_workflow(id, cancel_children=True)`. Cancelling children is the
point: the expensive part is the enrichment workflows already on the `llm`
queue, and cancelling only the parent would leave them grinding through the GPU.
Measured on a real runaway: 287 of 298 children cancelled, the queue drained to
zero and the GPU went idle. Whatever was stored before the cancel stays stored,
because pages commit as they are fetched.

**An unreadable reply costs one vacancy's grade, not the vacancy.** About one
assessment in two hundred runs to the token ceiling - the model falls into a
repetition loop and the JSON ends unterminated. The finished fields are kept by
closing the truncated object (`fit_score` and `verdict` are written first, so
they usually survive). If even that fails, `assess_step` returns nothing and the
vacancy stays ungraded with status `assessment_unreadable`: the translation is
already banked, and the next run retries a sampled call that usually succeeds.
Failing the workflow instead would have thrown away the expensive half.

**A run stops when the model server goes away mid-flight.** The preflight only
covers the moment before work is queued. When a queued vacancy fails, the
workflow asks whether the server is still there - rather than guessing from an
exception type that does not survive the workflow boundary reliably - and if it
is gone, cancels everything still queued. Measured by killing Ollama during a
real scrape: it stopped 17 seconds later having cancelled 49 vacancies, with 4
failures instead of the 156 the same event produced before.

**Enrichment is preflighted.** Before any vacancy is queued for the GPU, one
step asks whether the model server is reachable and the configured model
installed. Without it, an Ollama that is simply not running became one failed
workflow per vacancy - each burning three retries with backoff - and a stack
trace, for what is a single fact. A scrape whose grading is skipped still keeps
everything it scraped and says why; the next run picks the vacancies up
untouched, because they are pending rather than failed.

## The GPU constraint

There is one GPU. All model work is enqueued on the `llm` queue, registered with
`worker_concurrency=1` **and** `global_concurrency=1`. The second limit is
enforced in the system database, so a second app process or a stray CLI run
still cannot put two jobs on the card - a single Horizon-style worker would only
bind one process. Verified by enqueueing six jobs at once and observing peak
concurrency 1.

Page fetching is network-bound and runs on a separate `job-ingest` queue with
`worker_concurrency=3`, so scraping is parallel while inference stays serial.
**The queue pauses when the GPU runs hot.** Before each vacancy's first model
call, `enrich_job_workflow` reads the temperature with `nvidia-smi`; above
`GPU_MAX_TEMP_C` (80°C) it `DBOS.sleep`s for `GPU_COOLDOWN_SECONDS` (3 min),
re-checks, and gives up after `GPU_MAX_COOLDOWNS` (5) pauses. It holds the one
`llm` slot while it waits, so nothing else touches the card. An unreadable
sensor never pauses anything. See ADR-0011.

Batch company scrapes run on their own `company-scrape` queue
(`worker_concurrency=2`), so employers waiting on the GPU never hold the
page-fetch slots.

**Clearing a wedged queue.** Bumping the version has a sharp edge: DBOS only
recovers workflows belonging to the *current* version, so a job left PENDING by
the old one is never resumed - while still counting against
`global_concurrency`. On a queue that allows a single job, one orphan wedges it
permanently and nothing runs again. `python scripts/enrich_jobs.py
--clear-stuck` cancels workflows stranded by another version. Observed: one
`enrich_job_workflow` PENDING at 0.1.0 held the only slot while 91 jobs waited.

**Bump `APPLICATION_VERSION` (`app/workflows/enrichment.py`) when a workflow's
step sequence changes.** It is one constant shared by `main.py` and
`scripts/enrich_jobs.py`; a guard test fails if either hardcodes its own. DBOS
replays a recovered workflow against the current code and aborts with
`DBOSUnexpectedStepError` if the recorded steps no longer line up. Inserting a
step into the enrichment sequence while a scrape was mid-flight killed that
scrape on the next restart. A new version leaves in-flight workflows to the old
one rather than replaying them against code they were not started with.

`register_queues()` must be called **after** `DBOS.launch()`: it writes the
limits to the system database and raises if called earlier.

## Request and data flows

**Company ingest** (operational, manual):

```
companies.json -> CompanyCreate validation -> bulk_upsert (ON CONFLICT name)
               -> companies table -> sequence resync
```

Idempotent: re-running updates in place instead of duplicating.

**Job search** (durable workflow):

```
POST /jobs/search -> job_search_workflow
                       |- fetch_jobs        (step, retried 3x, checkpointed)
                       |- log_jobs          (step, parses via JobSearchResponse)
                       |- store_jobs        (step, maps + upserts into 8 tables)
                     -> logs/dev.log + PostgreSQL
```

Idempotent on `(source, external_id)`: re-running the same search updates the
existing vacancies instead of duplicating them. Pass `{"store": false}` to log
without persisting.

**Full pipeline** (scrape, then translate and grade):

```
POST /jobs/pipeline -> scrape_and_enrich_workflow
    |- fetch page 1, learn the page count
    |- pages 2..n on the job-ingest queue      (parallel, concurrency 3)
    '- enrich_pending_workflow
         '- one enrich_job_workflow per vacancy on the llm queue  (SERIAL, 1)
              |- prepare_text   sanitise, hash, detect language   (no GPU)
              |- translate_step chunked Dutch -> English          (GPU)
              |- assess_step    score against the profile as JSON (GPU)
              '- persist_enrichment                               (no GPU)
```

Skipping is the main optimisation: English text never reaches the model, and a
vacancy whose cleaned-text hash and prompt version are unchanged uses no GPU at
all.

Steps are the checkpoint unit: a crash after `fetch_jobs` resumes without
re-issuing the HTTP call.

**Full-query ingest** (every page of one search):

```
POST /jobs/ingest  -> job_ingest_workflow   (durable; one step per page)
scripts/ingest_jobs.py -> ingest_query()    (same flow, no DBOS)
                            |- fetch page 1 -> learn how many pages exist
                            |- for each page: map -> upsert jobs
                            |                         + company + location
                            -> IngestStats
```

The page count comes from the first response rather than being guessed, each
page commits in its own transaction, and a page that fails is recorded in
`failed_pages` while the walk continues - one bad page must not discard the
rest of the crawl. Re-running updates in place.

## Conventions

- **Transactions belong to the caller.** Repositories `flush()` but never
  `commit()`, so several calls compose into one atomic unit of work.
- **Get-or-create is done inside a SAVEPOINT.** Pages are ingested in parallel,
  so two of them can race to create the same company, location or vocabulary
  term. Losing that race rolls back only the savepoint and returns the row the
  winner committed, leaving the rest of the page's work intact. `store_page` is
  additionally retried, because PostgreSQL breaks a lock cycle between two
  concurrent pages by aborting one with a deadlock error.
- **Domain errors, not ORM errors.** Repositories raise `CompanyNotFound` /
  `DuplicateCompany`; callers never catch `IntegrityError`.
- **Schema changes only via Alembic.** `create_all` is used solely to build the
  test database. `tests/unit/test_migrations.py` fails if models and migrations
  drift.
- **DTOs are separate from models.** Storage can change without changing the
  public contract.
- **Raw JSON crosses workflow-step boundaries**, not pydantic models, so
  checkpointed values stay plain and portable.
- **Secrets come from the environment.** `.env` is git-ignored; `.env.example`
  documents the keys.
- **Model work is always queued, never called directly.** The GPU guarantee
  lives in the `llm` queue; scripts enqueue rather than invoke.
- **Prompts are versioned constants**, and the version is stored with every row
  the model produces.

## Testing strategy

| Suite | Command | Needs |
|---|---|---|
| Unit | `pytest tests/unit -q` | PostgreSQL (`nljobs_test`) |
| E2E | `pytest tests/test_app.py -q` | app on :8000 (auto-started) + Chromium |
| Everything | `pytest -q` | both |

Unit tests run against real PostgreSQL rather than SQLite because the repository
relies on `ON CONFLICT` and `ILIKE`. Each test runs inside a transaction that is
rolled back, so the suite leaves no residue and tests cannot leak into one
another (ADR-0004).

## Auditability

Every shell command run against this project by an automated agent is recorded
by `tools/cmdlog.py` to `logs/command_audit.log` (human) and
`logs/command_audit.jsonl` (machine), with a risk level from `none` to `severe`.
`python tools/cmdlog.py verify` flags any command logged below the risk its
heuristics assign. See ADR-0005.

## Known gaps

- No HTTP API for companies or vacancies yet - CRUD is reachable only from
  Python. The repositories are deliberately framework-free so routes are a thin
  addition.
- Ingest is triggered by hand (CLI or endpoint); there is no scheduled crawl.
- The board offers **no employer filter** - `filters=company:x` and similar are
  silently ignored, returning the unfiltered set. Only full-text search is
  available, so "vacancies at employer X" is not directly expressible; a text
  search for a brand returns vacancies that *mention* it, mostly posted by
  agencies.
- `companies.type`/`website` fill in only for employers that appear in a
  fetched vacancy; the 2901 seed rows stay empty until then.
- `province` holds cities and is not normalised.
- No authentication, and no CI pipeline.
- **The app and the CLI use different DBOS system databases.** With
  `DBOS_SYSTEM_DATABASE_URL` unset, DBOS keeps its state in a SQLite file named
  after the app: `main.py` ("dbos-app-starter") uses `dbos_app_starter.sqlite`,
  `enrich_jobs.py` ("job-hunt-enrich") uses `job_hunt_enrich.sqlite`. So
  `global_concurrency=1` does not span the two processes, and
  `enrich_jobs.py --clear-stuck` cannot see workflows stranded in the app's
  database. Setting `DBOS_SYSTEM_DATABASE_URL` for both (or giving them one
  name) would close this.
- `profiles/default.json` was seeded by inference from this project's stack, not
  confirmed with the user; every `fit_score` is only as good as that file.
- Assessment quality is bounded by a 4B model. Prompt constraints, JSON-mode
  decoding and field coercion contain it; a larger model is a settings change.
