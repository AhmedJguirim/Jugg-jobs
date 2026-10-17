"""Process one raw job listing: detect its platform, parse it, store the job.

The three stages are separate functions with plain-dict results so the DBOS
workflow (`app/workflows/listing_processing.py`) can checkpoint each one, and so
tests can call them without DBOS.

    load_listing -> parse -> store

Outcomes (`status` in the final result):

- `stored`            the job was parsed and written to `jobs_test`
- `not_found`         the platform says the posting no longer exists; nothing stored
- `unsupported`       the URL belongs to no platform we have a parser for
- `unparseable`       right platform, but the HTML is not a job page we can read
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.parsers import ListingNotFound, ListingParseError, detect_platform, parse_listing
from app.repositories import job_test as job_test_repo
from app.repositories import raw_job_listing as raw_repo
from app.schemas.job import JobCreate

STORED = "stored"
NOT_FOUND = "not_found"
UNSUPPORTED = "unsupported"
UNPARSEABLE = "unparseable"


def load_listing(session: Session, raw_listing_id: int) -> dict[str, Any]:
    listing = raw_repo.get(session, raw_listing_id)
    return {"id": listing.id, "url": listing.url, "html": listing.html_body}


def parse(listing: dict[str, Any]) -> dict[str, Any]:
    """Detect the platform and parse. Never raises for bad input: it reports."""
    base = {"raw_listing_id": listing["id"], "url": listing["url"]}

    platform = detect_platform(listing["url"])
    if platform is None:
        return {**base, "status": UNSUPPORTED, "platform": None, "reason": "no parser for this url's host"}

    try:
        result = parse_listing(platform, listing["url"], listing["html"])
    except ListingParseError as error:
        return {**base, "status": UNPARSEABLE, "platform": platform, "reason": str(error)}

    if isinstance(result, ListingNotFound):
        return {**base, "status": NOT_FOUND, "platform": platform, "reason": result.reason}

    job = result.job.model_copy(deep=True)
    # Where the job came from, so a stored row can be traced to its raw page.
    job.raw = {**(job.raw or {}), "raw_listing_id": listing["id"]}
    return {**base, "status": STORED, "platform": platform, "job": job.model_dump(mode="json")}


def store(session: Session, parsed: dict[str, Any]) -> dict[str, Any]:
    """Write a parsed job to `jobs_test`; pass any other outcome through."""
    if parsed["status"] != STORED:
        return parsed
    job, created = job_test_repo.upsert(session, JobCreate.model_validate(parsed["job"]))
    return {
        "raw_listing_id": parsed["raw_listing_id"],
        "url": parsed["url"],
        "status": STORED,
        "platform": parsed["platform"],
        "job_test_id": job.id,
        "created": created,
        "title": job.title,
    }


def process(session: Session, raw_listing_id: int) -> dict[str, Any]:
    """All three stages in one go (no DBOS), for tests and one-off use."""
    return store(session, parse(load_listing(session, raw_listing_id)))
