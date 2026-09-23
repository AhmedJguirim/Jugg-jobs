"""Controlled vocabularies attached to a vacancy.

Categories, industries, education levels and contract types are small, closed
sets supplied by the source (16, 30, 5 and 5 distinct values respectively in a
200-vacancy sample). They get lookup tables plus association tables rather than
array columns, so a value is stored once, spelled one way, and can be listed
without scanning every job. See docs/adr/0006.

Open-ended free text (e.g. the `job_titles` search keywords) stays an array
column on `jobs` - a lookup table buys nothing for values that are neither
closed nor reused.

Each association carries `is_primary`, which marks the value the API reports in
its singular field (`educationLevel` vs `educationLevels`). That keeps the
"main" value without duplicating it into a second column.
"""

from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, Table, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class _LookupMixin:
    """Shared shape for every controlled vocabulary table."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} id={self.id} name={self.name!r}>"


def _association(table_name: str, lookup_table: str, lookup_column: str) -> Table:
    """Build a job<->vocabulary association table.

    Composite primary key prevents the same value being attached twice, and
    ON DELETE CASCADE means deleting a job cleans up its links without a second
    statement.
    """
    return Table(
        table_name,
        Base.metadata,
        Column(
            "job_id",
            ForeignKey("jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column(
            lookup_column,
            ForeignKey(f"{lookup_table}.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("is_primary", Boolean, nullable=False, server_default="false"),
    )


class Category(Base, _LookupMixin, TimestampMixin):
    """Occupational category, e.g. "Medisch/Zorg", "Automatisering/Internet"."""

    __tablename__ = "categories"


class Industry(Base, _LookupMixin, TimestampMixin):
    """Employer industry, e.g. "Gezondheidszorg/Welzijn", "Industrie"."""

    __tablename__ = "industries"


class EducationLevel(Base, _LookupMixin, TimestampMixin):
    """Dutch education level: MBO, HBO, WO, VMBO/MAVO, Lagere school."""

    __tablename__ = "education_levels"


class ContractType(Base, _LookupMixin, TimestampMixin):
    """Contract form: Vast, Tijdelijk, Freelance, Stage, Vakantiewerk."""

    __tablename__ = "contract_types"


job_categories = _association("job_categories", "categories", "category_id")
job_industries = _association("job_industries", "industries", "industry_id")
job_education_levels = _association("job_education_levels", "education_levels", "education_level_id")
job_contract_types = _association("job_contract_types", "contract_types", "contract_type_id")


class DcoTitle(Base, TimestampMixin):
    """A classifier's normalised occupation title for one vacancy.

    The source returns a ranked list of candidates with confidence scores -
    the raw employer title mapped onto a controlled occupation vocabulary. This
    is a child table rather than an array because each entry carries its own
    attributes (score, rank, generic bucket).

    `rank` 0 is the winning candidate, mirrored onto `jobs.dco_title` for
    filtering without a join.
    """

    __tablename__ = "job_dco_titles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    generic_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 0..1 confidence; observed range 0.717-0.934.
    similarity: Mapped[float | None] = mapped_column(nullable=True)
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        UniqueConstraint("job_id", "rank", name="uq_job_dco_titles_job_id_rank"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DcoTitle job_id={self.job_id} rank={self.rank} title={self.title!r}>"
