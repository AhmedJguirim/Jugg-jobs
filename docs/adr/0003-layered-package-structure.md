# 0003 - Layered package structure with a repository layer

Status: Accepted - 2026-09-20

## Context

The project started as a single `main.py` plus loose root modules
(`dev_log.py`, `nvb_schema.py`). Adding persistence, ingestion, and an external
API client to that flat layout would have produced a file where HTTP routing,
SQL, and third-party parsing all mixed - hard to test and hard to change safely.

## Decision

Adopt a layered `app/` package with one-directional dependencies:

    main.py -> repositories / integrations -> schemas / db -> config

- `app/repositories/` owns all query construction for a table. It takes an
  explicit `Session`, calls `flush()` but never `commit()`, and raises domain
  errors (`CompanyNotFound`, `DuplicateCompany`) rather than leaking
  `IntegrityError`.
- `app/schemas/` holds pydantic DTOs, kept separate from ORM models.
- `app/integrations/<provider>/` holds an outbound client plus its response
  models, free of any DBOS import.
- `scripts/` and `tools/` sit outside the runtime path.

`dev_log.py` moved to `app/logging_setup.py`; `nvb_schema.py` moved to
`app/integrations/nvb/schema.py`.

## Consequences

- Transaction boundaries live with the caller, so several repository calls
  compose into one atomic unit of work.
- Callers depend on the repository interface, not on SQLAlchemy, so the ORM
  could be replaced behind it.
- Integration code is callable from a workflow, a script, or a test without
  starting DBOS - which is what makes it testable.
- More files and more indirection than a flat layout. Accepted deliberately:
  the cost is paid once, the benefit recurs on every change.
- An HTTP API for companies is now a thin routing layer over the repository.
