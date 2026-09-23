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
  app/services/              flows: order multiple repository/client calls
  app/repositories/          all SQL for a table; returns ORM objects
  app/integrations/          outbound HTTP clients + their response models
      |
      v
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
| `app/services/` | Flow layer: multi-step operations (paginating job ingest) |
| `app/integrations/nvb/` | NVB client (`client.py`), models (`schema.py`), mapper (`mapper.py`) |
| `app/logging_setup.py` | File logger for development (`logs/dev.log`) |
| `scripts/ingest_companies.py` | Loads `companies.json` into `companies` |
| `scripts/ingest_jobs.py` | Ingests every result page for one or more queries |
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
