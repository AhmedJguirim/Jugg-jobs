# 0025 - Worldwide postings only: the Dutch pipeline is removed

Status: Accepted - 2026-11-07

## Context

The project started as a tool for the Dutch market: a seed list of about 2,900
employers (`companies.json`), a Nationale Vacaturebank client, and a pipeline that
translated each Dutch vacancy to English before grading it (ADR-0006, 0007, 0010).

Since ADR-0015 the useful half has been the other one: postings on Ashby, Lever,
Greenhouse, Workable and Breezy HR, collected from Google results, fetched
(ADR-0024) and graded by a grader built for postings from any country. The project
is being published, and the Dutch half cannot go with it in working order: it
depends on a scraped employer dataset that should not be redistributed. Shipped
without that data, it is a set of pages and scripts that do nothing.

The repository also carried development scaffolding that a user has no reason to
see: the `/jobs-test` page (parser output checked before a parser was hooked in,
ADR-0014), the experimental `scripts/fetch_listings.py` (ADR-0023), and the DBOS
starter demo the app was generated from (`/`, `/workflow`, `/queue`, `/comm`).

## Decision

- **Removed:** the NVB client and mapper, the company seed file and its loader,
  the Companies page and its API, the company scrape and batch scrape workflows,
  the translation step and the Dutch assessment prompt and grader
  (`service.assess_job`, `prompts.py`), and `scripts/ingest_jobs.py`,
  `ingest_companies.py`, `enrich_jobs.py`.
- **Removed:** the `/jobs-test` page and API, `scripts/fetch_listings.py`, the
  code that wrote `jobs_test` and `job_listings_test`, and the DBOS demo.
- **Kept, unchanged:** every table, including `companies` (jobs still link to
  their employer), `job_translations` (it holds the cleaned text a grade was made
  from), `jobs_test` and `job_listings_test`. No migration drops anything, so an
  existing database keeps all its rows and the schema history stays intact.
- **Moved, not changed:** what the worldwide flow shared with the Dutch one. The
  `llm` queue, the GPU cooldown and the model check are now
  `app/workflows/llm_queue.py`; the scoring rules decided in code (stack tier,
  permit evidence, work authorisation, JSON salvage) are `app/llm/scoring.py`.
  Workflow and step names are the same, so `APPLICATION_VERSION` stays 0.5.0 and
  work in flight under the old layout is still recovered.
- **One navigation** replaces the Jobs / Workspace switch: Listing URLs, Raw
  listings, Job board, in the order they are used. `/` opens the board.
- `scripts/clear_stuck.py` replaces `enrich_jobs.py --clear-stuck`, and now
  opens the app's own DBOS state, which the old one could not see.

## Consequences

- Dutch vacancies already in a database still show on the board with their
  grades, but nothing adds new ones. The code is in the repository's history for
  anyone who has the data.
- ADR-0002 and ADR-0010 describe code that no longer exists; the translation half
  of ADR-0007 likewise. They stay as the record of why the schema looks the way it
  does.
- A new parser can no longer be checked on `/jobs-test` before it is hooked in.
  Its fixtures and unit tests do that job now.
- Some names still carry the history: the `job_translations` table, the
  `dutch_required` column, `source` defaulting to `nvb`. Renaming them would take
  a migration for no change in behaviour.
