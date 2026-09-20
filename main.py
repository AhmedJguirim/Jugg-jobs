import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
import uvicorn
from dbos import DBOS, DBOSConfig, SetWorkflowID
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import dev_log
from dev_log import log

# Welcome to DBOS!
# This example shows you how to use DBOS to build applications
# that are resilient to any failure.

app = FastAPI()
config: DBOSConfig = {
    "name": "dbos-app-starter",
    "system_database_url": os.environ.get("DBOS_SYSTEM_DATABASE_URL"),
    "application_version": "0.1.0",
    "conductor_key": os.environ.get("DBOS_CONDUCTOR_KEY"),
}
DBOS(config=config)

# Mirror DBOS/uvicorn logs into logs/dev.log alongside our own dev logger.
dev_log.attach("dbos", "uvicorn.error")
dev_log.section("app start")

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


# This endpoint serves the HTML frontend.
@app.get("/")
def readme():
    with open(os.path.join("html", "app.html"), encoding="utf-8") as file:
        html = file.read()
    return HTMLResponse(html)


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

NVB_URL = "https://api.nationalevacaturebank.nl/api/jobs/v3/sites/nationalevacaturebank.nl/jobs"
NVB_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nationalevacaturebank.nl/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


# A step is the unit DBOS checkpoints: if the workflow is interrupted after this
# returns, it resumes without re-issuing the HTTP call. Transient network errors
# are retried with backoff before the step is considered failed.
@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=2)
def fetch_jobs(query: str, page: int = 1, limit: int = 10, sort: str = "date") -> dict:
    params = {"page": page, "limit": limit, "sort": sort, "query": query}
    log.info("GET %s params=%s", NVB_URL, params)

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(NVB_URL, params=params, headers=NVB_HEADERS)

    log.info(
        "response status=%s elapsed=%.2fs bytes=%s",
        response.status_code,
        response.elapsed.total_seconds(),
        len(response.content),
    )
    response.raise_for_status()
    return response.json()


@DBOS.step()
def log_jobs(payload: dict) -> int:
    # The API is HAL-shaped: the list lives under _embedded.jobs.
    jobs = payload.get("_embedded", {}).get("jobs", [])
    log.info(
        "page %s/%s - %s job(s) on this page, %s total",
        payload.get("page"),
        payload.get("pages"),
        len(jobs),
        payload.get("total"),
    )

    if not jobs:
        log.warning("no jobs in response; top-level keys=%s", list(payload.keys()))

    for index, job in enumerate(jobs, start=1):
        location = job.get("workLocation") or {}
        log.info(
            "  %2d. %s | %s | %s | posted %s | %s",
            index,
            job.get("title"),
            (job.get("company") or {}).get("name"),
            location.get("city"),
            (job.get("startDate") or "")[:10],
            job.get("_links", {}).get("self", {}).get("href"),
        )
    return len(jobs)


@DBOS.workflow()
def job_search_workflow(query: str, page: int = 1, limit: int = 10) -> int:
    dev_log.section(f"job search: {query}")
    payload = fetch_jobs(query, page=page, limit=limit)
    count = log_jobs(payload)
    log.info("job search finished for %r: %s job(s) logged", query, count)
    return count


@app.post("/jobs/search")
def search_jobs(body: dict | None = None):
    body = body or {}
    query = body.get("query", "100 Grams")
    page = int(body.get("page", 1))
    limit = int(body.get("limit", 10))
    handle = DBOS.start_workflow(job_search_workflow, query, page, limit)
    return {"workflow_id": handle.workflow_id}


if __name__ == "__main__":
    DBOS.launch()
    DBOS.register_queue(QUEUE_NAME, worker_concurrency=DEFAULT_WORKER_CONCURRENCY, on_conflict="never_update")
    uvicorn.run(app, host="0.0.0.0", port=8000)
