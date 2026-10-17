"""The `raw_job_listings` table.

A listing the user pasted in by hand: the link it came from and the full HTML
body of the page, stored untouched. Nothing here is parsed or cleaned - later
workshop steps decide what to do with it, so keeping the original lets them be
re-run without asking the user to paste it again.
"""

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class RawJobListing(Base, TimestampMixin):
    __tablename__ = "raw_job_listings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Text, not String: job-board URLs carry long tracking query strings, and
    # the body is a whole page of HTML.
    url: Mapped[str] = mapped_column(Text, nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RawJobListing id={self.id} url={self.url!r}>"
