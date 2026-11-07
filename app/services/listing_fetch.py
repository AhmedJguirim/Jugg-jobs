"""Fetch the HTML of collected job-posting links (ADR-0023, ADR-0024).

    fetch_listing(url)   one link -> an outcome dict, no database
    fetch_pending(...)   the experiment: outcomes go to `job_listings_test`, nothing
                         else is touched
    process_link(url)    the real flow: fetch one link and store the page as a raw
                         listing, i.e. put it on the waiting list (the button on
                         /workspace/urls; `app/workflows/listing_fetch.py`)

Neither grades anything: grading stays on demand (ADR-0016). Requests are spaced
by a random 1-3 seconds so a run over many links does not hit a host in a burst.
"""

from __future__ import annotations

import random
import time
from contextlib import AbstractContextManager
from typing import Any, Callable, Optional

import httpx

from app.db.session import session_scope
from app.integrations.listing_pages import extract
from app.integrations.listing_pages.client import Page, fetch_page
from app.parsers import PLATFORM_HOSTS, detect_platform
from app.repositories import job_listing_test as repo
from app.repositories import manual_job_listing_url as url_repo
from app.repositories import raw_job_listing as raw_repo
from app.schemas.raw_job_listing import RawJobListingCreate
from sqlalchemy.orm import Session

MIN_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 3.0

_GONE = (404, 410)


def random_delay() -> float:
    return random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)


def paced(
    fetch: Callable[..., Page],
    sleep: Callable[[float], None] = time.sleep,
    delay: Callable[[], float] = random_delay,
    *,
    delay_first: bool = False,
) -> Callable[..., Page]:
    """`fetch` with a pause before every request except the first one made
    (`delay_first`: before that one too, when an earlier request was just made).

    Counted per request, not per link: a platform may make several requests for
    one link (Workable asks for the job and then for the company).
    """
    made = [delay_first]

    def get(url: str, **kwargs: Any) -> Page:
        if made[0]:
            sleep(delay())
        made[0] = True
        return fetch(url, **kwargs)

    return get


def fetch_listing(url: str, *, fetch: Callable[..., Page] = fetch_page) -> dict[str, Any]:
    """Request one link and say how it went. Never raises for a bad answer.

    `requested` is False when no request was made (an unsupported site).
    `fetch(url, json=False)` makes one request; it is called once per request
    the platform needs, so a caller wanting a pause between them passes `paced`.
    """
    outcome: dict[str, Any] = {
        "url": url, "status": repo.UNSUPPORTED, "html_body": "",
        "http_status": None, "page_bytes": None, "note": None, "requested": False,
    }
    platform = detect_platform(url)
    if not extract.supported(platform):
        outcome["note"] = (
            "no parser for this url's host" if platform is None else f"no fetcher for {platform}"
        )
        return outcome

    outcome["requested"] = True
    try:
        page = extract.request(platform, url, fetch)
        outcome["http_status"] = page.status_code
        outcome["page_bytes"] = page.size

        if page.status_code in _GONE or (page.status_code == 200 and extract.is_gone(platform, page)):
            # Kept, so the parser reads the platform's "job gone" page as a result.
            return {
                **outcome, "status": repo.NOT_FOUND,
                "html_body": extract.gone_page(platform, page), "note": "the server says it is gone",
            }
        if page.status_code != 200:
            return {**outcome, "status": repo.BLOCKED, "note": f"HTTP {page.status_code}"}

        block = extract.extract_posting(platform, page, fetch)
    except httpx.TransportError as error:
        return {**outcome, "status": repo.ERROR, "note": f"{type(error).__name__}: {error}"}

    if block is None:
        return {**outcome, "status": repo.NO_CONTENT, "note": "the page has no posting block"}
    return {**outcome, "status": repo.FETCHED, "html_body": block}


def fetch_pending(
    limit: int,
    *,
    retry: bool = False,
    include_pasted: bool = False,
    platform: Optional[str] = None,
    fetch: Callable[..., Page] = fetch_page,
    sleep: Callable[[float], None] = time.sleep,
    delay: Callable[[], float] = random_delay,
    open_session: Callable[[], AbstractContextManager[Session]] = session_scope,
    on_result: Optional[Callable[[dict[str, Any]], None]] = None,
) -> list[dict[str, Any]]:
    """Fetch up to `limit` links that have not been fetched yet.

    Each outcome is committed as soon as it is known, so stopping a long run
    loses nothing already done. A random pause comes before every request after
    the first, never after the last, and not around links that make no request.
    `platform` (a name from `PLATFORM_HOSTS`) keeps only that platform's links.
    """
    with open_session() as session:
        hosts = [host for host, name in PLATFORM_HOSTS.items() if name == platform] if platform else None
        urls = repo.pending_urls(session, limit, retry=retry, include_pasted=include_pasted, hosts=hosts)

    get = paced(fetch, sleep, delay)
    results: list[dict[str, Any]] = []
    for url in urls:
        outcome = fetch_listing(url, fetch=get)
        with open_session() as session:
            repo.upsert(
                session, url,
                status=outcome["status"], html_body=outcome["html_body"],
                http_status=outcome["http_status"], page_bytes=outcome["page_bytes"],
                note=outcome["note"],
            )
        results.append(outcome)
        if on_result is not None:
            on_result(outcome)
    return results


# What `process_link` did with a link.
ADDED = "added"      # stored as a raw listing: it waits to be graded
GONE = "gone"        # the platform says it was removed: stored as not_found, never listed
RETRY = "retry"      # blocked, failed or empty: nothing stored, the link stays waiting
EXISTS = "exists"    # a raw listing for it appeared meanwhile: left alone


def fetchable_hosts() -> list[str]:
    """Hosts of every platform we can fetch, for picking links to process."""
    return [host for host, name in PLATFORM_HOSTS.items() if extract.supported(name)]


def store_as_raw(session: Session, outcome: dict[str, Any]) -> str:
    """Put a fetched page on the waiting list. Only a page that is usable (or
    known to be gone) is stored: a refusal or a timeout leaves the link without
    HTML so the next run tries it again."""
    url = outcome["url"]
    if url_repo.processed_urls(session, [url]):
        return EXISTS
    if outcome["status"] == repo.FETCHED:
        raw_repo.create(session, RawJobListingCreate(url=url, html_body=outcome["html_body"]))
        return ADDED
    if outcome["status"] == repo.NOT_FOUND:
        listing = raw_repo.create(session, RawJobListingCreate(url=url, html_body=outcome["html_body"]))
        raw_repo.mark(session, listing.id, raw_repo.NOT_FOUND)
        return GONE
    return RETRY


def process_link(
    url: str,
    *,
    pause: bool = False,
    fetch: Callable[..., Page] = fetch_page,
    sleep: Callable[[float], None] = time.sleep,
    delay: Callable[[], float] = random_delay,
    open_session: Callable[[], AbstractContextManager[Session]] = session_scope,
) -> dict[str, Any]:
    """Fetch one link and store the page as a raw listing.

    `pause`: the caller made a request just before this one, so wait first. Every
    further request inside this call (a platform may need several) is paced too.
    Returns a small plain dict, safe to checkpoint.
    """
    outcome = fetch_listing(url, fetch=paced(fetch, sleep, delay, delay_first=pause))
    with open_session() as session:
        action = store_as_raw(session, outcome)
    return {
        "url": url, "status": outcome["status"], "action": action,
        "note": outcome["note"], "requested": outcome["requested"],
    }
