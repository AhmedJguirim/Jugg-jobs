import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import uvicorn
from dbos import DBOS, DBOSConfig, SetWorkflowID
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app import logging_setup
from app.api import board_router, companies_router, workspace_router
from app.db.session import session_scope
from app.integrations.nvb import fetch_jobs_page, to_job_creates
from app.integrations.nvb.schema import JobSearchResponse
from app.repositories import enrichment as enrichment_repo
from app.repositories import job as job_repo
from app.services.job_ingest import DEFAULT_LIMIT, IngestStats
from app.workflows import enrichment as enrichment_flow
from app.logging_setup import log

# Welcome to DBOS!
# This example shows you how to use DBOS to build applications
# that are resilient to any failure.

app = FastAPI()
config: DBOSConfig = {
    "name": "dbos-app-starter",
    "system_database_url": os.environ.get("DBOS_SYSTEM_DATABASE_URL"),
    # Bump APPLICATION_VERSION whenever a workflow's *shape* changes - a step
    # added, removed or reordered. DBOS replays a recovered workflow against the
    # current code and aborts if the recorded steps no longer line up
    # (DBOSUnexpectedStepError). Changing the enrichment step order under a
    # running scrape killed one that way. A new version leaves in-flight
    # workflows to the old one instead of replaying them against new code.
    "application_version": enrichment_flow.APPLICATION_VERSION,
    "conductor_key": os.environ.get("DBOS_CONDUCTOR_KEY"),
}
DBOS(config=config)

# APIs behind the views (app/api/).
app.include_router(board_router)
app.include_router(companies_router)
app.include_router(workspace_router)

# Mirror DBOS/uvicorn logs into logs/dev.log alongside our own dev logger.
logging_setup.attach("dbos", "uvicorn.error")
logging_setup.section("app start")

steps_event = "steps_event"

SCHEDULE_NAME = "scheduled-workflow"
DEFAULT_CRON = "*/5 * * * * *"

QUEUE_NAME = "demo-queue"
DEFAULT_WORKER_CONCURRENCY = 3


# This endpoint uses DBOS to launch a durable workflow.
@app.get("/workflow/{task_id}")
def launch_durable_workflow(task_id: str) -> None:
    log.info("launching durable workflow task_id=%s", task_id)
    with SetWorkflowID(task_id):
        DBOS.start_workflow(workflow)


# Here is the code for a durable workflow with three steps.
# DBOS workflows are resilient to any failure: if your program is crashed,
# interrupted, or restarted while running this workflow, it automatically
# resumes from its last completed step.
@DBOS.step()
def step_one():
    time.sleep(5)
    DBOS.logger.info("Completed step 1!")


@DBOS.step()
def step_two():
    time.sleep(5)
    DBOS.logger.info("Completed step 2!")


@DBOS.step()
def step_three():
    time.sleep(5)
    DBOS.logger.info("Completed step 3!")


@DBOS.workflow()
def workflow():
    step_one()
    # Use DBOS.set_event to publish progress for the frontend to display.
    DBOS.set_event(steps_event, 1)
    step_two()
    DBOS.set_event(steps_event, 2)
    step_three()
    DBOS.set_event(steps_event, 3)


# Scheduled workflow: runs on a cron schedule, sleeps 5 seconds between log lines.
@DBOS.workflow()
def scheduled_workflow(scheduled_time: datetime, context: Any):
    DBOS.logger.info("Scheduled workflow starting.")
    DBOS.sleep(1)
    DBOS.logger.info("Scheduled workflow ending.")


# This endpoint retrieves the status of a workflow.
@app.get("/last_step/{task_id}")
def get_last_completed_step(task_id: str):
    try:
        step = DBOS.get_event(task_id, steps_event, timeout_seconds=0)
    except KeyError:  # If the task hasn't started yet
        return 0
    return step if step is not None else 0


# This endpoint crashes the application. For demonstration purposes only :)
@app.post("/crash")
def crash_application():
    os._exit(1)


def render_page(name: str, section: str) -> HTMLResponse:
    """Serve html/<name> with the shared top bar (section switch + nav) injected."""
    with open(os.path.join("html", "_topbar.html"), encoding="utf-8") as file:
        topbar = file.read().replace("__SECTION__", section)
    with open(os.path.join("html", name), encoding="utf-8") as file:
        html = file.read().replace("<!--TOPBAR-->", topbar)
    return HTMLResponse(html)


# ---- Jobs section ----


# This endpoint serves the job board: what the flows actually produced.
@app.get("/board")
def board_page():
    return render_page("board.html", "jobs")


# This endpoint serves the employer browser.
@app.get("/companies")
def companies_page():
    return render_page("companies.html", "jobs")


# This endpoint serves the HTML frontend.
@app.get("/")
def readme():
    return render_page("app.html", "jobs")


# ---- Workspace section ----


# Raw listings: paste a link and a page's HTML, stored as-is.
@app.get("/workspace")
def workspace_page():
    return render_page("workspace.html", "workspace")


# The form behind "Process existing listings" (/existing, link prefilled from
# the collected URLs) and "Process other url" (/other, blank). One page; it
# reads the mode from its own path.
@app.get("/workspace/raw/existing")
@app.get("/workspace/raw/other")
def workspace_raw_form_page():
    return render_page("workspace_raw.html", "workspace")


# Listing URLs: extract result links from a pasted Google results block.
@app.get("/workspace/urls")
def workspace_urls_page():
    return render_page("workspace_urls.html", "workspace")


# ---- Schedule endpoints ----

@app.get("/schedule/status")
def get_schedule_status():
    try:
        sched = DBOS.get_schedule(SCHEDULE_NAME)
        cron = sched["schedule"]
        schedule_status = sched["status"]
    except Exception:
        cron = DEFAULT_CRON
        schedule_status = "UNKNOWN"

    since = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    all_wfs = DBOS.list_workflows(
        name="scheduled_workflow",
        start_time=since,
        limit=500,
        load_input=False,
        load_output=False,
    )

    counts: dict[str, int] = {}
    for wf in all_wfs:
        counts[wf.status] = counts.get(wf.status, 0) + 1

    return {
        "cron": cron,
        "schedule_status": schedule_status,
        "workflow_counts": counts,
    }


@app.post("/schedule/apply")
def apply_schedule_endpoint(body: dict):
    cron = body.get("cron", DEFAULT_CRON)
    DBOS.apply_schedules([{
        "schedule_name": SCHEDULE_NAME,
        "workflow_fn": scheduled_workflow,
        "schedule": cron,
        "context": None,
    }])
     # explicitly resume so Apply always leaves the schedule active.
    try:
        DBOS.resume_schedule(SCHEDULE_NAME)
    except Exception:
        pass
    return {"ok": True}

@app.post("/schedule/pause")
def pause_schedule():
    DBOS.pause_schedule(SCHEDULE_NAME)
    return {"ok": True}


@app.post("/schedule/resume")
def resume_schedule():
    DBOS.resume_schedule(SCHEDULE_NAME)
    return {"ok": True}


@app.post("/schedule/trigger")
def trigger_schedule():
    DBOS.trigger_schedule(SCHEDULE_NAME)
    return {"ok": True}


# ---- Queue workflow ----

@DBOS.workflow()
def enqueued_workflow():
    DBOS.logger.info("Enqueued workflow starting.")
    DBOS.sleep(5)
    DBOS.logger.info("Enqueued workflow ending.")


# ---- Queue endpoints ----

@app.get("/queue/status")
def get_queue_status():
    queue = DBOS.retrieve_queue(QUEUE_NAME)
    worker_concurrency = queue.worker_concurrency if queue else DEFAULT_WORKER_CONCURRENCY

    since = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    all_wfs = DBOS.list_workflows(
        name="enqueued_workflow",
        start_time=since,
        limit=500,
        load_input=False,
        load_output=False,
    )

    counts: dict[str, int] = {}
    for wf in all_wfs:
        counts[wf.status] = counts.get(wf.status, 0) + 1

    return {
        "worker_concurrency": worker_concurrency,
        "workflow_counts": counts,
    }


@app.post("/queue/enqueue")
def enqueue_workflow_endpoint():
    DBOS.enqueue_workflow(QUEUE_NAME, enqueued_workflow)
    return {"ok": True}


@app.post("/queue/concurrency")
def update_queue_concurrency(body: dict):
    concurrency = int(body.get("concurrency", DEFAULT_WORKER_CONCURRENCY))
    DBOS.register_queue(QUEUE_NAME, worker_concurrency=concurrency, on_conflict="always_update")
    return {"ok": True}


# ---- Workflow Communication ----

APPROVAL_TOPIC = "approval"
COMM_STATUS_EVENT = "comm_status"

@DBOS.step()
def comm_step_one():
    time.sleep(2)
    DBOS.logger.info("Communication workflow: step 1 complete.")


@DBOS.step()
def comm_step_two():
    time.sleep(2)
    DBOS.logger.info("Communication workflow: step 2 complete.")


@DBOS.workflow()
def communication_workflow():
    comm_step_one()
    DBOS.set_event(COMM_STATUS_EVENT, "waiting")
    decision = DBOS.recv(APPROVAL_TOPIC, timeout_seconds=120)
    if decision == "approve":
        DBOS.set_event(COMM_STATUS_EVENT, "step2")
        comm_step_two()
        DBOS.set_event(COMM_STATUS_EVENT, "completed")
    elif decision == "deny":
        DBOS.set_event(COMM_STATUS_EVENT, "denied")
        DBOS.logger.info("Communication workflow: denied.")
    else:
        DBOS.set_event(COMM_STATUS_EVENT, "timeout")
        DBOS.logger.info("Communication workflow: timed out waiting for approval.")


@app.get("/comm/status/{workflow_id}")
def get_comm_status(workflow_id: str):
    try:
        status = DBOS.get_event(workflow_id, COMM_STATUS_EVENT, timeout_seconds=0)
    except Exception:
        status = None
    return {"state": status or "step1"}


@app.post("/comm/start")
def start_comm_workflow():
    import uuid
    wf_id = str(uuid.uuid4()).replace("-", "")[:12]
    with SetWorkflowID(wf_id):
        DBOS.start_workflow(communication_workflow)
    return {"workflow_id": wf_id}


@app.post("/comm/approve/{workflow_id}")
def approve_comm(workflow_id: str):
    DBOS.send(workflow_id, "approve", APPROVAL_TOPIC)
    return {"ok": True}


@app.post("/comm/deny/{workflow_id}")
def deny_comm(workflow_id: str):
    DBOS.send(workflow_id, "deny", APPROVAL_TOPIC)
    return {"ok": True}


# ---- Job search (Nationale Vacaturebank) ----
# The HTTP call and the response models live in app/integrations/nvb/; this
# module only wires them into durable workflow steps.


# A step is the unit DBOS checkpoints: if the workflow is interrupted after this
# returns, it resumes without re-issuing the HTTP call. Transient network errors
# are retried with backoff before the step is considered failed.
@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=2)
def fetch_jobs(query: str, page: int = 1, limit: int = 10, sort: str = "date") -> dict:
    return fetch_jobs_page(query, page=page, limit=limit, sort=sort)


@DBOS.step()
def log_jobs(payload: dict) -> int:
    # Parse the raw dict into typed models; see app/integrations/nvb/schema.py.
    page = JobSearchResponse.model_validate(payload)
    log.info(
        "page %s/%s - %s job(s) on this page, %s total",
        page.page,
        page.pages,
        len(page.jobs),
        page.total,
    )

    if not page.jobs:
        log.warning("no jobs in response; top-level keys=%s", list(payload.keys()))

    for index, job in enumerate(page.jobs, start=1):
        log.info(
            "  %2d. %s | %s | %s | %s | posted %s | salary %s",
            index,
            job.title,
            job.company_name,
            job.city,
            job.career_level,
            job.start_date.date() if job.start_date else None,
            job.salary_range or "n/a",
        )
        log.debug("      %s", job.url)
    return len(page.jobs)


@DBOS.step()
def store_jobs(payload: dict) -> dict:
    """Persist a page of vacancies. Idempotent on (source, external_id)."""
    created = updated = 0
    with session_scope() as session:
        for job_payload in to_job_creates(payload):
            _, was_created = job_repo.upsert(session, job_payload)
            created += was_created
            updated += not was_created

    log.info("stored vacancies: %s created, %s updated", created, updated)
    return {"created": created, "updated": updated}


@DBOS.workflow()
def job_search_workflow(query: str, page: int = 1, limit: int = 10, store: bool = True) -> dict:
    logging_setup.section(f"job search: {query}")
    payload = fetch_jobs(query, page=page, limit=limit)
    count = log_jobs(payload)
    result = store_jobs(payload) if store else {"created": 0, "updated": 0}
    log.info("job search finished for %r: %s job(s) logged, %s", query, count, result)
    return {"logged": count, **result}


# ---- Full-query ingest (all pages) ----
# The paging logic lives in app/services/job_ingest.py; here each page fetch and
# each page store is a DBOS step, so an interrupted crawl resumes at the page it
# reached instead of re-fetching everything.


@DBOS.step()
def page_count(payload: dict) -> int:
    """How many pages the first response says exist."""
    return int(payload.get("pages") or 1)


@DBOS.workflow()
def job_ingest_workflow(
    query: str,
    limit: int = DEFAULT_LIMIT,
    max_pages: int | None = None,
) -> dict:
    """Ingest every result page for one query, with its companies and locations."""
    logging_setup.section(f"ingest: {query}")
    stats = IngestStats(query=query)

    payload = fetch_jobs(query, page=1, limit=limit)
    total_pages = page_count(payload)
    stats.total_reported = payload.get("total")
    stats.pages_available = total_pages

    last_page = min(total_pages, max_pages) if max_pages else total_pages
    for page in range(1, last_page + 1):
        if page > 1:
            payload = fetch_jobs(query, page=page, limit=limit)
        logged = log_jobs(payload)
        result = store_jobs(payload)
        stats.pages_fetched += 1
        stats.jobs_seen += logged
        stats.created += result["created"]
        stats.updated += result["updated"]

    log.info("ingest workflow done - %s", stats.summary())
    return stats.as_dict()


@app.post("/jobs/ingest")
def ingest_jobs(body: dict | None = None):
    """Start a durable ingest of every page for a query."""
    body = body or {}
    query = body.get("query", "Odido")
    limit = int(body.get("limit", DEFAULT_LIMIT))
    max_pages = body.get("max_pages")
    handle = DBOS.start_workflow(
        job_ingest_workflow, query, limit, int(max_pages) if max_pages else None
    )
    return {"workflow_id": handle.workflow_id}


@app.get("/jobs/ingest/{workflow_id}")
def ingest_result(workflow_id: str):
    """Poll a running ingest; returns its stats once finished."""
    handle = DBOS.retrieve_workflow(workflow_id)
    status = handle.get_status()
    if status.status != "SUCCESS":
        return {"status": status.status}
    return {"status": status.status, "stats": handle.get_result()}


@app.post("/jobs/search")
def search_jobs(body: dict | None = None):
    body = body or {}
    query = body.get("query", "100 Grams")
    page = int(body.get("page", 1))
    limit = int(body.get("limit", 10))
    store = bool(body.get("store", True))
    handle = DBOS.start_workflow(job_search_workflow, query, page, limit, store)
    return {"workflow_id": handle.workflow_id}


# ---- LLM enrichment (translation + grading) ----
# All model work runs on the `llm` queue, which is pinned to one job at a time
# so the single GPU is never asked to run two models at once. See ADR-0007.


@app.post("/jobs/pipeline")
def start_pipeline(body: dict | None = None):
    """Scrape every page for a query, then translate and grade what was found."""
    body = body or {}
    handle = DBOS.start_workflow(
        enrichment_flow.scrape_and_enrich_workflow,
        body.get("query", "Odido"),
        int(body.get("limit", DEFAULT_LIMIT)),
        int(body["max_pages"]) if body.get("max_pages") else None,
        body.get("target_language", "en"),
        body.get("profile", "default"),
        bool(body.get("enrich", True)),
    )
    return {"workflow_id": handle.workflow_id}


@app.post("/jobs/enrich")
def start_enrichment(body: dict | None = None):
    """Translate and grade every vacancy that needs it, one at a time."""
    body = body or {}
    handle = DBOS.start_workflow(
        enrichment_flow.enrich_pending_workflow,
        int(body.get("limit", 50)),
        body.get("target_language", "en"),
        body.get("profile", "default"),
        bool(body.get("force", False)),
    )
    return {"workflow_id": handle.workflow_id}


@app.post("/jobs/{job_id}/enrich")
def enrich_one(job_id: int, body: dict | None = None):
    """Enrich a single vacancy (still queued, so still one GPU job at a time)."""
    body = body or {}
    handle = DBOS.enqueue_workflow(
        enrichment_flow.LLM_QUEUE,
        enrichment_flow.enrich_job_workflow,
        job_id,
        body.get("target_language", "en"),
        body.get("profile", "default"),
        bool(body.get("force", False)),
    )
    return {"workflow_id": handle.workflow_id}


@app.get("/jobs/enrich/progress")
def enrichment_progress(profile: str = "default"):
    """How much of the corpus is translated and graded, and GPU time spent."""
    with session_scope() as session:
        return enrichment_repo.enrichment_progress(session, profile_name=profile)


@app.get("/jobs/shortlist")
def job_shortlist(
    profile: str = "default",
    min_score: int = 55,
    include_blocked: bool = False,
    limit: int = 25,
):
    """The payoff: best-fitting vacancies with English summaries."""
    with session_scope() as session:
        rows = enrichment_repo.shortlist(
            session,
            profile_name=profile,
            min_score=min_score,
            include_blocked=include_blocked,
            limit=limit,
        )
        return [
            {
                "job_id": job.id,
                "fit_score": assessment.fit_score,
                "verdict": assessment.verdict,
                "blocked": assessment.blocked,
                "dutch_required": assessment.dutch_required,
                "title_en": (translation.title if translation else None) or job.title,
                "title_original": job.title,
                "company": job.company.name if job.company else None,
                "city": job.city,
                "salary": job.salary_range,
                "summary": assessment.summary,
                "red_flags": assessment.red_flags,
                "url": job.detail_url,
            }
            for job, assessment, translation in rows
        ]


@app.get("/jobs/{job_id}/english")
def job_in_english(job_id: int):
    """One vacancy, translated - the reason this pipeline exists."""
    with session_scope() as session:
        job = job_repo.get(session, job_id)
        if job is None:
            return {"error": "job not found"}
        translation = enrichment_repo.get_translation(session, job_id, "en")
        return {
            "job_id": job.id,
            "title_original": job.title,
            "title_en": translation.title if translation else None,
            "description_en": translation.description if translation else None,
            "translated": translation is not None,
            "model": translation.model if translation else None,
            "company": job.company.name if job.company else None,
            "city": job.city,
            "url": job.detail_url,
        }


if __name__ == "__main__":
    DBOS.launch()
    # Queues are registered after launch: register_queue writes the queue's
    # concurrency limits to the system database, which is only open post-launch.
    # Nothing is enqueued before this point, so the limits are in force for all
    # work this process starts.
    enrichment_flow.register_queues()
    DBOS.register_queue(QUEUE_NAME, worker_concurrency=DEFAULT_WORKER_CONCURRENCY, on_conflict="never_update")
    uvicorn.run(app, host="0.0.0.0", port=8000)
