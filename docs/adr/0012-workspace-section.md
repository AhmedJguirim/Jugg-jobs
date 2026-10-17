# 0012 - A second section: the workspace

Status: Accepted - 2026-10-16

## Context

Everything so far is about scraped vacancies: ingest, translate, grade, browse.
The user wants a separate area to work on listings they find by hand, built up
step by step. The first step is only to capture one: its link and the page's
HTML.

## Decision

- **Two sections, one app.** *Jobs* is the existing board, employers and
  workflows pages. *Workspace* is new. A switch at the top of every page moves
  between them: left is Jobs, right is Workspace. Each section has its own
  navigation next to the switch (`html/_topbar.html`).
- **One shared top bar, injected by the server.** The pages are static HTML with
  no build step, so `main.py:render_page` replaces a `<!--TOPBAR-->` marker with
  the partial. The partial is plain CSS/JS, not Tailwind, because
  `html/app.html` does not load Tailwind. A new page in either section needs the
  marker after `<body>` and an entry in the partial's `sections` map.
- **`raw_job_listings`** holds `url` and `html_body` (both `Text`) plus the usual
  timestamps. The body is stored exactly as pasted - no sanitising, no
  trimming - so later steps can be re-run from the original. Only the link is
  trimmed. Blank values are rejected with a 422.
- The listing endpoint omits bodies; one row is a whole page of HTML.
- Nothing is parsed, deduplicated or sent to the model yet. The same link can be
  submitted twice, deliberately: a re-paste is a new capture of the page.

## Consequences

- The switch's destinations are hard-coded to `/board` and `/workspace`.
- There is no edit or delete for a stored listing yet.
- The e2e suite does not submit the form (it would write to the application
  database); the write path is tested in `tests/unit/test_raw_job_listing.py`.
