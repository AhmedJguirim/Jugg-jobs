# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A job search tool. It collects links to postings on Ashby, Lever, Greenhouse,
Workable and Breezy HR (pasted Google results), fetches each posting, and grades
it against a candidate profile with a **local** LLM (`qwen3.5:4b` via Ollama).
DBOS provides durable workflows; PostgreSQL holds everything.

`README.md` is setup and usage, `docs/operations.md` the runbook,
`docs/architecture.md` the design overview, and `docs/adr/` records why each
decision was made - read the relevant ADR before changing a decision it covers.

## Commands

On Windows use `.venv\Scripts\python.exe`, never a bare `python` - that may be a
different interpreter without the project's packages.

```shell
:: tests
.venv\Scripts\python.exe -m pytest tests/unit -q            :: unit only, no browser
.venv\Scripts\python.exe -m pytest -q                       :: all (starts the app if it is not running)
.venv\Scripts\python.exe -m pytest tests/unit/test_global_grader.py -q
.venv\Scripts\python.exe -m pytest "tests/unit/test_stack_classification.py::TestTierSelection::test_familiar_stack" -q

:: run the app (http://localhost:8000)
.venv\Scripts\python.exe main.py

:: grade stored raw listings from the command line
.venv\Scripts\python.exe scripts\process_listing.py --all --profile default
:: unwedge the llm queue (app stopped)
.venv\Scripts\python.exe scripts\clear_stuck.py

:: migrations
.venv\Scripts\python.exe -m alembic revision --autogenerate -m "what changed"
.venv\Scripts\python.exe -m alembic upgrade head
```

Unit tests need PostgreSQL (`job_hunt_test`); they skip cleanly without it. No
unit test makes a network request or calls the model. Grading needs Ollama
running. There is no linter or formatter configured.

## Architecture

### Layering

Dependencies point one way only:

```
main.py                    page routes + DBOS launch
app/api/                   HTTP for the pages (workspace, board)
app/workflows/             DBOS workflows, steps, queue registration
app/services/              flows: order multiple repository/client calls
app/repositories/          ALL SQL lives here; no query construction elsewhere
app/llm/, app/text/, app/integrations/, app/parsers/   (parsers are pure: html in, JobCreate out)
app/schemas/, app/db/      DTOs, models, session
app/config.py              settings from .env
```

Repositories take an explicit `Session`, call `flush()` but **never `commit()`**
- transaction boundaries belong to the caller - and raise domain errors
(`JobNotFound`, `RawJobListingNotFound`) rather than leaking `IntegrityError`.

### The flow

```
paste Google results -> manual_job_listing_urls      (/workspace/urls)
  "Process all"      -> raw_job_listings             listing-fetch queue, 1 at a time
  Grade / Bulk grade -> parse -> prepare -> grade -> persist
                                            (llm queue)  -> jobs + job_assessments
```

Fetching never grades, and saving HTML never grades: grading is on demand, one
listing or a batch of up to 100 (ADR-0016, ADR-0024). Nothing reaches `jobs`
until it has a grade (ADR-0015).

### The GPU constraint - the central design fact

There is one GPU and one model. **Every LLM call goes through the `llm` DBOS
queue** (`app/workflows/llm_queue.py`), registered with `worker_concurrency=1`
*and* `global_concurrency=1`. `app/integrations/ollama/client.py` is the only code
that occupies the GPU and does not serialise itself; calling it outside a queued
workflow breaks the guarantee, which is why scripts enqueue rather than invoke.

Fetching runs on `listing-fetch` (one at a time, 1-3 s random pause between
requests) and grading batches on `listing-grade` (two at a time). They report
progress through DBOS workflow events (`progress`, `stage`).

Before each grade, `grade_listing_workflow` checks the temperature
(`nvidia-smi`) and sleeps 3 min while it is above 80°C (`GPU_*` settings,
ADR-0011). Without `nvidia-smi` it never pauses.

### Decide in code what a 4B model cannot be trusted to decide

This pattern recurs and is the main lesson of the codebase. The model is asked
only for judgement; anything checkable is computed (`app/llm/scoring.py`,
`app/llm/global_grader.py`):

- **Stack tier** (`scoring.classify_stack`) - a weighted keyword match, not the
  model's opinion. It repeatedly called a Python posting "unrelated" on a
  profile listing Python under `familiar`.
- **Restrictions** (a language, a country, a permit, a nationality) count only
  when the model quotes a sentence that *appears in the advert* **and** says what
  the claim needs. It once "quoted" a real sentence that said nothing about
  permits as proof a permit was required.
- **Whether a restriction bars this candidate** - a lookup against the profile
  (languages, `based_in`, `citizenship`, `work_authorization`), not a model
  inference.

### Scoring model

`fit_score` rates the work only, banded by stack tier (core 75-95, proficient
55-75, familiar 30-55, unrelated 10-30, non-technical 0-10) and clamped into
that band. Eligibility is reported separately as a traffic light:
`blocked` / `relocation` / `remote_ok`. Never fold eligibility into the score -
that conflation was the original bug and ADR-0008 explains it.

Nothing is graded twice unnecessarily: `job_assessments` carries `source_hash`
(of the *cleaned* text) and `prompt_version`. **Bump
`GLOBAL_ASSESSMENT_PROMPT_VERSION` in `app/llm/global_prompts.py` when you change
a prompt** - that is what marks existing grades stale.

Profiles live in `profiles/<name>.json`; `LLM_DEFAULT_PROFILE` picks one. Only
`profiles/default.json` (an example) is tracked; real ones are git-ignored.
Assessments are keyed `(job_id, profile_name)`, so profiles never mix.

### Adding a platform

A parser in `app/parsers/<platform>.py` (pure: url + HTML -> `ParsedListing` /
`ListingNotFound`, raising `ListingParseError` for markup it does not know), its
host in `PLATFORM_HOSTS`, the element to keep in
`app/integrations/listing_pages/extract.py`, and real pages under
`tests/fixtures/<platform>/` with tests that parse them.

## Gotchas that will cost you time

- **`register_queues()` must be called AFTER `DBOS.launch()`** - it writes to
  the system database and raises otherwise.
- **Bump `APPLICATION_VERSION` in `app/workflows/llm_queue.py` when a
  workflow's step sequence changes** (a `DBOS.set_event` counts as a step).
  `main.py` and `scripts/process_listing.py` both read it. DBOS replays recovered
  workflows against current code and aborts with `DBOSUnexpectedStepError`
  when recorded steps no longer line up. Renaming a workflow or step function
  changes the sequence too.
- **A version bump can wedge the queue.** DBOS only recovers workflows of the
  current version, so a job left `PENDING` by the old one holds the single `llm`
  slot forever and nothing runs again. `scripts/clear_stuck.py` fixes it - with
  the app stopped. Symptom: work pending, zero GPU activity, no errors.
- **The DBOS app name is still `dbos-app-starter`** (main.py, clear_stuck.py).
  With `DBOS_SYSTEM_DATABASE_URL` unset, DBOS keeps its state in a SQLite file
  named after it; renaming it orphans every workflow in flight.
- **Get-or-create runs inside a SAVEPOINT** (`app/repositories/job.py`).
  Workers race to create the same company/location; plain SELECT-then-INSERT
  deadlocks and violates unique constraints.
- **The unit-test fixture drops and recreates the test schema** each session,
  because `create_all` will not alter an existing table.
- **Services that open their own session** (`session_scope`) write to the
  application database. Their tests pass `open_session=` or monkeypatch it, or
  they pollute the real data.
- **Descriptions can be double-encoded HTML** (`&amp;eacute;`), so
  `app/text/sanitize.py` unescapes until stable. Never feed raw description
  text to the model.
- Thinking mode is off deliberately: it cost 4547 tokens vs 163 for the same
  output. The model's own defaults (`temperature=1`, `presence_penalty=1.5`)
  are overridden on every call.
- Some names are history from the retired Dutch pipeline (ADR-0025): the
  `job_translations` table (it holds the cleaned text a grade was made from),
  the `dutch_required` column, `jobs_test` / `job_listings_test` (kept, unused).
  Leave them unless a migration is worth it.

## Conventions

- Shell commands run against this project are recorded with a risk level:
  `.venv\Scripts\python.exe tools\cmdlog.py run --risk <none|low|medium|high|severe> --note "why" -- <command>`.
  `cmdlog verify` flags anything logged below its heuristic risk. Keep
  credentials out of the logged text (use `PGPASSWORD`).
- Schema changes only through Alembic. `tests/unit/test_migrations.py` fails if
  models and migrations drift.
- Update `docs/architecture.md` in the same change that alters what it
  describes; add a numbered ADR for a real decision rather than editing an
  accepted one.
