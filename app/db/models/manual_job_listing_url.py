"""The `manual_job_listing_urls` table.

Links to individual job postings collected by hand from Google results
(`site:jobs.ashbyhq.com ...`). The url is the identity: submitting the same
link twice never creates a second row.
"""

from datetime import datetime

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ManualJobListingUrl(Base, TimestampMixin):
    __tablename__ = "manual_job_listing_urls"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Text: result URLs carry long tracking query strings.
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # When the page behind the link was fetched. NULL until something scrapes
    # it - nothing does yet.
    scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set when the user removes the link (ADR-0017). The row stays, so the unique
    # url still stops the same link coming back from the next Google paste; every
    # list and queue just leaves it out.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ManualJobListingUrl id={self.id} url={self.url!r}>"
