# 0013 - Manual job listing URLs from Google results

Status: Accepted - 2026-10-17

## Context

Vacancies are also found by hand with Google queries such as
`site:jobs.ashbyhq.com ("PHP" AND "Remote")`. The user copies the
`<div id="search">` element of the results page and wants the links in a table,
each link once.

## Decision

- **Table `manual_job_listing_urls`:** `url` (text, **unique**), `scraped_at`
  (nullable - empty until something fetches the page; nothing does yet) plus the
  usual timestamps.
- **Parsing is deterministic and stdlib-only** (`app/text/search_results.py`, no
  new dependency, no model). A result is an `<a href>` that wraps the result's
  `<h3>` title, inside `id="search"`. That excludes the "Read more" deep links,
  `ping` tracking URLs, icons and anything outside the block. With no
  `id="search"` in the paste, the whole text is scanned instead.
- **The URL fragment is dropped** (`#:~:text=...` is Google's highlight anchor);
  the query string is kept, since tracking parameters are part of the link as
  Google returned it.
- **Uniqueness is enforced by the database**: `INSERT ... ON CONFLICT DO NOTHING
  RETURNING url`, so the response can say exactly which links were new and a
  concurrent paste cannot create a duplicate. Duplicates within one paste
  collapse first.

## Consequences

- Two links to the same posting that differ only in query string are two rows.
- If Google changes the result markup (the `<a>` around `<h3>`), the parser
  finds nothing and the endpoint answers 422 rather than storing garbage.
