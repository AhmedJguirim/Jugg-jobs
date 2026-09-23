# 0005 - Append-only command audit log for agent work

Status: Accepted - 2026-09-20

## Context

Much of the work on this project is performed by an AI agent running in an
automatic mode, where shell commands execute without a per-command approval
prompt. The reviewer needs to answer, after the fact: what was run, why, and
could any of it have destroyed something?

Shell history is not sufficient - it records the command but not the intent,
not the outcome, and not the severity.

## Decision

Add `tools/cmdlog.py`, a standalone tool that records every command with an
explicit risk level on a five-point scale:

| Level | Meaning | Example |
|---|---|---|
| `none` | read-only, cannot change state | `ls`, `SELECT`, `git status` |
| `low` | writes scratch/derived state, trivially reversible | `mkdir`, `pytest` |
| `medium` | changes project state or installs software | `pip install`, `alembic upgrade` |
| `high` | changes persistent data or history | `DELETE FROM`, `git reset --hard` |
| `severe` | destroys a datastore, effectively irreversible | `DROP DATABASE`, `git push --force` |

Design constraints:

- **Standalone.** It imports only the standard library and nothing from `app/`,
  so it cannot break the application or be broken by it.
- **Append-only**, to two files: `command_audit.log` (human) and
  `command_audit.jsonl` (machine).
- **Heuristics are checked against the stated risk.** If a command is logged as
  less dangerous than the built-in rules judge it, the tool prints a warning at
  write time, and `cmdlog verify` re-checks the whole history afterwards. This
  is what stops an agent from quietly under-reporting its own risk.
- **Unknown commands default to `low`, never `none`** - an unrecognised command
  is not assumed harmless.
- Two modes: `run` (execute and record, capturing exit code and duration) and
  `log` (record a command executed elsewhere).

## Consequences

- The reviewer can audit a session with `cmdlog report --min-risk high` and
  confirm honesty with `cmdlog verify`.
- Credentials must be kept out of the recorded command text (passwords are
  passed via environment variables such as `PGPASSWORD`).
- The record depends on the agent actually invoking the tool; it documents
  behaviour, it does not enforce it. A shell wrapper or hook could enforce it
  later.
- The risk rules are regex-based and will need extending as new tooling is
  introduced; they are unit-tested in `tests/unit/test_cmdlog.py`.
