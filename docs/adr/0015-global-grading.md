# 0015 - Grading vacancies from anywhere, and creating real jobs

Status: Accepted - 2026-10-17

## Context

Listings now arrive from places other than Nationale Vacaturebank (first Ashby,
via ADR-0014). The Dutch grader assumes a Netherlands job board: Dutch is its
only language barrier, the EU/NL permit is its only legal question, and
"Dutch education levels" are written into the prompt. A vacancy from a global
board can need Mandarin, Russian citizenship, a security clearance, a time zone,
or hire remotely only in three named countries (both stored Ashby pages do the
last: "remote within Poland", "UK and select EU countries").

Two requirements shaped the design: the Dutch pipeline must not change, and a
listing must be graded before anything is written to `jobs`.

## Decision

**A second grader, not a change to the first.** `app/llm/global_grader.py` with
its own prompt (`global_prompts.py`, version `assess-global-v1`) and profile
rendering. It *imports* the Dutch grader's helpers (`classify_stack`, the band
table, JSON parsing and repair, the quote check) and changes none of them. The
Dutch prompt, `assess_job` and their tests are untouched.

**The model reports with quotes; code decides** (the lesson of ADR-0008, applied
to new facts). The model returns, each with a verbatim quote from the advert:
required languages (with a level), required citizenship, security clearance, the
region a remote role is limited to, visa sponsorship, and an existing
right-to-work requirement. A claim counts only if its quote is in the text the
model was given - and, for languages, names the language; for citizenship,
mentions citizens/nationals/passports; for permits and clearance, the relevant
terms. Then lookups against the profile decide whether it bars *this* candidate:

| Restriction | Bars the candidate when |
|---|---|
| language (level above "preferred") | the profile does not list that language as native/fluent/proficient (basic only meets "basic") |
| citizenship | none of the nationalities named is the candidate's (`citizenship` in the profile if present, otherwise the country they live in) |
| security clearance | always |
| remote limited to places | the candidate's country (from `location.based_in`) is not in them; a small region table (EU, Europe, EMEA, Africa, MENA, ...) lives in `app/llm/geo.py` |
| existing right to work / no sponsorship | the profile says they lack authorisation, and the role is not open remote |

The tier is then `blocked` (any barrier), `remote_ok` (remote and not
restricted out), or `relocation` (everything else, as for Dutch vacancies).
Preferred languages and other restrictions become red flags only. The stack
tier and the score band are still computed in code. `dutch_required` is set only
when a verified, blocking requirement is Dutch, so the board's Dutch badge keeps
meaning one thing.

**Grade first, then create.** `process_listing_workflow` runs: load, parse, check
the model is up, prepare, **grade on the `llm` queue** (same single slot and
temperature cooldown as the Dutch enrichment), then persist. Persisting writes
the job, an "English" translation row holding the cleaned text (marked
`skipped`, so the board's drawer shows it), and the assessment in one
transaction. An unreadable reply or a stopped model ends with a status and
writes nothing. A listing whose current grade matches (same text, prompt version
and profile) skips the model.

**The Dutch pipeline is fenced off.** It selects work by "no current translation
/ assessment", which every Ashby job would match, and would overwrite the new
grade with a Netherlands-specific one. So `pending_translation`,
`pending_assessment` and `select_for_enrichment` only consider
`source == "nvb"`, and `enrich_job_workflow` returns `not_nvb` for anything else
(its `prepare_text` result gained a `source` key, read with a default so a
recovered run replays cleanly; the step sequence is unchanged, so
`APPLICATION_VERSION` is not bumped). This is the "condition on the vacancy's
origin": the source is set by the ingest that creates the row, and every existing
vacancy is `nvb`. Nothing else in the NVB path changed.

**Saving a listing starts the workflow** when its URL has a parser
(`POST /api/workspace/raw-job-listings`); other URLs, NVB included, are only
stored. The form shows each run's outcome. `scripts/process_listing.py` and
`POST .../{id}/process` run the same workflow (with `--profile`/`--force`).

## Consequences

- The board lists these jobs with the same card and drawer as Dutch ones; fields
  the platform does not provide (dates, contact, hours, contract, education) are
  simply unknown there.
- `jobs_test` and its page remain but nothing writes to them any more.
- No translation for non-English listings: the text is stored as written and the
  prompt assumes English. A German Ashby posting is graded on its German text.
- Grading takes as long as the model does. On a machine where the GPU has no free
  memory Ollama falls back to the CPU (about 9 minutes per listing was observed),
  so the status line shows "waiting for the model" for a while.
- The nationality table (`geo.DEMONYMS`) and region table are finite. A
  nationality or region not in them is treated as not matching, which errs
  towards blocking and a human look.
- `profiles/*.json` may gain an optional `citizenship` list. Without it the
  candidate's country of residence stands in, so a role demanding any other
  nationality is blocked.
