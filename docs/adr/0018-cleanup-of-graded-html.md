# 0018 - Cleaning up the stored HTML of graded listings

Status: Accepted - 2026-10-17

## Context

Every saved listing keeps the full HTML of its page (about 14 KB each, ADR-0012:
"stored untouched, so later steps can be re-run"). Once a listing is graded, the
job, its cleaned description and the page's facts live in `jobs` (and
`jobs.raw`), so the saved page is rarely needed again. Measured when this was
added: 130 graded listings held 1.8 MB of HTML in a 117 MB database whose `jobs`
table alone is 60 MB - not a problem yet, but it grows with every listing.

## Decision

- A button, **Cleanup graded raw listings**, on the raw-listing page empties
  `html_body` of every listing whose status is `graded`, after a confirmation
  that says what is lost. The line next to it shows how many listings and how
  much HTML it would clean.
- **Only the page body goes.** The row stays with its url, status and `job_id`:
  the url is what stops a link being processed twice, and the job link is how a
  listing is traced to its job. The column keeps its NOT NULL constraint (the
  body becomes the empty string).
- **Only graded listings.** Waiting, unreadable, removed and "job not found"
  listings keep their HTML: for the first two it is the input the next attempt
  needs.
- **A cleaned listing cannot be damaged later.** Processing one (the script's
  `--all`, an old workflow id) finds no HTML. That returns its own status,
  `no_html`, which nothing records, instead of "unparseable" - which would have
  put a graded listing back on the waiting list.

## Consequences

- It cannot be undone, and a cleaned listing cannot be re-parsed or re-graded
  from its stored page; to re-grade, paste the page again (the job is an upsert
  on its platform id, so it updates the same job).
- Emptying the column frees the space for reuse inside the table, but PostgreSQL
  only gives disk back to the operating system after `VACUUM FULL`; the database
  file does not shrink on its own.
- Because the unique link and the row stay, the Listing URLs page still shows
  these links as processed.
