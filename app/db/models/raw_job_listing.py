"""The `raw_job_listings` table.

A listing the user pasted in by hand: the link it came from and the full HTML
body of the page, stored untouched. Nothing here is parsed or cleaned - later
workshop steps decide what to do with it, so keeping the original lets them be
re-run without asking the user to paste it again.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class RawJobListing(Base, TimestampMixin):
    __tablename__ = "raw_job_listings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Text, not String: job-board URLs carry long tracking query strings, and
    # the body is a whole page of HTML.
    url: Mapped[str] = mapped_column(Text, nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)

    # Where this listing stands (ADR-0016). NULL: waiting to be graded.
    #   graded       a job was created from it (`job_id`)
    #   not_found    the platform says the posting no longer exists
    #   unparseable  right platform, HTML not a job page; can be retried
    #   unsupported  no parser for this site, so it can never be graded
    status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RawJobListing id={self.id} url={self.url!r}>"
