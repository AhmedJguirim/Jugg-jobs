# 0006 - Vacancy schema: what gets its own table

Status: Accepted - 2026-09-23

## Context

A vacancy from the Nationale Vacaturebank arrives as a single nested document
with ~39 fields, several of them lists or sub-objects. Storing it as one flat
row would make filtering ("all HBO jobs in Utrecht") a string scan, while
normalising everything would produce a dozen tables for values that are never
reused.

Before deciding, 200 live vacancies across 8 unrelated queries were sampled:

| Field | Distinct values in 200 vacancies | Shape |
|---|---|---|
| `categories` | 16 | closed vocabulary, 1 value per job (2 in 1 case) |
| `industries` | 30 | closed vocabulary, 1 value per job |
| `educationLevels` | 5 | closed vocabulary (MBO/HBO/WO/VMBO/Lagere school) |
| `contractTypes` | 5 | closed vocabulary (Vast/Tijdelijk/Freelance/Stage/…) |
| `jobTitles` | 142 | open free text |
| `workLocation` | 146 companies, cities repeat heavily | shared entity |
| `dcoTitles` | 3 ranked entries per job, each scored | structured list |
| `salary`, `workingHours`, `apply`, `contact` | one per vacancy | 1:1 |

## Decision

Three treatments, chosen by the shape of the data rather than uniformly:

1. **Lookup + association tables** for the four closed vocabularies
   (`categories`, `industries`, `education_levels`, `contract_types`). Small,
   reused, and the thing users filter by. Each association carries
   `is_primary`, which records the value the API reports in its *singular*
   field (`educationLevel` vs `educationLevels`) without duplicating it into a
   second column.
2. **Its own table** for `locations` (places repeat across vacancies, so
   storing each once makes city/province filtering an indexed join) and for
   `job_dco_titles` (a ranked list whose entries carry their own score, rank
   and generic bucket - not expressible as an array).
3. **Columns on `jobs`** for everything genuinely 1:1 - salary band, working
   hours, apply details, contact person, street and postcode - plus an
   **array column** for `job_titles`, which is open free text rather than a
   controlled vocabulary.

Supporting decisions:

- **`(source, external_id)` is the natural key**, with a surrogate integer `id`.
  This makes re-ingesting a search result idempotent (a board re-lists the same
  vacancy on every query) and leaves room for other job boards later.
- **`raw` JSONB** keeps the untouched source payload, so a new field can be
  back-filled from stored data instead of re-fetching the API.
- **`dco_title`/`dco_title_slug` are mirrored onto `jobs`** from the rank-0
  classifier entry, so the common filter needs no join. The full ranked list
  stays in `job_dco_titles`.
- **Address detail stays on the vacancy.** Two jobs in Amsterdam share a city,
  not a street, so `street`/`zip_code` are columns on `jobs` while the place is
  a foreign key.
- **Vocabulary terms are created on first sight.** A new category appearing
  upstream must not fail an ingest.
- **A slug collision between two employers drops the slug** rather than failing
  the batch: `companies.slug` is unique, the source can repeat one, and we do
  not depend on it.

## Consequences

- Filtering by city, province, category, industry, education level, contract
  type, career level, salary floor and posting date is indexed and composable.
- `list_vocabulary()` answers "what categories exist?" from a 16-row table
  rather than scanning every vacancy.
- Vacancies fill in `companies.slug`, `type` and `website` - the columns the
  seed file left empty (ADR-0002) - enriching existing rows without
  overwriting what the seed file already provided.
- Writing a vacancy touches up to eight tables. That fan-out is contained in
  `app/repositories/job.py`; callers pass plain values and never see a foreign
  key.
- Association rows must be written through the ORM relationship, not Core
  statements, or SQLAlchemy's bookkeeping goes stale (`StaleDataError`).
  `is_primary` is stamped by a follow-up UPDATE because a plain `secondary`
  relationship cannot carry it.
- `raw` roughly doubles the storage per vacancy. Accepted while the dataset is
  small and the mapping is still settling; it can be dropped once stable.
- The vacancy logo (`logos`) is deliberately **not** modelled or stored.
