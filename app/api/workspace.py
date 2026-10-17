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
from app.parsers import detect_platform
from app.workflows.listing_grading import process_listing_workflow

router = APIRouter(tags=["workspace"])


class RawJobListingIn(BaseModel):
    url: str = Field(min_length=1)
    html_body: str = Field(min_length=1)


def _summary(listing, body_chars: int | None = None) -> dict:
    row = {
        "id": listing.id,
        "url": listing.url,
        "created_at": listing.created_at.isoformat() if listing.created_at else None,
    }
    if body_chars is not None:
        row["body_chars"] = body_chars
    return row


def _start_processing(listing_id: int, profile: str | None = None, force: bool = False) -> str:
    """Start the grade-and-create workflow for one stored listing."""
    handle = DBOS.start_workflow(process_listing_workflow, listing_id, profile, force)
    return handle.workflow_id


@router.post("/api/workspace/raw-job-listings", status_code=201)
def submit_raw_job_listing(body: RawJobListingIn):
    if not body.url.strip() or not body.html_body.strip():
        raise HTTPException(status_code=422, detail="url and html_body must not be blank")
    with session_scope() as session:
        listing = repo.create(
            session, RawJobListingCreate(url=body.url, html_body=body.html_body)
        )
        summary = _summary(listing, len(listing.html_body))
    # After the commit above, so the workflow can read the row it is given.
    # A url on a platform with a parser is graded and turned into a job; any
    # other (including Nationale Vacaturebank, which has its own pipeline) is
    # only stored.
    if detect_platform(summary["url"]):
        summary["workflow_id"] = _start_processing(summary["id"])
    return summary


@router.get("/api/workspace/raw-job-listings")
def list_raw_job_listings(
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
):
    with session_scope() as session:
        rows = repo.list_recent(session, limit=limit, offset=offset)
        return {
            "total": repo.count(session),
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


@router.post("/api/workspace/raw-job-listings/{listing_id}/process", status_code=202)
def process_raw_job_listing(listing_id: int, profile: str | None = None, force: bool = False):
    """Start the workflow that parses, grades and creates a job from one stored listing."""
    with session_scope() as session:
        try:
            repo.get(session, listing_id)
        except RawJobListingNotFound:
            raise HTTPException(status_code=404, detail="raw job listing not found")
    return {"workflow_id": _start_processing(listing_id, profile, force)}


@router.get("/api/workspace/raw-job-listings/process/{workflow_id}")
def process_raw_job_listing_result(workflow_id: str):
    """Poll a processing run; the result carries its `status` once finished."""
    handle = DBOS.retrieve_workflow(workflow_id)
    status = handle.get_status().status
    if status != "SUCCESS":
        return {"status": status}
    return {"status": status, "result": handle.get_result()}


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


@router.get("/api/workspace/manual-job-listing-urls/next")
def next_manual_job_listing_url(skip: int = Query(0, ge=0)):
    """The next collected URL still waiting for its HTML, and how many wait."""
    with session_scope() as session:
        row = url_repo.next_unprocessed(session, skip=skip)
        return {
            "url": row.url if row else None,
            "remaining": url_repo.count_unprocessed(session),
        }


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
