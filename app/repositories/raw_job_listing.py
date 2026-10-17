"""CRUD operations for raw job listings.

Same contract as the other repositories: an explicit `Session`, `flush()` but
never `commit()`, and domain errors instead of SQLAlchemy exceptions.
"""

from typing import Optional, Sequence

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, load_only

from app.db.models.raw_job_listing import RawJobListing
from app.schemas.raw_job_listing import RawJobListingCreate


class RawJobListingNotFound(Exception):
    def __init__(self, identifier: object) -> None:
        super().__init__(f"raw job listing not found: {identifier!r}")
        self.identifier = identifier


GRADED = "graded"
NOT_FOUND = "not_found"
UNPARSEABLE = "unparseable"
UNSUPPORTED = "unsupported"
REMOVED = "removed"  # the user dropped it; kept, but never listed again


def _waiting():
    """Listings still to grade: never processed, or a page that could not be
    read (which may work after a parser fix, so it stays on the list)."""
    return or_(RawJobListing.status.is_(None), RawJobListing.status == UNPARSEABLE)


def create(
    session: Session, payload: RawJobListingCreate, *, status: Optional[str] = None
) -> RawJobListing:
    listing = RawJobListing(url=payload.url.strip(), html_body=payload.html_body, status=status)
    session.add(listing)
    session.flush()
    return listing


def get(session: Session, listing_id: int) -> RawJobListing:
    listing = session.get(RawJobListing, listing_id)
    if listing is None:
        raise RawJobListingNotFound(listing_id)
    return listing


def list_recent(
    session: Session, limit: int = 50, offset: int = 0, *, waiting: bool = False
) -> Sequence[RawJobListing]:
    """Without the bodies: they are whole pages of HTML.

    Newest first; with `waiting=True` only the listings still to grade, oldest
    first, so "the first N" is the top of the list.
    """
    columns = load_only(
        RawJobListing.id, RawJobListing.url, RawJobListing.created_at,
        RawJobListing.status, RawJobListing.job_id,
    )
    statement = select(RawJobListing).options(columns)
    if waiting:
        statement = statement.where(_waiting()).order_by(RawJobListing.id)
    else:
        statement = statement.order_by(RawJobListing.created_at.desc(), RawJobListing.id.desc())
    return session.scalars(statement.limit(limit).offset(offset)).all()


def count(session: Session, *, waiting: bool = False) -> int:
    statement = select(func.count()).select_from(RawJobListing)
    if waiting:
        statement = statement.where(_waiting())
    return session.scalar(statement) or 0


def remove(session: Session, listing_id: int) -> RawJobListing:
    """Drop a listing from the waiting list for good (the row is kept)."""
    listing = get(session, listing_id)
    listing.status = REMOVED
    listing.processed_at = func.now()
    session.flush()
    return listing


def mark(
    session: Session, listing_id: int, status: str, *, job_id: Optional[int] = None
) -> None:
    """Record how processing ended. Quiet if the listing is gone."""
    session.execute(
        update(RawJobListing)
        .where(RawJobListing.id == listing_id)
        .values(status=status, job_id=job_id, processed_at=func.now())
    )
