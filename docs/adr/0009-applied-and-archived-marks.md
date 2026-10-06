# 0009 - Applied and archived marks on vacancies

Status: Accepted - 2026-09-27

## Context

The board lists every vacancy, re-sorted by fit on each visit. Once you have
applied to one, or decided it is not for you, it keeps coming back at the top.
The user wanted to mark a vacancy **applied** or **archived** from the list,
hide those by default, and still be able to pull either set back up.

Three questions shaped the design:

1. Where the marks live - on `jobs`, on `job_assessments`, or in a table of
   their own.
2. Whether a mark is a flag or a timestamp.
3. Whether filtering happens in SQL or in the page.

## Decision

**Two nullable `timestamptz` columns on `jobs`: `applied_at` and
`archived_at`.** `NULL` means not marked.

- **On `jobs`, not per profile.** Assessments are keyed by profile because each
  profile grades differently, but "I applied to this" is a fact about the
  person, not about which rubric scored it. There is one user. A separate table
  would be one-to-one with `jobs` and ADR-0006 already puts one-to-one data in
  columns.
- **Timestamps, not booleans.** "When did I apply?" is the obvious next question
  and a boolean cannot answer it. Setting a mark that is already set keeps the
  original time.
- **Independent, not one status enum.** A vacancy can be applied *and* archived
  (applied, then rejected). An enum would force a choice.
- **Ingest never clears them.** The marks are not in `JobCreate`, so `upsert`,
  which sees the same vacancy again on every search, does not touch them.
- **Filtered client-side.** `/api/board` already ships the whole corpus and the
  page filters in the browser; the marks ride along as two more fields. The
  default view is "Open" (neither mark), with Applied / Archived / Everything
  one select away.
- **One write endpoint:** `PATCH /api/jobs/{id}/marks` with
  `{"applied": bool?, "archived": bool?}`; an omitted field is left alone and
  `false` clears the mark. It goes through `job_repo.set_marks`.

## Consequences

- The board API is no longer strictly read-only.
- Marks are not per profile. If profiles ever stand for different people, this
  needs revisiting (a `profile_name` key, like assessments).
- `delete_expired` still deletes expired vacancies whether marked or not, so an
  applied vacancy's record goes with it. Nothing calls `delete_expired` today.
- There is no history: un-marking and re-marking loses the first timestamp.
