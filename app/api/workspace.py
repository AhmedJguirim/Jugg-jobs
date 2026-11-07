"""HTTP API behind the workspace section: raw job listings.

Listing responses leave the HTML body out; they are whole pages and the list
only needs to show what was submitted. `GET /api/workspace/raw-job-listings/{id}`
returns the body, with its size, for when something needs it.
"""

from __future__ import annotations

from dbos import DBOS
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db.session import session_scope
from app.repositories import manual_job_listing_url as url_repo
from app.repositories import raw_job_listing as repo
from app.repositories.raw_job_listing import RawJobListingNotFound
from app.schemas.raw_job_listing import RawJobListingCreate
from app.text.search_results import extract_result_urls
from app.config import get_settings
from app.parsers import detect_platform
from app.services import listing_fetch as fetching
from app.services import listing_progress
from app.workflows import listing_fetch as fetch_flow
from app.workflows import listing_grading as grading_flow
from app.workflows.llm_queue import STAGE_EVENT

router = APIRouter(tags=["workspace"])


class RawJobListingIn(BaseModel):
    url: str = Field(min_length=1)
    html_body: str = Field(min_length=1)


def _summary(listing, body_chars: int | None = None) -> dict:
    row = {
        "id": listing.id,
        "url": listing.url,
        "created_at": listing.created_at.isoformat() if listing.created_at else None,
        "status": listing.status,
        "job_id": listing.job_id,
    }
    if body_chars is not None:
        row["body_chars"] = body_chars
    return row


def _start_processing(listing_id: int, profile: str | None = None, force: bool = False) -> str:
    """Start the grade-and-create workflow for one stored listing."""
    handle = DBOS.start_workflow(grading_flow.process_listing_workflow, listing_id, profile, force)
    return handle.workflow_id


@router.post("/api/workspace/raw-job-listings", status_code=201)
def submit_raw_job_listing(body: RawJobListingIn):
    if not body.url.strip() or not body.html_body.strip():
        raise HTTPException(status_code=422, detail="url and html_body must not be blank")
    # Saving only stores. Grading is a separate, deliberate step (the Grade
    # button or a batch), because it takes the GPU for minutes. A url on a site
    # with no parser can never be graded, so it is marked and never listed.
    status = None if detect_platform(body.url.strip()) else repo.UNSUPPORTED
    with session_scope() as session:
        listing = repo.create(
            session, RawJobListingCreate(url=body.url, html_body=body.html_body), status=status
        )
        return _summary(listing, len(listing.html_body))


# Not under /raw-job-listings/...: a path segment there is read as a listing id.
@router.get("/api/workspace/raw-job-listings-cleanup")
def graded_html_stats():
    """How much stored HTML belongs to listings that are already graded."""
    with session_scope() as session:
        listings, size = repo.graded_html_stats(session)
        return {"listings": listings, "bytes": size}


@router.post("/api/workspace/raw-job-listings-cleanup")
def cleanup_graded_html():
    """Empty the stored HTML of graded listings. Their rows, links and jobs stay."""
    with session_scope() as session:
        listings, size = repo.clear_graded_html(session)
        return {"cleaned": listings, "bytes_freed": size}


@router.get("/api/workspace/raw-job-listings")
def list_raw_job_listings(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    waiting: bool = False,
):
    """`waiting=true`: only the listings still to grade, oldest first."""
    with session_scope() as session:
        rows = repo.list_recent(session, limit=limit, offset=offset, waiting=waiting)
        return {
            "total": repo.count(session, waiting=waiting),
            "items": [_summary(row) for row in rows],
        }


@router.get("/api/workspace/raw-job-listings/{listing_id}")
def get_raw_job_listing(listing_id: int):
    with session_scope() as session:
        try:
            listing = repo.get(session, listing_id)
        except RawJobListingNotFound:
            raise HTTPException(status_code=404, detail="raw job listing not found")
        return {**_summary(listing, len(listing.html_body)), "html_body": listing.html_body}


@router.post("/api/workspace/raw-job-listings/{listing_id}/remove")
def remove_raw_job_listing(listing_id: int):
    """Drop a listing - and its link - for good, e.g. a page that can never be read.

    The listing leaves the waiting list and the link leaves the collected URLs
    (it is kept as removed, so pasting the same Google results again does not
    bring it back). Nothing is deleted from the database.
    """
    with session_scope() as session:
        try:
            listing = repo.remove(session, listing_id)
        except RawJobListingNotFound:
            raise HTTPException(status_code=404, detail="raw job listing not found")
        url_repo.remove(session, listing.url)
        return {"id": listing_id, "removed": True}


@router.post("/api/workspace/raw-job-listings/{listing_id}/process", status_code=202)
def process_raw_job_listing(listing_id: int, profile: str | None = None, force: bool = False):
    """Start the workflow that parses, grades and creates a job from one stored listing."""
    with session_scope() as session:
        try:
            repo.get(session, listing_id)
        except RawJobListingNotFound:
            raise HTTPException(status_code=404, detail="raw job listing not found")
    return {"workflow_id": _start_processing(listing_id, profile, force)}


def _grading_entry(workflow_id: str, listing: dict) -> dict:
    """Where one listing's grading run has got to, as one readable line.

    Progress is read from the run's DBOS status and from its grading step (whose
    id is derived from the run's), so no extra bookkeeping is stored.
    """
    status = DBOS.get_workflow_status(workflow_id)
    state = listing_progress.state_of(status.status if status else None)
    entry: dict = {**listing, "workflow_id": workflow_id, "state": state, "problem": False}

    if state == "waiting":
        entry["label"] = "waiting for a free slot"
    elif state == "running":
        grade_id = grading_flow.grade_child_id(workflow_id)
        grade = DBOS.get_workflow_status(grade_id)
        grade_state = listing_progress.state_of(grade.status) if grade else None
        stage = DBOS.get_event(grade_id, STAGE_EVENT, timeout_seconds=0) if grade_state == "running" else None
        entry["label"] = listing_progress.running_label(grade_state, stage)
    elif state == "done":
        result = DBOS.retrieve_workflow(workflow_id).get_result()
        entry["label"], entry["problem"] = listing_progress.finished_label(result)
        entry["result"] = result
    elif state == "cancelled":
        entry["label"] = "cancelled - nothing was saved for this listing"
    else:
        entry["label"], entry["problem"] = "failed - see the logs", True
    return entry


@router.get("/api/workspace/grading/{workflow_id}")
def grading_status(workflow_id: str):
    """Poll one listing's grading run (started by its Grade button)."""
    if DBOS.get_workflow_status(workflow_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _grading_entry(workflow_id, {})


@router.post("/api/workspace/grading/{workflow_id}/cancel")
def cancel_grading(workflow_id: str):
    """Cancel a run or a batch, and the grading steps it queued."""
    if DBOS.get_workflow_status(workflow_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    DBOS.cancel_workflow(workflow_id, cancel_children=True)
    return {"workflow_id": workflow_id, "cancelled": True}


class GradeBatch(BaseModel):
    count: int = Field(default=10, ge=1, le=grading_flow.MAX_BATCH)
    profile: str | None = None


@router.post("/api/workspace/grading-batch")
def start_grading_batch(body: GradeBatch):
    """Grade the first `count` waiting listings, one after another on the GPU.

    Each runs the same flow as its own Grade button. Returns at once with the
    batch's id and the listings it picked.
    """
    with session_scope() as session:
        rows = repo.list_recent(session, limit=body.count, offset=0, waiting=True)
        listings = [{"id": row.id, "url": row.url} for row in rows]
    if not listings:
        raise HTTPException(status_code=400, detail="no listing is waiting to be graded")
    profile = body.profile or get_settings().llm_default_profile
    handle = DBOS.start_workflow(grading_flow.grade_listings_workflow, listings, profile)
    return {"workflow_id": handle.workflow_id, "listings": listings}


@router.get("/api/workspace/grading-batch/{batch_id}")
def grading_batch_status(batch_id: str):
    """Poll a batch: its own status plus one progress line per listing."""
    status = DBOS.get_workflow_status(batch_id)
    if status is None:
        raise HTTPException(status_code=404, detail="batch not found")
    listings = DBOS.get_event(batch_id, "listings", timeout_seconds=0) or []
    rows = [_grading_entry(grading_flow.batch_child_id(batch_id, item["id"]), item) for item in listings]
    return {
        "workflow_id": batch_id,
        "status": status.status,
        "listings": rows,
        "counts": listing_progress.batch_summary(rows),
    }


class SearchResultsIn(BaseModel):
    html: str = Field(min_length=1)


@router.post("/api/workspace/manual-job-listing-urls/extract")
def extract_manual_job_listing_urls(body: SearchResultsIn):
    """Parse a pasted Google `#search` block and store the new result links."""
    urls = extract_result_urls(body.html)
    if not urls:
        raise HTTPException(status_code=422, detail="no result links found in the pasted HTML")
    with session_scope() as session:
        added = url_repo.add_many(session, urls)
    added_set = set(added)
    return {
        "found": len(urls),
        "added": len(added),
        "skipped": len(urls) - len(added),
        "urls": [{"url": url, "added": url in added_set} for url in urls],
    }


class UrlIn(BaseModel):
    url: str = Field(min_length=1)


@router.post("/api/workspace/manual-job-listing-urls/remove")
def remove_manual_job_listing_url(body: UrlIn):
    """Remove a collected link, so it is never offered for processing again."""
    with session_scope() as session:
        return {"removed": url_repo.remove(session, body.url.strip())}


@router.get("/api/workspace/manual-job-listing-urls/next")
def next_manual_job_listing_url(skip: int = Query(0, ge=0)):
    """The next collected URL still waiting for its HTML, and how many wait."""
    with session_scope() as session:
        row = url_repo.next_unprocessed(session, skip=skip)
        return {
            "url": row.url if row else None,
            "remaining": url_repo.count_unprocessed(session),
        }


def _active_fetch_run() -> str | None:
    """Id of a "Process all" run that has not finished, if there is one."""
    runs = DBOS.list_workflows(
        queue_name=fetch_flow.FETCH_QUEUE, status=["ENQUEUED", "PENDING"],
        limit=1, load_input=False, load_output=False,
    )
    return runs[0].workflow_id if runs else None


@router.get("/api/workspace/manual-job-listing-urls/process-all")
def active_process_all():
    """The run in progress, so the page can pick it up again after a reload."""
    return {"workflow_id": _active_fetch_run()}


@router.post("/api/workspace/manual-job-listing-urls/process-all", status_code=202)
def start_process_all():
    """Fetch the page of every collected link that has none and put it on the waiting list.

    Only links on a platform we can fetch are taken (up to `MAX_PER_RUN`, oldest
    first). Nothing is graded: the pages wait for the Grade buttons. Returns at
    once; poll the run for progress. One run at a time.
    """
    running = _active_fetch_run()
    if running:
        raise HTTPException(status_code=409, detail="a run is already in progress")
    with session_scope() as session:
        urls = url_repo.unprocessed_urls(session, fetch_flow.MAX_PER_RUN, fetching.fetchable_hosts())
    if not urls:
        raise HTTPException(status_code=400, detail="no collected link is waiting for its page")
    handle = DBOS.enqueue_workflow(fetch_flow.FETCH_QUEUE, fetch_flow.fetch_listings_workflow, urls)
    return {"workflow_id": handle.workflow_id, "total": len(urls)}


@router.get("/api/workspace/manual-job-listing-urls/process-all/{workflow_id}")
def process_all_status(workflow_id: str):
    """Where a run has got to: its state and the counts it publishes as it goes."""
    status = DBOS.get_workflow_status(workflow_id)
    if status is None:
        raise HTTPException(status_code=404, detail="run not found")
    progress = DBOS.get_event(workflow_id, fetch_flow.PROGRESS_EVENT, timeout_seconds=0)
    return {"workflow_id": workflow_id, "state": listing_progress.state_of(status.status), "progress": progress}


@router.post("/api/workspace/manual-job-listing-urls/process-all/{workflow_id}/cancel")
def cancel_process_all(workflow_id: str):
    """Stop a run after the link it is on. Pages already stored stay."""
    if DBOS.get_workflow_status(workflow_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    DBOS.cancel_workflow(workflow_id)
    return {"workflow_id": workflow_id, "cancelled": True}


@router.get("/api/workspace/manual-job-listing-urls")
def list_manual_job_listing_urls(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    hide_processed: bool = False,
):
    with session_scope() as session:
        rows = url_repo.list_recent(session, limit=limit, offset=offset, only_unprocessed=hide_processed)
        processed = url_repo.processed_urls(session, [row.url for row in rows])
        return {
            "total": url_repo.count(session, only_unprocessed=hide_processed),
            # How many of the waiting links "Process all" can fetch (their site has a fetcher).
            "fetchable": url_repo.count_unprocessed(session, fetching.fetchable_hosts()),
            "items": [
                {
                    "id": row.id,
                    "url": row.url,
                    "scraped_at": row.scraped_at.isoformat() if row.scraped_at else None,
                    # True once its HTML has been stored (a raw listing with this url).
                    "processed": row.url in processed,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ],
        }
