"""Turn a company scrape's workflow state into one line a person can read.

A batch scrape can run for hours, so the page shows, per employer, something
like "vacancy 6/23 - translating the description". The raw material is the
workflow's DBOS status plus the events it publishes (see `_report` in
`app/workflows/enrichment.py`); this module only decides what to say.

Kept free of DBOS, like `company_scrape`, so it is testable without a runtime.
"""

from __future__ import annotations

from typing import Any, Optional

# DBOS workflow statuses, grouped by what they mean to the person watching.
WAITING = {"ENQUEUED"}
RUNNING = {"PENDING"}
FINISHED = {"SUCCESS"}
CANCELLED = {"CANCELLED"}
FAILED = {"ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}


def state_of(status: Optional[str]) -> str:
    """Collapse a DBOS status into waiting / running / done / cancelled / failed.

    None means the workflow does not exist yet: the batch creates its children
    a moment after it starts, and a poll can land in between.
    """
    if status is None or status in WAITING:
        return "waiting"
    if status in RUNNING:
        return "running"
    if status in FINISHED:
        return "done"
    if status in CANCELLED:
        return "cancelled"
    return "failed"


def running_label(
    progress: Optional[dict],
    current_state: Optional[str] = None,
    current_stage: Optional[str] = None,
) -> str:
    """What a running company scrape is doing right now.

    `progress` is the scrape's latest `progress` event. While grading,
    `current_state` and `current_stage` describe the vacancy it is waiting on:
    another employer's vacancy may hold the GPU, in which case this one is
    queued behind it, and saying so explains a long pause.
    """
    if not progress:
        return "starting"

    phase = progress.get("phase")
    if phase == "fetching":
        page, pages, found = progress.get("page"), progress.get("pages"), progress.get("found", 0)
        where = f"fetching page {page}/{pages}" if pages else "searching the job board"
        return f"{where} · {found} found" if found else where

    if phase == "grading":
        position = f"vacancy {progress.get('done', 0) + 1}/{progress.get('total', 0)}"
        if current_state == "waiting":
            return f"{position} - waiting for the GPU"
        return f"{position} - {current_stage or 'reading the vacancy'}"

    return phase or "running"


def finished_label(result: Optional[dict]) -> tuple[str, bool]:
    """Summarise a finished company scrape. Returns (label, is_problem)."""
    result = result or {}
    status = result.get("status")
    if status == "no_query":
        return "no search query for this employer", True
    if status == "missing":
        return "employer no longer exists", True

    found = f"{result.get('vacancies', 0)} found · {result.get('created', 0)} new"
    if result.get("enrich_status") == "llm_unavailable":
        return f"{found} · grading skipped: Ollama is not running", True
    enriched = result.get("enriched", 0)
    return (f"{found} · {enriched} graded" if enriched else found), bool(result.get("failed"))


def batch_summary(companies: list[dict[str, Any]]) -> dict[str, int]:
    """Counts across the batch, for the header line."""
    counts = {"total": len(companies), "waiting": 0, "running": 0, "done": 0, "cancelled": 0, "failed": 0}
    for company in companies:
        counts[company["state"]] += 1
    return counts
