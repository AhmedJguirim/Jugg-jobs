"""The `companies` table.

A company is an employer we track job openings for. Each `*_jobs_link` column
holds a pre-built search URL for that job board, so a consumer can jump straight
to the employer's listings without re-deriving the query.
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Company(Base, TimestampMixin):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)

    # Nullable: the seed dataset carries no `type` value for any record.
    # See docs/adr/0002-companies-schema-nullability.md.
    type: Mapped[str | None] = mapped_column(String(128), nullable=True)

    website: Mapped[str | None] = mapped_column(String(512), nullable=True)
    slug: Mapped[str | None] = mapped_column(String(512), nullable=True, unique=True)

    # Nullable: 45 of the 2901 seed records have no province.
    province: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Job-board search URLs. Text rather than String: these carry encoded query
    # strings with no meaningful length ceiling.
    google_jobs_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_jobs_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    indeed_jobs_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    glassdoor_jobs_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    nationalevacaturebank_jobs_link: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- scrape bookkeeping ---
    # When this employer's own job-board search was last run, and what it
    # found. Stored on the company rather than derived from `jobs` because
    # "we looked and found nothing" is different from "we never looked", and
    # only a recorded timestamp can tell them apart.
    last_scraped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_scrape_vacancies: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        # Supports the common "filter employers by region" read pattern.
        Index("ix_companies_province", "province"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Company id={self.id} name={self.name!r} province={self.province!r}>"
