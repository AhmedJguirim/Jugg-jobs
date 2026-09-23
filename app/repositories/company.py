"""CRUD operations for companies.

The repository owns all query construction for the `companies` table; callers
(workflows, API routes, scripts) never build SQL themselves. It takes an
explicit `Session` and never commits - transaction boundaries belong to the
caller, so several repository calls can share one atomic unit of work.

Raises `CompanyNotFound` / `DuplicateCompany` instead of leaking SQLAlchemy
exceptions, so callers depend on this module rather than on the ORM.
"""

from typing import Iterable, Optional, Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.company import Company
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
