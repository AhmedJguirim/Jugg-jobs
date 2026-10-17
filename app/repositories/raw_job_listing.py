"""CRUD operations for raw job listings.

Same contract as the other repositories: an explicit `Session`, `flush()` but
never `commit()`, and domain errors instead of SQLAlchemy exceptions.
"""

from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only

from app.db.models.raw_job_listing import RawJobListing
from app.schemas.raw_job_listing import RawJobListingCreate


class RawJobListingNotFound(Exception):
    def __init__(self, identifier: object) -> None:
        super().__init__(f"raw job listing not found: {identifier!r}")
        self.identifier = identifier


def create(session: Session, payload: RawJobListingCreate) -> RawJobListing:
    listing = RawJobListing(url=payload.url.strip(), html_body=payload.html_body)
    session.add(listing)
    session.flush()
    return listing


def get(session: Session, listing_id: int) -> RawJobListing:
    listing = session.get(RawJobListing, listing_id)
    if listing is None:
        raise RawJobListingNotFound(listing_id)
    return listing


def list_recent(session: Session, limit: int = 50, offset: int = 0) -> Sequence[RawJobListing]:
    """Newest first, without the bodies: they are whole pages of HTML."""
    statement = (
        select(RawJobListing)
        .options(load_only(RawJobListing.id, RawJobListing.url, RawJobListing.created_at))
        .order_by(RawJobListing.created_at.desc(), RawJobListing.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return session.scalars(statement).all()


def count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(RawJobListing)) or 0
