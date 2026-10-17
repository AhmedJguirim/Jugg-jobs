# 0017 - Removing links for good

Status: Accepted - 2026-10-17

## Context

Google results include links that can never be graded: a company's careers page
(`jobs.ashbyhq.com/philo?utm_source=...`) with no job id, a duplicate, a dead
page. Saving such a link and pressing Grade ends in "could not read the page",
and the only option was Retry, which fails the same way every time. The same
link also stays in the collected URLs, so it is offered again by "Process
existing listings".

## Decision

- **Two removals, both soft.** Nothing is deleted from the database.
  - A **listing that failed** (its page could not be read, or its last run ended
    in a problem) gets a **Remove link** button beside **Retry**. It sets the raw
    listing's `status` to `removed` and removes its collected link.
  - The form for existing listings gets **Remove this link** beside **Skip this
    link**. Skip only moves past a link for this visit; Remove takes it out of
    the queue permanently. It also clears the pasted HTML, which belonged to it.
- **A removed link stays as a row** (`manual_job_listing_urls.removed_at`). The
  unique `url` is what stops a link being added twice (ADR-0013), so deleting the
  row would let the next Google paste bring it back; keeping it means it does
  not. Every list and queue (Listing URLs, the next-link queue and its count)
  leaves removed links out.
- A removed listing is excluded from the waiting list by its `removed` status;
  the "waiting" rule is unchanged (NULL or `unparseable`).
- Healthy waiting listings show only **Grade**; Remove is offered for failures
  and is hidden while a run is in progress.

## Consequences

- Removal cannot be undone from the UI. It is one column
  (`removed_at = NULL`, or `status = NULL` on the listing) away in the database.
- The Listing URLs page has no Remove button of its own; links are removed from
  the processing pages where the failure is seen.
