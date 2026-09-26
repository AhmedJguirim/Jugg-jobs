# Architecture Decision Records

One file per decision, numbered in order. An ADR is immutable once accepted:
to change a decision, add a new ADR that supersedes it rather than editing the
old one, so the reasoning trail survives.

Format: Context (the forces at play) / Decision (what we chose) /
Consequences (what this costs us, including what it rules out).

| # | Title | Status |
|---|---|---|
| [0001](0001-postgresql-sqlalchemy-alembic.md) | PostgreSQL with SQLAlchemy and Alembic | Accepted |
| [0002](0002-companies-schema-nullability.md) | Companies schema: nullability and uniqueness | Accepted |
| [0003](0003-layered-package-structure.md) | Layered package structure with a repository layer | Accepted |
| [0004](0004-testing-against-real-postgres.md) | Test against real PostgreSQL with rollback isolation | Accepted |
| [0005](0005-command-audit-log.md) | Append-only command audit log for agent work | Accepted |
| [0006](0006-job-schema-normalisation.md) | Vacancy schema: what gets its own table | Accepted |
| [0007](0007-local-llm-enrichment.md) | Local LLM enrichment on a single GPU | Accepted |
