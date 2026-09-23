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
| `app/schemas/` | `CompanyCreate/Update/Read/Page` DTOs + normalisation |
| `app/repositories/` | CRUD; raises domain errors, never commits |
| `app/integrations/nvb/` | NVB HTTP client (`client.py`) and models (`schema.py`) |
| `app/logging_setup.py` | File logger for development (`logs/dev.log`) |
| `scripts/ingest_companies.py` | Loads `companies.json` into `companies` |
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
                     -> logs/dev.log
```

Steps are the checkpoint unit: a crash after `fetch_jobs` resumes without
re-issuing the HTTP call.

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

- No HTTP API for companies yet - CRUD is reachable only from Python. The
  repository is deliberately framework-free so routes are a thin addition.
- `vacancies` are fetched and logged but never persisted; no `jobs` table yet.
- `type`/`website` are empty for every company until a source supplies them.
- `province` holds cities and is not normalised.
- No authentication, and no CI pipeline.
