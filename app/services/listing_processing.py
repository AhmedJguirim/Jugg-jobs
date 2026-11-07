"""Read one raw job listing: detect its platform and parse it into a job.

The two stages are separate functions with plain-dict results so the DBOS
workflow (`app/workflows/listing_grading.py`) can checkpoint each one, and so
tests can call them without DBOS.

    load_listing -> parse

Outcomes (`status` in the result):

- `stored`            the page parsed into a job (`job`, ready to be graded)
- `not_found`         the platform says the posting no longer exists; nothing stored
- `unsupported`       the URL belongs to no platform we have a parser for
- `unparseable`       right platform, but the HTML is not a job page we can read
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.parsers import ListingNotFound, ListingParseError, detect_platform, parse_listing
from app.repositories import raw_job_listing as raw_repo

STORED = "stored"
NOT_FOUND = "not_found"
UNSUPPORTED = "unsupported"
UNPARSEABLE = "unparseable"
NO_HTML = "no_html"


def load_listing(session: Session, raw_listing_id: int) -> dict[str, Any]:
    listing = raw_repo.get(session, raw_listing_id)
    return {"id": listing.id, "url": listing.url, "html": listing.html_body}


def parse(listing: dict[str, Any]) -> dict[str, Any]:
    """Detect the platform and parse. Never raises for bad input: it reports."""
    base = {"raw_listing_id": listing["id"], "url": listing["url"]}

    # A graded listing's HTML may have been cleaned up. That is not an unreadable
    # page: it must not be marked as one (which would put a graded listing back on
    # the waiting list), so it gets its own status that nothing records.
    if not (listing.get("html") or "").strip():
        return {
            **base,
            "status": NO_HTML,
            "platform": None,
            "reason": "its HTML was cleaned up after grading; paste the page again to re-grade it",
        }

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
