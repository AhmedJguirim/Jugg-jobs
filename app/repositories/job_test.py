"""Storage for parsed listings in `jobs_test`.

Same contract as the other repositories (explicit `Session`, `flush()`, never
`commit()`). Company and location are resolved with the helpers the `jobs`
repository already uses, so they are shared rows, not copies.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.job_test import JobTest
from app.repositories.job import get_or_create_company, get_or_create_location
from app.schemas.job import JobCreate

# JobCreate fields that are not columns of the vacancy itself.
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


def get_by_external_id(session: Session, source: str, external_id: str) -> Optional[JobTest]:
    return session.scalar(
        select(JobTest).where(JobTest.source == source, JobTest.external_id == external_id)
    )


def upsert(session: Session, payload: JobCreate) -> tuple[JobTest, bool]:
    """Insert the job, or refresh it if (source, external_id) is known.

    Returns `(job, created)`, so processing the same page twice updates one row.
    The user's marks (`applied_at`, `archived_at`) are not in `JobCreate` and so
    survive a refresh.
    """
    job = get_by_external_id(session, payload.source, payload.external_id)
    created = job is None
    values = payload.model_dump(exclude=_RELATED_FIELDS)
    if created:
        job = JobTest(**values)
        session.add(job)
    else:
        for field, value in values.items():
            setattr(job, field, value)

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

    session.flush()
    return job, created
