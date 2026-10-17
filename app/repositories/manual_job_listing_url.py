"""Storage for manually collected job-posting URLs.

Same contract as the other repositories: explicit `Session`, `flush()` but never
`commit()`.
"""

from typing import Sequence

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.manual_job_listing_url import ManualJobListingUrl
from app.db.models.raw_job_listing import RawJobListing


def add_many(session: Session, urls: Sequence[str]) -> list[str]:
    """Insert the URLs that are not stored yet; return the ones actually added.

    `ON CONFLICT DO NOTHING` rather than check-then-insert, so two submissions
    racing on the same link cannot both pass the check.
    """
    unique = list(dict.fromkeys(urls))
    if not unique:
        return []
    statement = (
        pg_insert(ManualJobListingUrl)
        .values([{"url": url} for url in unique])
        .on_conflict_do_nothing(index_elements=["url"])
        .returning(ManualJobListingUrl.url)
    )
    added = set(session.scalars(statement).all())
    session.flush()
    return [url for url in unique if url in added]


def list_recent(
    session: Session, limit: int = 200, offset: int = 0, *, only_unprocessed: bool = False
) -> Sequence[ManualJobListingUrl]:
    """Newest first. `only_unprocessed` leaves out links whose HTML is stored,
    which includes the ones whose page turned out to be "job not found"."""
    statement = select(ManualJobListingUrl)
    if only_unprocessed:
        statement = statement.where(_unprocessed())
    statement = statement.order_by(ManualJobListingUrl.id.desc()).limit(limit).offset(offset)
    return session.scalars(statement).all()


def _unprocessed():
    """URLs with no stored raw listing (matched on the exact url)."""
    return ~exists().where(RawJobListing.url == ManualJobListingUrl.url)


def next_unprocessed(session: Session, skip: int = 0) -> ManualJobListingUrl | None:
    """The oldest collected URL whose HTML has not been stored yet.

    `skip` passes over that many of them, for links the user wants to leave
    for later.
    """
    statement = (
        select(ManualJobListingUrl)
        .where(_unprocessed())
        .order_by(ManualJobListingUrl.id)
        .offset(skip)
        .limit(1)
    )
    return session.scalars(statement).first()


def count_unprocessed(session: Session) -> int:
    statement = select(func.count()).select_from(ManualJobListingUrl).where(_unprocessed())
    return session.scalar(statement) or 0


def processed_urls(session: Session, urls: Sequence[str]) -> set[str]:
    """Which of these URLs already have a stored raw listing."""
    if not urls:
        return set()
    return set(session.scalars(select(RawJobListing.url).where(RawJobListing.url.in_(urls))))


def count(session: Session, *, only_unprocessed: bool = False) -> int:
    if only_unprocessed:
        return count_unprocessed(session)
    return session.scalar(select(func.count()).select_from(ManualJobListingUrl)) or 0
