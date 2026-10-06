"""CRUD operations for vacancies.

Same contract as the company repository: an explicit `Session`, `flush()` but
never `commit()`, and domain errors instead of SQLAlchemy exceptions.

Writing a vacancy touches several tables (company, location, four vocabularies,
classifier titles). That fan-out is this module's job: callers pass plain
values in a `JobCreate` and never deal with foreign keys.
"""

from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence, Type

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.company import Company
from app.db.models.job import Job
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
from app.schemas.job import JobCreate, JobUpdate, LocationBase, VocabularyTerms


class JobRepositoryError(Exception):
    """Base class for vacancy repository failures."""


class JobNotFound(JobRepositoryError):
    def __init__(self, identifier: object) -> None:
        super().__init__(f"job not found: {identifier!r}")
        self.identifier = identifier


class DuplicateJob(JobRepositoryError):
    def __init__(self, source: str, external_id: str) -> None:
        super().__init__(f"job {external_id!r} from {source!r} already exists")
        self.source = source
        self.external_id = external_id


# Which association table and column carry each vocabulary.
_VOCABULARIES = {
    "categories": (Category, job_categories, "category_id"),
    "industries": (Industry, job_industries, "industry_id"),
    "education_levels": (EducationLevel, job_education_levels, "education_level_id"),
    "contract_types": (ContractType, job_contract_types, "contract_type_id"),
}


# ---- resolving related rows ----



def _get_or_create(session: Session, model: Type, **keys):
    """Fetch a row by its natural key, creating it if absent - concurrency safe.

    A plain SELECT-then-INSERT races: the ingest queue processes pages in
    parallel, so two pages carrying the same industry ("Industrie") both find
    nothing and both insert, and one dies on the unique constraint. Observed in
    production as `UniqueViolation on uq_industries_name` during a four-page
    company scrape.

    The insert is therefore attempted inside a SAVEPOINT. If a competing
    transaction won the race, only the savepoint is rolled back - the rest of
    the page's work survives - and the row it committed is returned instead.
    """
    existing = session.scalar(select(model).filter_by(**keys))
    if existing is not None:
        return existing

    savepoint = session.begin_nested()
    try:
        created = model(**keys)
        session.add(created)
        savepoint.commit()
        return created
    except IntegrityError:
        savepoint.rollback()
        # The competing transaction has committed by now, so this finds its row.
        found = session.scalar(select(model).filter_by(**keys))
        if found is None:  # pragma: no cover - the other side rolled back
            raise
        return found


def get_or_create_term(session: Session, model: Type, name: str):
    """Fetch a vocabulary term by name, creating it if new.

    Vocabularies are closed sets that grow only when the source introduces a
    value, so create-on-first-sight is the right behaviour: ingest never fails
    because a new category appeared - nor because two pages introduced it at
    the same moment.
    """
    return _get_or_create(session, model, name=name)


def get_or_create_location(session: Session, payload: LocationBase) -> Location:
    """Fetch a place by its natural key (city + municipality + province)."""
    existing = session.scalar(
        select(Location).where(
            Location.city == payload.city,
            Location.municipality.is_not_distinct_from(payload.municipality),
            Location.province.is_not_distinct_from(payload.province),
        )
    )
    if existing is not None:
        return existing

    # Same race as the vocabularies: two pages can carry the same city.
    savepoint = session.begin_nested()
    try:
        location = Location(**payload.model_dump())
        session.add(location)
        savepoint.commit()
        return location
    except IntegrityError:
        savepoint.rollback()
        return session.scalar(
            select(Location).where(
                Location.city == payload.city,
                Location.municipality.is_not_distinct_from(payload.municipality),
                Location.province.is_not_distinct_from(payload.province),
            )
        )


def get_or_create_company(
    session: Session,
    *,
    name: str,
    slug: Optional[str] = None,
    type: Optional[str] = None,
    website: Optional[str] = None,
) -> Company:
    """Fetch an employer by name, creating it if unknown.

    Vacancies carry `slug`, `type` and `website` - the columns the seed file
    left empty - so an existing company is enriched with anything it lacks.
    Present values are never overwritten: the seed file is authoritative for
    what it does contain.
    """
    company = session.scalar(select(Company).where(Company.name == name))

    # `slug` is unique, and two distinct employers can arrive carrying the same
    # one. Dropping the slug keeps the vacancy ingestible instead of failing the
    # whole batch on a value we do not depend on.
    if slug is not None:
        clash = session.scalar(select(Company).where(Company.slug == slug))
        if clash is not None and (company is None or clash.id != company.id):
            slug = None

    if company is None:
        savepoint = session.begin_nested()
        try:
            company = Company(name=name, slug=slug, type=type, website=website)
            session.add(company)
            savepoint.commit()
            return company
        except IntegrityError:
            # Another page created this employer first.
            savepoint.rollback()
            company = session.scalar(select(Company).where(Company.name == name))
            if company is None:  # pragma: no cover - lost to a rollback
                raise

    for field, value in (("slug", slug), ("type", type), ("website", website)):
        if value and getattr(company, field) is None:
            setattr(company, field, value)
    return company


def _set_vocabulary(session: Session, job: Job, field: str, terms: VocabularyTerms) -> None:
    """Replace a job's links to one vocabulary, recording the primary value.

    The collection is assigned through the ORM so SQLAlchemy issues the
    association inserts and deletes itself - writing those rows with Core
    statements behind its back leaves its bookkeeping stale. `is_primary` is
    not reachable through a plain `secondary` relationship, so it is stamped
    afterwards with a targeted UPDATE.
    """
    model, association, column = _VOCABULARIES[field]

    terms_seen = dict.fromkeys(terms.values)  # de-duplicate, keep order
    rows = [get_or_create_term(session, model, name) for name in terms_seen]
    setattr(job, field, rows)
    session.flush()

    if not rows:
        return

    session.execute(
        association.update().where(association.c.job_id == job.id).values(is_primary=False)
    )
    primary = next((row for row in rows if row.name == terms.primary), None)
    if primary is not None:
        session.execute(
            association.update()
            .where(association.c.job_id == job.id, association.c[column] == primary.id)
            .values(is_primary=True)
        )


def _set_dco_titles(session: Session, job: Job, entries: Iterable) -> None:
    """Replace the classifier's ranked titles for a job."""
    job.dco_titles.clear()
    session.flush()
    for entry in entries:
        job.dco_titles.append(DcoTitle(**entry.model_dump()))


# ---- create ----

_RELATED_FIELDS = {
    "company_name",
    "company_slug",
    "company_type",
    "company_website",
    "location",
    "categories",
    "industries",
    "education_levels",
    "contract_types",
    "dco_titles",
}


def create(session: Session, payload: JobCreate) -> Job:
    """Insert a vacancy and everything hanging off it."""
    columns = payload.model_dump(exclude=_RELATED_FIELDS)
    job = Job(**columns)

    if payload.company_name:
        job.company = get_or_create_company(
            session,
            name=payload.company_name,
            slug=payload.company_slug,
            type=payload.company_type,
            website=payload.company_website,
        )
    if payload.location:
        job.location = get_or_create_location(session, payload.location)

    session.add(job)
    try:
        session.flush()
    except IntegrityError as error:
        session.rollback()
        raise DuplicateJob(payload.source, payload.external_id) from error

    for field in _VOCABULARIES:
        _set_vocabulary(session, job, field, getattr(payload, field))
    _set_dco_titles(session, job, payload.dco_titles)

    session.flush()
    return job


def upsert(session: Session, payload: JobCreate) -> tuple[Job, bool]:
    """Insert the vacancy, or update it if (source, external_id) is known.

    Returns `(job, created)`. This is what makes re-ingesting a search result
    idempotent - a board re-lists the same vacancy every time it is queried.
    """
    existing = get_by_external_id(session, payload.source, payload.external_id)
    if existing is None:
        return create(session, payload), True

    for field, value in payload.model_dump(exclude=_RELATED_FIELDS).items():
        setattr(existing, field, value)

    if payload.company_name:
        existing.company = get_or_create_company(
            session,
            name=payload.company_name,
            slug=payload.company_slug,
            type=payload.company_type,
            website=payload.company_website,
        )
    if payload.location:
        existing.location = get_or_create_location(session, payload.location)

    for field in _VOCABULARIES:
        _set_vocabulary(session, existing, field, getattr(payload, field))
    _set_dco_titles(session, existing, payload.dco_titles)

    session.flush()
    return existing, False


# ---- read ----


def get(session: Session, job_id: int) -> Optional[Job]:
    return session.get(Job, job_id)


def get_or_404(session: Session, job_id: int) -> Job:
    job = get(session, job_id)
    if job is None:
        raise JobNotFound(job_id)
    return job


def get_by_external_id(session: Session, source: str, external_id: str) -> Optional[Job]:
    return session.scalar(
        select(Job).where(Job.source == source, Job.external_id == external_id)
    )


def _listing_query(
    *,
    search: Optional[str] = None,
    company_id: Optional[int] = None,
    city: Optional[str] = None,
    province: Optional[str] = None,
    category: Optional[str] = None,
    industry: Optional[str] = None,
    education_level: Optional[str] = None,
    contract_type: Optional[str] = None,
    career_level: Optional[str] = None,
    status: Optional[str] = None,
    source: Optional[str] = None,
    min_salary: Optional[int] = None,
    posted_since: Optional[object] = None,
) -> Select:
    query = select(Job)

    if search:
        # Match the employer's wording or the classifier's normalised title,
        # so "python developer" also finds "Software Developer (Python)".
        pattern = f"%{search}%"
        query = query.where(
            or_(
                Job.title.ilike(pattern),
                Job.function_title.ilike(pattern),
                Job.dco_title.ilike(pattern),
            )
        )
    if company_id is not None:
        query = query.where(Job.company_id == company_id)
    if city or province:
        query = query.join(Location, Job.location_id == Location.id)
        if city:
            query = query.where(Location.city.ilike(city))
        if province:
            query = query.where(Location.province.ilike(province))
    if career_level:
        query = query.where(Job.career_level == career_level)
    if status:
        query = query.where(Job.status == status)
    if source:
        query = query.where(Job.source == source)
    if min_salary is not None:
        # A vacancy qualifies when the top of its band reaches the floor.
        query = query.where(Job.salary_max >= min_salary)
    if posted_since is not None:
        query = query.where(Job.start_date >= posted_since)

    for value, (model, association, column) in (
        (category, _VOCABULARIES["categories"]),
        (industry, _VOCABULARIES["industries"]),
        (education_level, _VOCABULARIES["education_levels"]),
        (contract_type, _VOCABULARIES["contract_types"]),
    ):
        if value:
            query = query.where(
                Job.id.in_(
                    select(association.c.job_id)
                    .join(model, model.id == association.c[column])
                    .where(model.name == value)
                )
            )

    return query


def list_jobs(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "start_date",
    descending: bool = True,
    **filters,
) -> Sequence[Job]:
    """Page through vacancies, newest first by default."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    column = getattr(Job, order_by, None)
    if column is None:
        raise ValueError(f"cannot order by unknown column {order_by!r}")

    query = _listing_query(**filters)
    ordering = column.desc().nullslast() if descending else column.asc().nullsfirst()
    query = query.order_by(ordering, Job.id).limit(limit).offset(offset)
    return session.scalars(query).unique().all()


def count(session: Session, **filters) -> int:
    query = _listing_query(**filters)
    return session.scalar(select(func.count()).select_from(query.subquery())) or 0


def list_vocabulary(session: Session, field: str) -> Sequence[str]:
    """Every known value of one vocabulary, alphabetically.

    Cheap because the values live in their own table - no scan over jobs.
    """
    if field not in _VOCABULARIES:
        raise ValueError(f"unknown vocabulary {field!r}")
    model, _, _ = _VOCABULARIES[field]
    return session.scalars(select(model.name).order_by(model.name)).all()


# ---- update ----


def update(session: Session, job_id: int, payload: JobUpdate) -> Job:
    """Apply a partial update to the vacancy's own columns."""
    job = get_or_404(session, job_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(job, field, value)
    session.flush()
    return job


def set_marks(
    session: Session,
    job_id: int,
    *,
    applied: Optional[bool] = None,
    archived: Optional[bool] = None,
) -> Job:
    """Mark a vacancy applied and/or archived, or clear either mark.

    `None` leaves a mark as it is. Setting a mark that is already set keeps its
    original timestamp, so a double click does not move "applied on" forward.
    """
    job = get_or_404(session, job_id)
    now = datetime.now(timezone.utc)
    for column, wanted in (("applied_at", applied), ("archived_at", archived)):
        if wanted is None:
            continue
        if not wanted:
            setattr(job, column, None)
        elif getattr(job, column) is None:
            setattr(job, column, now)
    session.flush()
    return job


# ---- delete ----


def delete(session: Session, job_id: int) -> None:
    """Delete a vacancy.

    The ORM removes its association rows and classifier titles (the database
    also has ON DELETE CASCADE as a backstop for non-ORM writes). The company
    and location are left alone - they are shared with other vacancies.
    """
    job = get_or_404(session, job_id)
    session.delete(job)
    session.flush()


def delete_expired(session: Session, before: object) -> int:
    """Delete vacancies whose expiration date has passed. Returns the count."""
    stale = session.scalars(
        select(Job).where(Job.expiration_date.is_not(None), Job.expiration_date < before)
    ).all()
    for job in stale:
        delete(session, job.id)
    return len(stale)
