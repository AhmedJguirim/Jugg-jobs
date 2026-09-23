# 0001 - PostgreSQL with SQLAlchemy and Alembic

Status: Accepted - 2026-09-20

## Context

The app needs durable storage for employers and, later, vacancies. A PostgreSQL
14 instance already runs locally, and the existing DBOS starter used SQLite,
which DBOS itself flags as development-only. The seed dataset is small (2901
rows) but the job data that follows will not be.

## Decision

Use PostgreSQL as the datastore, SQLAlchemy 2.x (typed `Mapped[]` declarative
style) as the ORM, `psycopg` 3 as the driver, and Alembic for migrations.

The schema changes **only** through an Alembic revision. `Base.metadata.create_all`
is permitted solely to build the test database.

## Consequences

- Access to PostgreSQL-specific features the workload wants: `INSERT ... ON
  CONFLICT` for idempotent ingest, `ILIKE` for case-insensitive search, and
  `timestamptz`.
- Every environment needs a PostgreSQL server; tests cannot fall back to an
  in-memory SQLite (see ADR-0004).
- Migrations are reviewable artefacts, and `tests/unit/test_migrations.py`
  fails the build if the models and the migrated schema drift apart.
- The DBOS system database stays on SQLite for now; that is DBOS's own state,
  not application data, and is a separate concern to migrate later.
