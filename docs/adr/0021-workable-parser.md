# 0021 - A Workable parser, validated before it is hooked in

Status: Accepted - 2026-10-18

## Context

Three `apply.workable.com` links were saved before any parser existed (stored as
`unsupported`, ADR-0016). Two were job pages. The third (`supportyourapp/j/1C3E8E25DA`)
was a mistake: the job had been removed, Workable redirected the browser to the
company's careers page (`.../supportyourapp/?not_found=true`), and that page's
~430 KB of HTML was copied under the job's URL. As with Greenhouse and Lever, the
parser is built from the saved pages and checked in `jobs_test` first.

## Decision

- **`app/parsers/workable.py`**, same contract as the other parsers. Workable's CSS
  classes are hashed, but its `data-ui` attributes are stable and descriptive, so
  only those are read: `job-title` (the h1), `job-workplace`, `job-department`,
  `job-type`, `job-code`, `job-location`, `company-logo`, `apply-button`, and the
  sections `job-description` / `job-requirements` / `job-benefits`. The facts use
  the labels the Ashby and Lever parsers use; the three sections are joined, each
  keeping its heading, and every word of them is kept.
- **A removed job is recognised two ways**, both judged before the URL is read (a
  redirect URL can have no job id): `not_found=true` in the URL, and the careers
  page's own message "This job is no longer available". The second is what catches
  the saved page above, whose URL looks like a job's. A careers page *without* that
  message is not a removal but the wrong page, and is reported as unreadable
  ("this is the company's careers page"). The cards of its jobs list also carry
  `data-ui=job-title`, so only an `h1` counts as the job's title.
- **The company comes from the logo's `alt`**, and the board slug from the URL or,
  for a URL with no company (`/j/<id>`), from the logo's link. The canonical
  `detail_url` always includes the slug, so the same job saved with either URL
  has one `external_id` and one address.
- **Several locations** (a job open in many countries) are joined with `; ` in
  `raw["sections"]["Location"]`; only a *single* postal location becomes a
  `locations` row.
- **The careers-page fixture is trimmed** (SVGs and huge attributes removed, 45 KB
  instead of 432 KB); the structure the parser reads is untouched.
- **Not hooked in**: not in `PLATFORM_HOSTS` / `_registry` (a test records that).

## Found while checking the output (and fixed for every parser)

A salary of "1-5 hourly" came out for a job that states no salary at all. Workable's
bullet lists have no full stops, so a whole section was read as one sentence, and the
"5" of "5+ years" was taken as a wage. Three changes in `app/parsers/common.py`:

- pay is read per block (`Node.lines()`: one string per paragraph or list item), not
  per run-on string;
- a pay *label* with no figure ("Base Pay Grade") may borrow the next line when that
  line is an amount (a currency marker or a thousands-formatted number), which is how
  Affirm's Greenhouse page lays out its range;
- a number only counts as pay if a currency is named or it is a real-looking figure
  (1,000 or more), so a bare "5" or "28" never becomes a wage.

The Greenhouse and Lever parsers use the same code; their tests are unchanged and pass.

## Checked on the three real pages

`jobs_test` holds the two jobs (DaCodes, Pearl) with title, company, facts, locations
and links verified against the pages, and descriptions identical word for word to the
three sections. Neither job states a salary ("Competitive Salary: Based on experience
and skills" has no figure), and neither is given one. The careers page gives
`ListingNotFound`.

## To hook it in

1. Add `apply.workable.com` to `PLATFORM_HOSTS` and the parser to `_registry`; replace
   `TestNotHookedYet` with detection tests.
2. Set the Workable raw listings saved as `unsupported` back to waiting, except the
   careers page (#235): running it will mark it `not_found`, which is right.

## Consequences

- **Pearl posts on behalf of a client** ("a HealthTech Company"); the company is Pearl,
  the poster. DaCodes' page is in Spanish: it is graded on its Spanish text, and shows
  as "Not stated" in the board's Language filter (ADR-0015: no translation of
  non-English listings).
- Hex job ids keep the case they were saved with (`5E3ED2DFA7`); a URL with a
  different case would be a different `external_id`.
- Parsing creates `companies` rows in the shared tables, as for the other parsers.

## Update: hooked in (2026-10-18)

After the pages checked out, `apply.workable.com` was added to `PLATFORM_HOSTS` and the
parser to `_registry`. The three raw listings saved as `unsupported` were set back to
waiting, the careers-page one included: grading it reports `not_found` and marks it so,
which is the right ending for it. A Workable URL saved from now on waits to be graded
like the others.
