"""Durable workflow: raw listing -> parsed -> graded -> a real job (ADR-0015).

    load -> parse -> [model available?] -> prepare -> grade (llm queue) -> persist

Everything before `grade` is cheap and GPU-free. `grade` is its own workflow on
the `llm` queue, so it shares the one GPU slot with the Dutch enrichment (and the
temperature cooldown applies to it too). Nothing is written to `jobs` until a
grade exists: if the model is down or its reply is unreadable the workflow ends
with a status and stores nothing, and running it again is safe - `persist` is an
upsert on (source, external_id), and an unchanged listing with a current grade
skips the GPU entirely.

Like `listing_processing`, a page that cannot be used (removed posting,
unsupported site, unreadable markup) is a returned status, not an exception.
"""

from __future__ import annotations

from typing import Any, Optional

from dbos import DBOS

from app.db.session import session_scope
from app.logging_setup import log
from app.services import listing_grading as grading
from app.services import listing_processing as processing
from app.workflows.enrichment import LLM_QUEUE, STAGE_EVENT, cool_down_gpu, llm_available
from app.workflows.listing_processing import load_listing_step, parse_step


@DBOS.step()
def prepare_step(job: dict[str, Any], profile_name: Optional[str]) -> dict[str, Any]:
    with session_scope() as session:
        return grading.prepare(job, profile_name, session)


@DBOS.step()
def grade_step(prepared: dict[str, Any]) -> dict[str, Any]:
    """GPU step. Runs only inside `grade_listing_workflow`, on the `llm` queue."""
    return grading.grade(prepared)


@DBOS.step()
def persist_step(job: dict[str, Any], prepared: dict[str, Any], assessment: dict[str, Any]) -> dict[str, Any]:
    with session_scope() as session:
        return grading.persist(session, job, prepared, assessment)


@DBOS.workflow()
def grade_listing_workflow(prepared: dict[str, Any]) -> dict[str, Any]:
    """Grade one prepared listing. Enqueue on LLM_QUEUE - never call directly -
    so only one model call is on the GPU at a time."""
    cool_down_gpu()
    DBOS.set_event(STAGE_EVENT, "grading the listing")
    return grade_step(prepared)


@DBOS.workflow()
def process_listing_workflow(
    raw_listing_id: int,
    profile_name: Optional[str] = None,
    force: bool = False,
) -> dict[str, Any]:
    listing = load_listing_step(raw_listing_id)
    parsed = parse_step(listing)
    if parsed["status"] != processing.STORED:
        log.info("raw listing %s: %s (%s)", raw_listing_id, parsed["status"], parsed.get("reason"))
        return {k: v for k, v in parsed.items() if k != "job"}

    job = parsed["job"]
    prepared = prepare_step(job, profile_name)
    if not prepared["text"]:
        return {"status": "no_description", "raw_listing_id": raw_listing_id, "reason": "the page had no description to grade"}
    if prepared["current"] and not force:
        log.info("raw listing %s: already graded, GPU not used", raw_listing_id)
        return {"status": "up_to_date", "raw_listing_id": raw_listing_id, "job_id": prepared["job_id"], "title": prepared["title"]}

    health = llm_available()
    if not health["ok"]:
        return {"status": "llm_unavailable", "raw_listing_id": raw_listing_id, "reason": health["message"]}

    assessment = DBOS.enqueue_workflow(LLM_QUEUE, grade_listing_workflow, prepared).get_result()
    if not assessment:
        return {
            "status": "ungraded",
            "raw_listing_id": raw_listing_id,
            "reason": "the model's reply could not be read; nothing was stored. Try again.",
        }

    result = persist_step(job, prepared, assessment)
    log.info(
        "raw listing %s -> job %s (%s, fit %s, %s)",
        raw_listing_id, result["job_id"], "new" if result["created"] else "updated",
        result["fit_score"], result["eligibility"],
    )
    return {**result, "raw_listing_id": raw_listing_id}
