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

from dbos import DBOS, SetWorkflowID

from app.db.session import session_scope
from app.logging_setup import log
from app.services import listing_grading as grading
from app.repositories import raw_job_listing as raw_repo
from app.services import listing_processing as processing
from app.workflows.enrichment import LLM_QUEUE, STAGE_EVENT, cool_down_gpu, llm_available
from app.workflows.listing_processing import load_listing_step, parse_step


# Batches of listings run on their own queue, two at a time like the company
# batch: while one waits for the GPU the next is being read. The grading itself
# still goes through the single-slot `llm` queue.
LISTING_QUEUE = "listing-grade"
LISTING_CONCURRENCY = 2
MAX_BATCH = 100

_queue_registered = False


def register_queue() -> None:
    """Register the batch queue. Call AFTER `DBOS.launch()` (see
    `enrichment.register_queues` for why), right after it. Idempotent."""
    global _queue_registered
    if _queue_registered:
        return
    DBOS.register_queue(LISTING_QUEUE, worker_concurrency=LISTING_CONCURRENCY, on_conflict="always_update")
    _queue_registered = True


def batch_child_id(batch_id: str, listing_id: int) -> str:
    """Workflow id of one listing inside a batch; deterministic so the status
    endpoint can find it from the batch id alone."""
    return f"{batch_id}-listing-{listing_id}"


def grade_child_id(process_id: str) -> str:
    """Workflow id of the GPU step of a listing's run, so its progress (queued,
    cooling down, grading) can be read from the run's id alone."""
    return f"{process_id}-grade"


# What each ending means for the raw listing. Anything else (the model was down,
# its reply was unreadable) leaves it waiting, so it can simply be tried again.
_MARKS = {
    "not_found": raw_repo.NOT_FOUND,
    "unparseable": raw_repo.UNPARSEABLE,
    "unsupported": raw_repo.UNSUPPORTED,
}


@DBOS.step()
def mark_step(raw_listing_id: int, result: dict[str, Any]) -> None:
    """Record how the run ended on the raw listing, so the waiting list drops
    what is done (graded) or can never be graded (job gone, unsupported site)."""
    status = result.get("status")
    with session_scope() as session:
        if status in ("stored", "up_to_date"):
            raw_repo.mark(session, raw_listing_id, raw_repo.GRADED, job_id=result.get("job_id"))
        elif status in _MARKS:
            raw_repo.mark(session, raw_listing_id, _MARKS[status])


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


def _process(raw_listing_id: int, profile_name: Optional[str], force: bool) -> dict[str, Any]:
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

    # A known id, so the run's progress can be read from the grading step.
    with SetWorkflowID(grade_child_id(DBOS.workflow_id)):
        handle = DBOS.enqueue_workflow(LLM_QUEUE, grade_listing_workflow, prepared)
    assessment = handle.get_result()
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


@DBOS.workflow()
def process_listing_workflow(
    raw_listing_id: int,
    profile_name: Optional[str] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Grade one stored listing and create its job. Started by the Grade button,
    or as one child of `grade_listings_workflow`."""
    result = _process(raw_listing_id, profile_name, force)
    mark_step(raw_listing_id, result)
    return result


@DBOS.workflow()
def grade_listings_workflow(listings: list[dict], profile_name: Optional[str] = None) -> dict[str, Any]:
    """Grade several listings - the same flow as one - and wait for them all.

    `listings` is `[{"id": ..., "url": ...}]`, chosen by the caller. Each is one
    `process_listing_workflow` on LISTING_QUEUE; their grading still goes one at
    a time through the `llm` queue, with the GPU temperature check before each.

    Cancelling this workflow with `cancel_children=True` stops every listing
    still waiting or running, and the grading steps they queued.
    """
    batch_id = DBOS.workflow_id
    DBOS.set_event("listings", listings)
    log.info("grading batch %s: %s listing(s)", batch_id, len(listings))

    handles = []
    for listing in listings:
        with SetWorkflowID(batch_child_id(batch_id, listing["id"])):
            handles.append(
                DBOS.enqueue_workflow(LISTING_QUEUE, process_listing_workflow, listing["id"], profile_name, False)
            )

    results = []
    for listing, handle in zip(listings, handles):
        try:
            results.append(handle.get_result())
        except Exception as error:  # one listing must not sink the batch
            log.warning("grading batch %s: listing %s did not finish: %s", batch_id, listing["id"], error)
            results.append({"raw_listing_id": listing["id"], "status": "failed", "error": str(error)})

    summary = {
        "listings": len(listings),
        "graded": sum(1 for r in results if r.get("status") == "stored"),
        "not_found": sum(1 for r in results if r.get("status") == "not_found"),
        "problems": sum(1 for r in results if r.get("status") not in ("stored", "up_to_date", "not_found")),
    }
    log.info("grading batch %s done: %s", batch_id, summary)
    return summary
