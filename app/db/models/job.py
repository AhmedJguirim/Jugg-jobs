"""The `jobs` table: one vacancy.

What lives where, and why:

- **Own tables**: `locations` (places repeat across vacancies), the four
  controlled vocabularies, and `job_dco_titles` (a ranked list whose entries
  carry their own scores). See `taxonomy.py` and ADR-0006.
- **Columns on `jobs`**: everything genuinely one-to-one with the vacancy -
  salary band, working hours, how to apply, the contact person, the street
  address. A vacancy has exactly one of each and they are never shared, so a
  separate table would add a join and buy nothing.
- **Array column**: `job_titles`, an open-ended list of free-text search
  keywords (142 distinct values in a 200-vacancy sample). Not a closed
  vocabulary, so no lookup table.
- **`raw`**: the untouched source payload as JSONB, so new fields can be
  back-filled from stored data instead of re-fetching the API.

`external_id` + `source` is the natural key: it identifies the vacancy at the
board it came from and is what makes ingest idempotent. `id` stays a surrogate
integer so later sources can be added without changing the key shape.
"""

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.company import Company
from app.db.models.location import Location
from app.db.models.taxonomy import (
    Category,
    ContractType,
    DcoTitle,
    EducationLevel,
    Industry,
    job_categories,
    job_contract_types,
    job_education_levels,
    job_industries,
)

SOURCE_NVB = "nvb"


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

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

    categories: Mapped[list[Category]] = relationship(secondary=job_categories, lazy="selectin")
    industries: Mapped[list[Industry]] = relationship(secondary=job_industries, lazy="selectin")
    education_levels: Mapped[list[EducationLevel]] = relationship(
        secondary=job_education_levels, lazy="selectin"
    )
    contract_types: Mapped[list[ContractType]] = relationship(
        secondary=job_contract_types, lazy="selectin"
    )
    dco_titles: Mapped[list[DcoTitle]] = relationship(
        cascade="all, delete-orphan",
        order_by="DcoTitle.rank",
        lazy="selectin",
    )

    # --- description ---
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    origin: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- titles and classification ---
    function_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    function_title_slug: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Winning dco_titles entry (rank 0), mirrored here to filter without a join.
    dco_title: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    dco_title_slug: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # ISCO-08 occupation code, e.g. "2514" (applications programmers).
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

    # --- contact person (one per vacancy, never shared) ---
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

    # --- raw payload, for re-deriving fields without another API call ---
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_jobs_source_external_id"),
        # Supports the common listing: newest first, optionally per source.
        Index("ix_jobs_source_start_date", "source", "start_date"),
        # Keyword search over the free-text title list.
        Index("ix_jobs_job_titles", "job_titles", postgresql_using="gin"),
    )

    # -- convenience --

    @property
    def salary_range(self) -> str | None:
        if self.salary_min is None:
            return None
        return f"{self.salary_min}-{self.salary_max} ({self.salary_type})"

    @property
    def city(self) -> str | None:
        return self.location.city if self.location else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Job id={self.id} external_id={self.external_id!r} title={self.title!r}>"
