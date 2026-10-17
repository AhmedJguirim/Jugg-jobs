"""CRUD operations for companies.

The repository owns all query construction for the `companies` table; callers
(workflows, API routes, scripts) never build SQL themselves. It takes an
explicit `Session` and never commits - transaction boundaries belong to the
caller, so several repository calls can share one atomic unit of work.

Raises `CompanyNotFound` / `DuplicateCompany` instead of leaking SQLAlchemy
exceptions, so callers depend on this module rather than on the ORM.
"""

from datetime import datetime
from typing import Iterable, Optional, Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.company import Company
from app.db.models.enrichment import JobAssessment
from app.db.models.job import Job
from app.schemas.company import CompanyCreate, CompanyUpdate


class CompanyRepositoryError(Exception):
    """Base class for company repository failures."""


class CompanyNotFound(CompanyRepositoryError):
    def __init__(self, identifier: object) -> None:
        super().__init__(f"company not found: {identifier!r}")
        self.identifier = identifier


class DuplicateCompany(CompanyRepositoryError):
    def __init__(self, field: str, value: object) -> None:
        super().__init__(f"company with {field}={value!r} already exists")
        self.field = field
        self.value = value


def _duplicate_field(error: IntegrityError) -> tuple[str, str]:
    """Map a unique-violation to the (field, constraint) it came from."""
    text = str(getattr(error, "orig", error))
    for field in ("name", "slug"):
        if f"companies_{field}" in text or f"({field})" in text:
            return field, text
    return "unknown", text


# ---- create ----


def create(session: Session, payload: CompanyCreate) -> Company:
    """Insert one company. Raises DuplicateCompany on a unique conflict."""
    company = Company(**payload.model_dump())
    session.add(company)
    try:
        session.flush()
    except IntegrityError as error:
        session.rollback()
        field, _ = _duplicate_field(error)
        raise DuplicateCompany(field, getattr(payload, field, None)) from error
    return company


def bulk_upsert(
    session: Session,
    payloads: Iterable[dict],
    *,
    conflict_column: str = "name",
) -> int:
    """Insert many companies, updating the ones that already exist.

    Uses PostgreSQL's INSERT ... ON CONFLICT DO UPDATE so ingesting the same
    source file twice is idempotent rather than a duplicate-key failure.
    Returns the number of rows written. `updated_at` is bumped explicitly
    because ORM-level `onupdate` does not apply to a Core insert.
    """
    rows = list(payloads)
    if not rows:
        return 0

    statement = pg_insert(Company).values(rows)
    updatable = {
        column.name: statement.excluded[column.name]
        for column in Company.__table__.columns
        if column.name not in {"id", conflict_column, "created_at"}
    }
    updatable["updated_at"] = func.now()

    statement = statement.on_conflict_do_update(
        index_elements=[conflict_column],
        set_=updatable,
    )
    result = session.execute(statement)
    # psycopg3 reports -1 for this statement shape rather than a row count;
    # every row in the batch is either inserted or updated, so fall back to len.
    rowcount = result.rowcount
    return rowcount if rowcount is not None and rowcount >= 0 else len(rows)


# ---- read ----


def get(session: Session, company_id: int) -> Optional[Company]:
    """Return a company by id, or None."""
    return session.get(Company, company_id)


def get_or_404(session: Session, company_id: int) -> Company:
    """Return a company by id, raising CompanyNotFound if absent."""
    company = get(session, company_id)
    if company is None:
        raise CompanyNotFound(company_id)
    return company


def get_by_name(session: Session, name: str) -> Optional[Company]:
    return session.scalar(select(Company).where(Company.name == name))


def get_by_slug(session: Session, slug: str) -> Optional[Company]:
    return session.scalar(select(Company).where(Company.slug == slug))


def _listing_query(
    *,
    search: Optional[str] = None,
    province: Optional[str] = None,
) -> Select:
    query = select(Company)
    if search:
        # Case-insensitive contains. ILIKE is PostgreSQL-specific and matches
        # how the data is queried in practice.
        query = query.where(Company.name.ilike(f"%{search}%"))
    if province:
        query = query.where(Company.province == province)
    return query


def list_companies(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    province: Optional[str] = None,
    order_by: str = "name",
) -> Sequence[Company]:
    """Page through companies, optionally filtered by name fragment/province."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    column = getattr(Company, order_by, None)
    if column is None:
        raise ValueError(f"cannot order by unknown column {order_by!r}")

    query = _listing_query(search=search, province=province)
    query = query.order_by(column, Company.id).limit(limit).offset(offset)
    return session.scalars(query).all()


def count(
    session: Session,
    *,
    search: Optional[str] = None,
    province: Optional[str] = None,
) -> int:
    """Total matching rows, ignoring limit/offset."""
    query = _listing_query(search=search, province=province)
    return session.scalar(select(func.count()).select_from(query.subquery())) or 0


# ---- update ----


def update(session: Session, company_id: int, payload: CompanyUpdate) -> Company:
    """Apply a partial update. Unset fields are left untouched."""
    company = get_or_404(session, company_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(company, field, value)
    try:
        session.flush()
    except IntegrityError as error:
        session.rollback()
        field, _ = _duplicate_field(error)
        raise DuplicateCompany(field, changes.get(field)) from error
    return company


# ---- delete ----


def delete(session: Session, company_id: int) -> None:
    """Delete a company. Raises CompanyNotFound if it does not exist."""
    company = get_or_404(session, company_id)
    session.delete(company)
    session.flush()


# ---- browsing with vacancy statistics ----

# One row of the browse listing: the employer plus what we know about their
# vacancies. Kept as a tuple rather than a model because it is a read-model for
# the UI, not something that is ever written back.
CompanyStats = tuple[Company, int, Optional[int], Optional[object]]


def _browse_query(
    *,
    search: Optional[str] = None,
    province: Optional[str] = None,
    has_jobs: Optional[bool] = None,
    has_links: Optional[bool] = None,
    scraped_before: Optional[datetime] = None,
    include_never_scraped: bool = True,
    profile_name: str = "default",
) -> Select:
    """Employers with their vacancy count, best fit score and latest posting.

    LEFT JOINs throughout: the seed file contributed 2901 employers with no
    vacancies of their own, and they are exactly the ones worth browsing for
    their job-board search links.

    `scraped_before` keeps employers last scraped before that moment - the ones
    due for another look. A never-scraped employer was not scraped after it
    either, so it counts unless `include_never_scraped` is False.
    """
    best_fit = func.max(JobAssessment.fit_score)
    latest = func.max(Job.start_date)

    query = (
        select(Company, func.count(func.distinct(Job.id)), best_fit, latest)
        .outerjoin(Job, Job.company_id == Company.id)
        .outerjoin(
            JobAssessment,
            (JobAssessment.job_id == Job.id) & (JobAssessment.profile_name == profile_name),
        )
        .group_by(Company.id)
    )

    if search:
        query = query.where(Company.name.ilike(f"%{search}%"))
    if province:
        query = query.where(Company.province == province)
    if has_links is True:
        query = query.where(Company.google_jobs_link.is_not(None))
    elif has_links is False:
        query = query.where(Company.google_jobs_link.is_(None))
    if scraped_before is not None:
        stale = Company.last_scraped_at < scraped_before
        query = query.where(
            or_(stale, Company.last_scraped_at.is_(None)) if include_never_scraped else stale
        )
    if has_jobs is True:
        query = query.having(func.count(func.distinct(Job.id)) > 0)
    elif has_jobs is False:
        query = query.having(func.count(func.distinct(Job.id)) == 0)

    return query


def browse(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "name",
    profile_name: str = "default",
    **filters,
) -> Sequence[CompanyStats]:
    """A page of employers with their vacancy statistics.

    Paginated in the database rather than in the browser: 3000+ employers is
    past the point where shipping the whole table to the page is reasonable.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    query = _browse_query(profile_name=profile_name, **filters)

    orderings = {
        "name": Company.name.asc(),
        "vacancies": func.count(func.distinct(Job.id)).desc(),
        "fit": func.max(JobAssessment.fit_score).desc().nullslast(),
        "recent": func.max(Job.start_date).desc().nullslast(),
        # When the employer entered our table: the 2901 seed rows share one
        # timestamp, so the tiebreak by name below orders them among themselves.
        "newest": Company.created_at.desc(),
        "oldest": Company.created_at.asc(),
    }
    if order_by not in orderings:
        raise ValueError(f"cannot order by {order_by!r}")

    query = query.order_by(orderings[order_by], Company.name).limit(limit).offset(offset)
    return [tuple(row) for row in session.execute(query).all()]


def browse_count(session: Session, *, profile_name: str = "default", **filters) -> int:
    """How many employers match, ignoring pagination."""
    inner = _browse_query(profile_name=profile_name, **filters).subquery()
    return session.scalar(select(func.count()).select_from(inner)) or 0


def provinces(session: Session) -> Sequence[str]:
    """Distinct province values, for the filter dropdown."""
    return session.scalars(
        select(Company.province)
        .where(Company.province.is_not(None))
        .distinct()
        .order_by(Company.province)
    ).all()


def with_vacancies(
    session: Session, company_id: int, *, profile_name: str = "default"
) -> tuple[Company, Sequence[tuple[Job, Optional[JobAssessment]]]]:
    """One employer and every vacancy ingested for them, best fit first."""
    company = get_or_404(session, company_id)
    rows = session.execute(
        select(Job, JobAssessment)
        .outerjoin(
            JobAssessment,
            (JobAssessment.job_id == Job.id) & (JobAssessment.profile_name == profile_name),
        )
        .where(Job.company_id == company_id)
        .order_by(JobAssessment.fit_score.desc().nullslast(), Job.start_date.desc().nullslast())
    ).unique().all()
    return company, [tuple(row) for row in rows]
