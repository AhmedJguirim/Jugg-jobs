# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A job-hunting tool for the Dutch market. It scrapes vacancies from Nationale
Vacaturebank, translates the Dutch ones to English with a **local** LLM
(`qwen3.5:4b` via Ollama), and grades each one against a candidate profile.
DBOS provides durable workflows; PostgreSQL holds everything.

`exec.md` is the operator runbook (how to start and use it).
`docs/architecture.md` is the design overview, and `docs/adr/` records why each
decision was made — read the relevant ADR before changing a decision it covers.

## Commands

Use `.venv\Scripts\python.exe`, never a bare `python` — the latter is a
different interpreter without the project's packages.

```shell
:: tests
.venv\Scripts\python.exe -m pytest -q                       :: all (~2 min, starts the app itself)
.venv\Scripts\python.exe -m pytest -m "not e2e" -q          :: unit only, no browser
.venv\Scripts\python.exe -m pytest tests/unit/test_llm_service.py -q
.venv\Scripts\python.exe -m pytest "tests/unit/test_stack_classification.py::TestTierSelection::test_familiar_stack" -q

:: run the app (serves /board, /companies)
.venv\Scripts\python.exe main.py

:: pipeline
.venv\Scripts\python.exe scripts\ingest_jobs.py "Laravel" "Symfony"
.venv\Scripts\python.exe scripts\enrich_jobs.py --profile ahmed
.venv\Scripts\python.exe scripts\enrich_jobs.py --progress --profile ahmed
.venv\Scripts\python.exe scripts\enrich_jobs.py --clear-stuck     :: unwedge the llm queue

:: migrations
.venv\Scripts\python.exe -m alembic revision --autogenerate -m "what changed"
.venv\Scripts\python.exe -m alembic upgrade head
```

Tests need PostgreSQL (`nljobs_test`); they skip cleanly without it. Enrichment
needs Ollama running. There is no linter or formatter configured.

## Architecture

### Layering

Dependencies point one way only:

```
main.py                    FastAPI routes + DBOS wiring
app/api/                   read-only HTTP for the two pages
app/workflows/             DBOS workflows, steps, queue registration
app/services/              flows: order multiple repository/client calls
app/repositories/          ALL SQL lives here; no query construction elsewhere
app/llm/, app/text/, app/integrations/
app/schemas/, app/db/      DTOs, models, session
app/config.py              settings from .env
```

Repositories take an explicit `Session`, call `flush()` but **never `commit()`**
— transaction boundaries belong to the caller — and raise domain errors
(`CompanyNotFound`, `DuplicateJob`) rather than leaking `IntegrityError`.

### The GPU constraint — the central design fact

There is one GPU and one model. **Every LLM call goes through the `llm` DBOS
queue**, registered with `worker_concurrency=1` *and* `global_concurrency=1`.
`app/integrations/ollama/client.py` is the only code that occupies the GPU and
does not serialise itself; calling it outside a queued workflow breaks the
guarantee, which is why scripts enqueue rather than invoke.

Page fetching is network-bound and runs on a separate `job-ingest` queue.

Three caps bound what one click can cost (`app/workflows/enrichment.py`):
20 pages walked, 50 *new* vacancies stored, 50 vacancies sent to the GPU.

### Decide in code what a 4B model cannot be trusted to decide

This pattern recurs and is the main lesson of the codebase. The model is asked
only for judgement; anything checkable is computed:

- **Stack tier** (`service.classify_stack`) — a weighted keyword match, not the
  model's opinion. It repeatedly called a Python vacancy "unrelated" on a
  profile listing Python under `familiar`.
- **Eligibility tier** — derived from evidence. A permit claim counts only when
  the model quotes a sentence that *appears in the advert* **and** contains
  right-to-work language; it once "quoted" `"Laravel developer wanted for our
  Utrecht office."` as proof a permit was required.
- **Work authorisation** — a rule over the profile, not a model inference.

### Enrichment pipeline

```
prepare_text (no GPU)  ->  translate_step (GPU)  ->  assess_step (GPU)  ->  persist
```

Each vacancy is one queued child workflow, so a crash resumes rather than
repeating paid-for GPU work. Translation is persisted **before** grading: it is
the expensive half, and grading can fail on its own.

Nothing re-runs unnecessarily: `job_translations` and `job_assessments` carry
`source_hash` (of the *cleaned* text) and `prompt_version`. Unchanged text with
the same prompt version is skipped entirely. **Bump the prompt version in
`app/llm/prompts.py` when you change a prompt** — that is what marks existing
rows stale.

### Scoring model

`fit_score` rates the work only, banded by stack tier (core 75-95, proficient
55-75, familiar 30-55, unrelated 10-30, non-technical 0-10) and clamped into
that band. Eligibility is reported separately as a traffic light:
`blocked` / `relocation` / `remote_ok`. Never fold eligibility into the score —
that conflation was the original bug and ADR-0008 explains it.

Profiles live in `profiles/<name>.json`; `LLM_DEFAULT_PROFILE` picks one.
Assessments are keyed `(job_id, profile_name)`, so profiles never mix.

## Gotchas that will cost you time

- **`register_queues()` must be called AFTER `DBOS.launch()`** — it writes to
  the system database and raises otherwise.
- **Bump `application_version` in `main.py` when a workflow's step sequence
  changes.** DBOS replays recovered workflows against current code and aborts
  with `DBOSUnexpectedStepError` when recorded steps no longer line up.
- **A version bump can wedge the queue.** DBOS only recovers workflows of the
  current version, so a job left `PENDING` by the old one holds the single `llm`
  slot forever and nothing runs again. `enrich_jobs.py --clear-stuck` fixes it.
  Symptom: work pending, zero GPU activity, no errors.
- **Get-or-create runs inside a SAVEPOINT** (`app/repositories/job.py`). Pages
  ingest in parallel and race to create the same company/location/vocabulary
  term. Plain SELECT-then-INSERT deadlocks and violates unique constraints.
- **The unit-test fixture drops and recreates the test schema** each session,
  because `create_all` will not alter an existing table.
- **`store_page` writes through its own session** (`session_scope`), i.e. the
  application database. Tests that exercise it must monkeypatch
  `enrichment.session_scope` or they will pollute `nljobs`.
- **Descriptions are double-encoded HTML** (`&amp;eacute;`), so
  `app/text/sanitize.py` unescapes until stable. Never feed raw description
  text to the model.
- Thinking mode is off deliberately: it cost 4547 tokens vs 163 for the same
  translation. The model's own defaults (`temperature=1`,
  `presence_penalty=1.5`) are overridden on every call.

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
