# 0004 - Test against real PostgreSQL with rollback isolation

Status: Accepted - 2026-09-20

## Context

The repository layer uses PostgreSQL-specific SQL: `INSERT ... ON CONFLICT DO
UPDATE` for idempotent ingest and `ILIKE` for case-insensitive search. Unit
tests could run against in-memory SQLite (fast, zero setup) or a real
PostgreSQL database.

SQLite does not implement `ON CONFLICT DO UPDATE` with the same semantics and
has no `ILIKE`. Tests passing on SQLite would therefore prove little about the
code that actually ships - the upsert path, the single riskiest part of the
ingest, would be untested or tested against different behaviour.

## Decision

Unit tests run against a dedicated `nljobs_test` PostgreSQL database.

Isolation is per-test via transaction rollback: the fixture opens a connection,
begins a transaction, binds the `Session` to it, and rolls back on teardown.
Nothing is ever committed. A `SAVEPOINT` (`begin_nested`) is opened as well,
because the repository calls `session.rollback()` when it traps an
`IntegrityError`; without the savepoint that rollback would discard the outer
test transaction.

The test schema is built with `Base.metadata.create_all`, while migration
correctness is asserted separately by comparing the migrated application
database against the models (`test_migrations.py`).

If PostgreSQL is unreachable the database tests `skip` rather than fail, so the
pure-logic tests still run anywhere.

## Consequences

- Tests exercise the same SQL dialect that production uses.
- The suite is self-cleaning: no fixture teardown ordering, no leaked rows, and
  tests are order-independent.
- Fast enough in practice - 84 tests in under a second - because no data is
  ever committed.
- Developers need a local PostgreSQL. Accepted: the app requires one anyway.
- The schema used by tests comes from the models, so a migration that diverges
  from the models would not be caught here; `test_migrations.py` exists
  precisely to close that gap.
