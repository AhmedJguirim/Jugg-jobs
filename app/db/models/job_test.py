"""The `jobs_test` table: a scratch copy of `jobs` for the workspace parsers.

Listings parsed from hand-collected pages (Ashby, ...) land here instead of in
`jobs`, so experimenting with parsers never touches the board's data. The
columns, defaults, indexes and the (source, external_id) key are the same as
`jobs`; `tests/unit/test_job_test_model.py` fails if the two drift apart.

What is *not* copied: the vocabulary and classifier tables that hang off
`jobs` (categories, industries, education levels, contract types, DCO titles).
`companies` and `locations` are the shared tables, referenced the same way.
See docs/adr/0014-listing-parsers.md.
"""

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.company import Company
from app.db.models.job import SOURCE_NVB
from app.db.models.location import Location


class JobTest(Base, TimestampMixin):
    __tablename__ = "jobs_test"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # --- provenance ---
    source: Mapped[str] = mapped_column(String(64), nullable=False, server_default=SOURCE_NVB)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # --- relationships ---
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL"), nullable=True, index=True
    )

    company: Mapped[Company | None] = relationship(lazy="joined")
    location: Mapped[Location | None] = relationship(lazy="joined")

    # --- description ---
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    origin: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- titles and classification ---
    function_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    function_title_slug: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dco_title: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    dco_title_slug: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    isco: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    jdco: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_titles: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )

    career_level: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # --- terms ---
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    working_hours_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    working_hours_max: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- address detail specific to this vacancy ---
    street: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zip_code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- applying ---
    apply_option: Mapped[str | None] = mapped_column(String(64), nullable=True)
    apply_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    number_of_applies: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- contact person ---
    contact_first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    contact_last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- lifecycle ---
    start_date: Mapped[datetime | None] = mapped_column(Date, nullable=True, index=True)
    end_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)

    recruiter_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- the user's marks ---
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- raw payload ---
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_jobs_test_source_external_id"),
        Index("ix_jobs_test_source_start_date", "source", "start_date"),
        Index("ix_jobs_test_job_titles", "job_titles", postgresql_using="gin"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JobTest id={self.id} external_id={self.external_id!r} title={self.title!r}>"
