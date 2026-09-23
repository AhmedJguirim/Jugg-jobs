"""Ingest every page of a job-board search into the database.

This is the flow layer: it owns *what happens in what order* (walk the pages,
map each vacancy, upsert it with its company and location), while the client,
mapper and repository each own one step of it.

Kept free of DBOS so the same code runs from a durable workflow, a CLI script,
or a test with a stubbed fetcher.

    stats = ingest_query("Odido", limit=50)
    print(stats.created, stats.updated)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.company import Company
from app.db.models.location import Location
from app.db.session import session_scope
from app.integrations.nvb import fetch_jobs_page, to_job_creates
from app.logging_setup import log
from app.repositories import job as job_repo

# Page size the API accepts comfortably; 50 was verified against the live API.
DEFAULT_LIMIT = 50

# Courtesy pause between page requests so a wide crawl does not hammer the board.
DEFAULT_DELAY_SECONDS = 0.3


class PageFetcher(Protocol):
    """A callable returning one raw search-result page."""

    def __call__(self, query: str, *, page: int, limit: int, sort: str) -> dict: ...


@dataclass
class IngestStats:
    """Outcome of one ingest run."""

    query: str
    total_reported: Optional[int] = None
    pages_available: Optional[int] = None
    pages_fetched: int = 0
    jobs_seen: int = 0
    created: int = 0
    updated: int = 0
    companies_created: int = 0
    locations_created: int = 0
    failed_pages: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed_pages

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "total_reported": self.total_reported,
            "pages_available": self.pages_available,
            "pages_fetched": self.pages_fetched,
            "jobs_seen": self.jobs_seen,
            "created": self.created,
            "updated": self.updated,
            "companies_created": self.companies_created,
            "locations_created": self.locations_created,
            "failed_pages": self.failed_pages,
        }

    def summary(self) -> str:
        return (
            f"{self.query!r}: {self.jobs_seen} vacancies over {self.pages_fetched} page(s) "
            f"-> {self.created} created, {self.updated} updated, "
            f"{self.companies_created} new employer(s), {self.locations_created} new location(s)"
            + (f", FAILED pages: {self.failed_pages}" if self.failed_pages else "")
        )


def _default_fetch(query: str, *, page: int, limit: int, sort: str) -> dict:
    return fetch_jobs_page(query, page=page, limit=limit, sort=sort)


def store_page(session: Session, payload: dict) -> tuple[int, int]:
    """Upsert every vacancy on one page. Returns (created, updated).

    Companies and locations are created as a side effect by the repository, so
    a vacancy always arrives with its employer attached.
    """
    created = updated = 0
    for job_payload in to_job_creates(payload):
        _, was_created = job_repo.upsert(session, job_payload)
        created += was_created
        updated += not was_created
    return created, updated


def ingest_query(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    max_pages: Optional[int] = None,
    sort: str = "date",
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    fetch: Optional[PageFetcher] = None,
    session_factory: Callable = session_scope,
) -> IngestStats:
    """Walk every result page for `query` and persist what it finds.

    The first response reports how many pages exist, so the page count is not
    guessed. A page that fails is recorded in `failed_pages` and the walk
    continues - one bad page should not discard the rest of the crawl.

    Each page commits in its own transaction, so an interruption leaves the
    pages already ingested intact rather than rolling back the whole crawl.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be >= 1 when given")

    fetch = fetch or _default_fetch
    stats = IngestStats(query=query)

    with session_factory() as session:
        companies_before = session.scalar(select(func.count()).select_from(Company)) or 0
        locations_before = session.scalar(select(func.count()).select_from(Location)) or 0

    page = 1
    while True:
        try:
            payload = fetch(query, page=page, limit=limit, sort=sort)
        except Exception:
            log.exception("ingest %r: page %s failed to fetch", query, page)
            stats.failed_pages.append(page)
            break

        if page == 1:
            stats.total_reported = payload.get("total")
            stats.pages_available = payload.get("pages")
            log.info(
                "ingest %r: %s vacancies across %s page(s) at limit=%s",
                query,
                stats.total_reported,
                stats.pages_available,
                limit,
            )

        jobs_on_page = len(payload.get("_embedded", {}).get("jobs", []))
        try:
            with session_factory() as session:
                created, updated = store_page(session, payload)
        except Exception:
            log.exception("ingest %r: page %s failed to store", query, page)
            stats.failed_pages.append(page)
        else:
            stats.created += created
            stats.updated += updated
            stats.jobs_seen += jobs_on_page
            log.info(
                "ingest %r: page %s/%s - %s vacancies (%s created, %s updated)",
                query,
                page,
                stats.pages_available,
                jobs_on_page,
                created,
                updated,
            )

        stats.pages_fetched += 1

        last_page = stats.pages_available or 1
        if max_pages is not None and stats.pages_fetched >= max_pages:
            break
        if page >= last_page or jobs_on_page == 0:
            break

        page += 1
        if delay_seconds:
            time.sleep(delay_seconds)

    with session_factory() as session:
        companies_after = session.scalar(select(func.count()).select_from(Company)) or 0
        locations_after = session.scalar(select(func.count()).select_from(Location)) or 0
    stats.companies_created = companies_after - companies_before
    stats.locations_created = locations_after - locations_before

    log.info("ingest done - %s", stats.summary())
    return stats
