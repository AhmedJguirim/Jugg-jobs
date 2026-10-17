# 0019 - A Greenhouse parser, validated before it is hooked in

Status: Accepted - 2026-10-17

## Context

Greenhouse links (`job-boards.greenhouse.io/<board>/jobs/<id>`) were being saved
as raw listings before any parser existed, so they were stored as `unsupported`
(ADR-0016). The user asked for a parser built from the latest three, checked in
`jobs_test`, and only then connected to the real `jobs` table (ADR-0014/0015).

## Decision

- **`app/parsers/greenhouse.py`**, same contract as the Ashby parser
  (`parse(url, html)` -> `ParsedListing`, `ListingNotFound` or `ListingParseError`).
  Greenhouse's copied job content uses plain classes (`job__title`,
  `job__location`, `job__description`); the fragment has no header, so the company
  is read from the job-alert box ("Interested in building your career at X?").
- **Pay is read only from a sentence about pay** and never from one about equity,
  bonus, stock, stipend or commission. One of the three sampled pages has
  "New hire equity: $24,000 - $36,000" next to a real base pay grade in zloty; the
  first must not become a salary, the second must. The currency is kept in
  `raw["compensation"]` (a written code like CAD/PLN wins; otherwise a symbol:
  `zl` PLN, euro, pound, and a bare `$` as USD, recorded as inferred).
- **A place only when it is a postal "City, Region, Country"**; "Remote Poland" or
  "Canada (Remote)" stay in `raw["sections"]["Location"]` (which the grader reads),
  as for Ashby.
- **Shared helpers** moved to `app/parsers/common.py` (`description_html`,
  `parse_compensation`) so both parsers use one copy.
- **Not hooked in.** The host is not in `PLATFORM_HOSTS` and the parser is not in
  `_registry` (`app/parsers/__init__.py`), so nothing in the real flow can use it.
  `tests/unit/test_greenhouse_parser.py::TestNotHookedYet` records that.

## Checked on the three real pages (stored in `jobs_test`)

Title, company, location, pay range and currency were each verified against the
page, and the stored description matches the page's text word for word (1,365,
1,009 and 1,022 words) with no scripts or unsafe tags.

## To hook it in

1. Add the hosts (`greenhouse.HOSTS`) to `PLATFORM_HOSTS` and the parser to
   `_registry`, and replace `TestNotHookedYet` with detection tests.
2. Existing Greenhouse raw listings were saved as `unsupported`; set them back to
   waiting (`status = NULL`) so the Grade button picks them up.
3. Decide how to treat pay in a currency other than EUR (below).

## Consequences

- **Salary has no currency column.** `jobs.salary_min/max` hold the number only, so
  259,000 (PLN) would read like 259,000 euros on the board. The currency is in
  `raw`; deciding whether to add a column, convert, or leave non-euro pay empty is
  open.
- **"Job not found" is unverified.** No removed Greenhouse page was available, so
  the detection (a missing title plus "no longer open"-style wording, or
  `error=true` in the URL) is a best guess covered by synthetic tests only.
- Parsing creates `companies` rows (and a `locations` row for a postal address) in
  the shared tables, as for Ashby.
