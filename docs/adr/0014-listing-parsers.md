# 0014 - Listing parsers and the `jobs_test` table

Status: Accepted - 2026-10-17

## Context

Raw job listings (a link plus the page's HTML) are collected by hand. The next
step is turning one into a vacancy with the same fields the board uses, starting
with Ashby (`jobs.ashbyhq.com`). Postings get taken down, so a pasted page is
often Ashby's "Job not found" page. Parsers are new and will change, and the
`jobs` table feeds the board, so experiments must not write there.

## Decision

- **`jobs_test`** has the same columns, defaults, indexes and `(source,
  external_id)` key as `jobs` (`tests/unit/test_job_test_model.py` fails if they
  drift). It references the shared `companies` and `locations` tables. It does
  **not** copy the vocabulary and DCO-title tables hanging off `jobs`;
  categories, industries, education levels and contract types are not filled.
  Moving to `jobs` later is a copy of the row.
- **Platform comes from the URL host** (`app/parsers/__init__.py`,
  `detect_platform`), never from the page. One module per platform exposing
  `PLATFORM` and `parse(url, html)`, registered in one place.
- **Parsers are pure and return `JobCreate`**, the DTO the NVB ingest already
  produces, so storing a parsed job reuses the company/location resolution and
  nothing downstream knows which platform it came from. A parser returns
  `ParsedListing`, or `ListingNotFound`, or raises `ListingParseError`.
- **"Not found" is checked first** and is a result, not an error.
- **No model involved.** Everything is read from Ashby's stable `ashby-job-posting-*`
  classes and headings; the hashed CSS-module classes are not relied on, except
  the not-found marker as a second signal. HTML is read with a small stdlib tree
  (`app/text/dom.py`); no new dependency.
- **Workflow** `process_raw_listing_workflow(raw_listing_id)`: load, parse, store
  as three checkpointed steps, no queue (no GPU, no network). Storing is an
  upsert on `(source, external_id)`. Unusable pages (`not_found`, `unsupported`,
  `unparseable`) are returned as the result, not raised, so DBOS does not retry
  a deterministic outcome. Started by `POST
  /api/workspace/raw-job-listings/{id}/process` or `scripts/process_listing.py`.
  It did not change any existing workflow's steps, so `APPLICATION_VERSION` is
  unchanged.

### Ashby field mapping

| `jobs_test` | From |
|---|---|
| `source`, `external_id` | `ashbyhq`; the job UUID in the URL |
| `title`, `job_titles` | the heading |
| `description` | the description's HTML, inline styles and empty paragraphs removed |
| company, `company_website` | the header logo's `alt` and link |
| location | the postal "Address" only (`City, Region`; a region equal to the city is dropped) |
| `salary_min/max/type` | "Compensation", when present and readable |
| `detail_url`, `apply_url`, `apply_option` | URL, URL + `/application`, `external` |
| `status`, `origin` | `active` (the page rendered the posting), `manual` |
| `raw` | platform, org, logo URL, `raw_listing_id`, and every left-pane fact |

Fields the page does not carry (dates, contact person, career level, working
hours, applicants) stay empty; they are not inferred.

## Consequences

- Parsing a page creates `companies` rows (and a `locations` row when there is
  an address) in the shared tables, as NVB ingest does.
- "Location" ("EU Remote") is free text and is kept in `raw`, not made a city.
  Employment type, location type and department have no column; they are in
  `raw["sections"]`.
- The company slug is not set: `companies.slug` is unique and holds the NVB
  slug, and the Ashby org name is not that.
- Ashby's markup can change; the parser then raises `ListingParseError` and the
  result says why, instead of storing a half-empty job.
