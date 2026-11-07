"""One readable line for a listing's grading run, and a batch's totals.

A batch can run for hours (grading is one listing at a time on the GPU), so
each listing shows what it is doing - "waiting for the GPU", "GPU at 85C -
cooling down for 3 min" - and how it ended. DBOS-free, so it is testable
without a runtime.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["state_of", "running_label", "finished_label", "batch_summary"]

# DBOS workflow statuses, grouped by what they mean to the person watching.
WAITING = {"ENQUEUED"}
RUNNING = {"PENDING"}
FINISHED = {"SUCCESS"}
CANCELLED = {"CANCELLED"}


def state_of(status: Optional[str]) -> str:
    """Collapse a DBOS status into waiting / running / done / cancelled / failed.

    None means the workflow does not exist yet: a batch creates its children a
    moment after it starts, and a poll can land in between.
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


def running_label(grade_state: Optional[str], stage: Optional[str]) -> str:
    """What a running listing is doing.

    `grade_state` is the state of its grading step (the one on the single-slot
    `llm` queue): None until the page has been read and the step is queued.
    `stage` is that step's latest stage event, which carries the cooldown
    message while the GPU is hot.
    """
    if grade_state is None:
        return "reading the page"
    if grade_state == "waiting":
        return "waiting for the GPU"
    if grade_state == "running":
        return stage or "grading"
    return "saving the job"


def finished_label(result: Optional[dict]) -> tuple[str, bool]:
    """Summarise a finished listing. Returns (label, is_problem)."""
    result = result or {}
    status = result.get("status")
    reason = result.get("reason")

    if status == "stored":
        label = f"graded: fit {result.get('fit_score')} ({str(result.get('verdict', '')).replace('_', ' ')}), {result.get('eligibility')}"
        flags = result.get("red_flags") or []
        if result.get("blocked") and flags:
            label += f" - {flags[0]}"
        return label, False
    if status == "up_to_date":
        return "already graded - nothing changed", False
    if status == "not_found":
        return "job not found on the platform - nothing to grade", False
    if status == "unsupported":
        return "no parser for this site", True
    if status == "no_html":
        return "its HTML was cleaned up after grading - paste the page again to re-grade", True
    if status == "unparseable":
        return f"could not read the page: {reason}" if reason else "could not read the page", True
    if status == "llm_unavailable":
        return f"not graded: {reason or 'Ollama is not running'}", True
    if status == "ungraded":
        return "the model's reply could not be read - nothing was saved, try again", True
    if status == "no_description":
        return "the page had no description to grade", True
    return (status or "finished").replace("_", " "), True


def batch_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"total": len(rows), "waiting": 0, "running": 0, "done": 0, "cancelled": 0, "failed": 0}
    for row in rows:
        counts[row["state"]] += 1
    return counts
