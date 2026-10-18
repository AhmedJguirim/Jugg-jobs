# 0020 - A Lever parser, validated before it is hooked in

Status: Accepted - 2026-10-18

## Context

Four Lever links (`jobs.lever.co/<company>/<uuid>`: three from `jobgether`, one
from `insiderone`) were saved before any parser existed, so they are stored as
`unsupported` (ADR-0016). As with Greenhouse (ADR-0019), the parser is built from
them and checked in `jobs_test` before it is connected to the real `jobs` table.

## Decision

- **`app/parsers/lever.py`**, same contract as the other parsers. Lever's copied
  content has plain, stable markup: the title is the h2 in `posting-headline`, the
  tags under it (`location`, `department`, `commitment`, `workplaceTypes`) become
  `raw["sections"]` under the labels the Ashby parser already uses (Location,
  Department, Employment Type, Location Type) so the grader reads every platform
  alike, and `data-qa="job-description"` starts the posting, followed by its
  titled sections.
- **The description is the posting only**: the opening block and each titled
  section. The AI-use disclaimer and the repeated "apply" button are page
  furniture and are left out; everything else is kept word for word.
- **The company is recovered from the page text.** The copied page has no company
  name, only the URL slug. A capitalised phrase in the text that spells the slug
  ("Insider One" for `insiderone`) is used; otherwise the slug is capitalised
  ("Jobgether"). Nothing else is guessed.
- **Shared helpers grew**: `common.read_pay` (pay from the sentence that states it,
  never from equity/bonus/stipend sentences, currency kept in `raw`) and
  `common.place_from_text` (a place only for a postal "City, Region, Country")
  moved out of the Greenhouse parser so both use one copy. A two-letter part
  after a comma is a US state only if it is one: "Austin, TX" is a state,
  "London, UK" a country (this fixed "London, UK" being read as a state).
- **Not hooked in.** The hosts (`jobs.lever.co`, `jobs.eu.lever.co`) are not in
  `PLATFORM_HOSTS` and the parser is not in `_registry`;
  `tests/unit/test_lever_parser.py::TestNotHookedYet` records that.

## Checked on the four real pages (stored in `jobs_test`)

Title, tags, company, link and pay were verified against each page; the one salary
($100,000-$130,000, Jobgether US) was found, "28 unavailable days per year" and
"a $500M Series E" were not mistaken for pay, and each stored description matches
its page's posting text word for word with no scripts, styles or classes.

## To hook it in

1. Add the hosts to `PLATFORM_HOSTS` and the parser to `_registry`; replace
   `TestNotHookedYet` with detection tests.
2. The Lever raw listings were saved as `unsupported`; set them back to waiting
   (`status = NULL`) so Grade picks them up.

## Consequences

- **Jobgether posts on behalf of a hidden employer** ("listed on behalf of a
  partner company"). The company is therefore "Jobgether", the poster, not the
  hiring company, which the page does not name.
- Lever's location is usually a bare country ("US", "Netherlands"), which is kept
  in `raw["sections"]` and not turned into a `locations` row; the grader sees it
  next to "Location Type: Remote".
- Salary has no currency column (as in ADR-0019); the currency is in `raw`.
- "Job not found" for Lever is unverified: no removed Lever page was available, so
  the detection (no title plus "couldn't find anything here" / "no longer open"
  wording) is covered by made-up examples only.
- Parsing creates `companies` rows in the shared tables, as for the other parsers.

## Update: hooked in (2026-10-18)

After the four pages checked out, `jobs.lever.co` and `jobs.eu.lever.co` were added
to `PLATFORM_HOSTS` and the parser to `_registry`. The four raw listings saved as
`unsupported` were set back to waiting so the Grade button and bulk grading pick
them up; a Lever URL saved from now on waits to be graded like the others. The
grader's Salary fact carries the currency kept in `raw` (ADR-0019), so the Jobgether
US range reads `100000-130000 USD (yearly)`.
