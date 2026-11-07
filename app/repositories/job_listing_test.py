"""Storage for fetched job-listing pages (`job_listings_test`).

Same contract as the other repositories: explicit `Session`, `flush()` but never
`commit()`.
"""

from typing import Optional, Sequence

from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, load_only

from app.db.models.job_listing_test import JobListingTest
from app.db.models.manual_job_listing_url import ManualJobListingUrl
from app.db.models.raw_job_listing import RawJobListing

FETCHED = "fetched"
NOT_FOUND = "not_found"
BLOCKED = "blocked"
NO_CONTENT = "no_content"
UNSUPPORTED = "unsupported"
ERROR = "error"

# Outcomes worth asking the server about again: the answer may differ next time.
RETRYABLE = (BLOCKED, ERROR, NO_CONTENT)


def upsert(
    session: Session,
    url: str,
    *,
    status: str,
    html_body: str = "",
    http_status: Optional[int] = None,
    page_bytes: Optional[int] = None,
    note: Optional[str] = None,
) -> None:
    """Record the outcome for a link, replacing any earlier one."""
    values = {
        "status": status,
        "html_body": html_body,
        "http_status": http_status,
        "page_bytes": page_bytes,
        "note": note,
        "fetched_at": func.now(),
    }
    session.execute(
        pg_insert(JobListingTest)
        .values(url=url, **values)
        .on_conflict_do_update(index_elements=["url"], set_={**values, "updated_at": func.now()})
    )
    session.flush()


def get_by_url(session: Session, url: str) -> Optional[JobListingTest]:
    return session.scalars(select(JobListingTest).where(JobListingTest.url == url)).first()


def pending_urls(
    session: Session,
    limit: int,
    *,
    retry: bool = False,
    include_pasted: bool = False,
    hosts: Optional[Sequence[str]] = None,
) -> list[str]:
    """Collected links to fetch, oldest first.

    Skips links the user removed and, unless `include_pasted` is set, links already
    stored by hand (they have a raw listing; fetching them again is how a fetched
    block is compared with the hand copy). A link is also skipped once it has a row here, unless `retry`
    is set and that row's outcome was a retryable one. `hosts` keeps only links on
    those hosts (a subdomain of one counts).
    """
    tried = exists().where(JobListingTest.url == ManualJobListingUrl.url)
    if retry:
        tried = exists().where(
            JobListingTest.url == ManualJobListingUrl.url,
            JobListingTest.status.notin_(RETRYABLE),
        )
    conditions = [ManualJobListingUrl.removed_at.is_(None), ~tried]
    if hosts:
        conditions.append(or_(*(ManualJobListingUrl.url.ilike(f"%{host}/%") for host in hosts)))
    if not include_pasted:
        conditions.append(~exists().where(RawJobListing.url == ManualJobListingUrl.url))
    statement = (
        select(ManualJobListingUrl.url)
        .where(*conditions)
        .order_by(ManualJobListingUrl.id)
        .limit(limit)
    )
    return list(session.scalars(statement))


def list_recent(session: Session, limit: int = 50, offset: int = 0) -> Sequence[JobListingTest]:
    """Newest first, without the bodies (whole blocks of HTML)."""
    columns = load_only(
        JobListingTest.id, JobListingTest.url, JobListingTest.status,
        JobListingTest.http_status, JobListingTest.page_bytes, JobListingTest.note,
        JobListingTest.fetched_at,
    )
    return session.scalars(
        select(JobListingTest)
        .options(columns)
        .order_by(JobListingTest.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()


def count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(JobListingTest)) or 0
