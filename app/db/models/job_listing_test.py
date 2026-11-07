"""The `job_listings_test` table: listings whose HTML the app fetched itself.

The experimental twin of `raw_job_listings`. That table holds pages the user
copied from the browser by hand; this one holds what `app/services/listing_fetch.py`
got by requesting the link, cut down to the same block the user would have
copied. Keeping it separate means the fetcher can be tried on every collected
link without touching the waiting list or the grading flow.
See docs/adr/0023-fetching-listing-html.md.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class JobListingTest(Base, TimestampMixin):
    __tablename__ = "job_listings_test"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The link as collected (`manual_job_listing_urls.url`); one row per link,
    # fetching it again replaces the row's content.
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # The posting's block of HTML, in the shape a hand copy has. Empty when
    # nothing usable came back.
    html_body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    # How the fetch ended:
    #   fetched      the posting's block was found and stored
    #   not_found    the server answered 404/410 (the body is kept: the parser
    #                reads the platform's "job gone" page as a result)
    #   blocked      403/429/5xx or a challenge page; try again later
    #   no_content   a normal page, but without the posting block
    #   unsupported  no fetcher for this site; no request was made
    #   error        the request itself failed (timeout, DNS, ...)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Size of the whole response, to compare with the stored block.
    page_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JobListingTest id={self.id} status={self.status!r} url={self.url!r}>"
