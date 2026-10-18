# 0022 - A Breezy parser built on URL-derived title and company

Status: Accepted - 2026-10-18

## Context

Nine `<company>.breezy.hr/p/<id>-<title-slug>` links were saved before any parser existed
(stored as `unsupported`, ADR-0016). As with the other platforms, the parser is built from
them and checked in `jobs_test` before it is connected to the real `jobs` table.

The saved pages differ from every earlier platform: **all nine are only the description
block** (`<div class="description">`), with no header, so the HTML has no title, company,
location or job type.

## Decision

- **`app/parsers/breezy.py`**, same contract as the other parsers. It reads the description
  block and takes the rest from the URL: the company from the subdomain, the title from
  the slug after the id, the id from `/p/<id>-...` (12 to 16 hex characters, lower-cased as
  the key). The canonical link is the page's own URL minus query and trailing slash, with
  a lower-cased host; it is also the apply link (the page has no separate one).
- **A slug is not a name, so its real spelling is looked for in the text** (shared helpers
  in `app/parsers/common.py`, which Lever now uses too): `company_from_slug` finds a
  capitalised phrase that spells the slug ("20four7va" -> "20four7VA", "localize" ->
  "Localize"), and `title_from_slug` finds the consecutive words that spell the title slug
  (found for 4 of 9: "B-CPT-11601 Bilingual Student Operations & Administrative Virtual
  Assistant"). Otherwise the slug is capitalised, keeping common acronyms upper-case
  ("ai" -> "AI", "qa" -> "QA"). Whether each was read or guessed is recorded in
  `raw["title_source"]` and `raw["company_source"]` ("text" or "slug").
- **Nothing structured is guessed about location, job type or department.** They are not in
  these pages; the few that appear as free text ("Location: Kyiv, Ukraine - Remote") stay in
  the description, which the grader reads. `raw["sections"]` is empty.
- **Pay is read as for the other platforms** (`read_pay`): none of the nine states a figure
  ("Competitive salary" has none) and none was given one.
- **A removed position** is recognised by wording ("no longer available", "position has been
  filled"...) when there is no description block. This is unverified: no removed Breezy page
  was available, so it is covered by made-up examples only.
- **Not hooked in**: not in `PLATFORM_HOSTS` / `_registry` (a test records that).

## Checked on the nine real pages (stored in `jobs_test`, ids 12-20)

Every description matches its page's text word for word, with no scripts and no tracking
attributes (`data-faitracker-click-bind` is stripped, links keep their `href`). Company and
title are exact where the text spells them and capitalised-slug otherwise.

## Found while testing

A URL with a trailing slash gave the title "Backend Engineer/" (the slug pattern swallowed
the slash), and an upper-case host leaked into the canonical link. Both fixed.

## To hook it in

1. Add `breezy.hr` to `PLATFORM_HOSTS` (it matches `<company>.breezy.hr` as a subdomain) and
   the parser to `_registry`; replace `TestNotHookedYet` with detection tests.
2. Set the nine Breezy raw listings saved as `unsupported` back to waiting.

## Consequences

- **Titles and companies are weaker than on the other platforms.** Five of nine titles are a
  capitalised slug without punctuation ("He4 Front End Developer React React Router
  Typescript", "Senior Frontend Developer Tech Lead") and four of nine companies are the
  subdomain ("Uvation", "Inallmedia LLC").
- **Copying more of the page would fix that.** A copy that includes the page's header (title,
  location, type, department) would carry the real values. This parser does not read a
  header because no saved page has one; supporting it needs a sample.
- Several postings are written by agencies for unnamed clients; the company is the poster.
- Parsing creates `companies` rows in the shared tables, as for the other parsers.
