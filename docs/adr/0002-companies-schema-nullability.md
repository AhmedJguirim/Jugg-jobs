# 0002 - Companies schema: nullability and uniqueness

Status: Accepted - 2026-09-20

## Context

The requested `companies` schema was: `id, name, type, nullable website,
nullable slug, province, nullable google_jobs_link, nullable linkedin_jobs_link,
nullable indeed_jobs_link, nullable glassdoor_jobs_link, nullable
nationalevacaturebank_jobs_link, created_at, updated_at`. Reading that
literally, `type` and `province` are NOT NULL.

Inspecting the seed file `companies.json` (2901 records) before building the
schema showed:

- **`type` does not exist in the source at all** - not null, absent. Same for
  `website` and `slug`.
- **`province` is null on 45 of 2901 records** (1.6%).
- `province` contains **cities**, not provinces: "Amsterdam" (1615),
  "Eindhoven" (208), "Delft" (75) - 166 distinct values, many with trailing
  whitespace ("Amsterdam ").
- `id` is dense and unique, 1..2901. `name` is unique across all 2901 records.

A NOT NULL `type` would have rejected 100% of the seed data; a NOT NULL
`province` would have rejected 45 rows.

## Decision

1. **`type` and `province` are nullable**, departing from a literal reading of
   the spec. The alternative - inventing a placeholder such as `'unknown'` -
   would fabricate data that was never in the source and would be
   indistinguishable from a real value later.
2. **`name` is UNIQUE**, and is the conflict target for ingest upserts. This is
   what makes re-running the ingest idempotent rather than duplicating 2901
   rows. It is justified by the data: all 2901 names are distinct.
3. **`slug` is UNIQUE** (nullable; PostgreSQL permits many NULLs under a unique
   constraint).
4. **`province` is indexed** to support "employers in region X" reads.
5. **Job-board links are `TEXT`**, not `VARCHAR(n)`: they carry encoded query
   strings with no meaningful length ceiling.
6. **Whitespace is trimmed and empty strings become NULL** at the DTO boundary,
   so "Amsterdam " and "Amsterdam" do not become two distinct values.
7. **Timestamps are database-owned** (`server_default now()` / `onupdate`), so
   they stay correct for writes that bypass the ORM. The `created_at` /
   `updated_at` values in the source file are deliberately **not** imported -
   they describe rows in the *previous* system, not in this one.

## Consequences

- The ingest loads all 2901 records with zero rejects.
- `type` and `website` are NULL for every row until a source provides them; any
  consumer must handle that.
- `province` holding city names is a **known, unresolved data-quality issue**.
  It was not silently "fixed" because mapping cities to the 12 Dutch provinces
  is a lossy transformation that should be an explicit, reviewed decision.
- Adding NOT NULL to `type` later requires a backfill plus a migration.
- Renaming a company to an existing name now fails loudly with
  `DuplicateCompany` - intended, but callers must handle it.
