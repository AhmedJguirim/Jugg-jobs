"""Translate an NVB API vacancy into the application's own `JobCreate`.

Kept apart from both the API models and the repository so the boundary is
explicit: when the board changes a field name, this file changes and nothing
else does.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.db.models.job import SOURCE_NVB
from app.integrations.nvb.schema import Job as NvbJob
from app.integrations.nvb.schema import JobSearchResponse
from app.schemas.job import DcoTitleBase, JobCreate, LocationBase, VocabularyTerms


def _as_date(value) -> Optional[date]:
    return value.date() if value is not None else None


def _as_decimal(value: Optional[str]) -> Optional[Decimal]:
    """Coordinates arrive as strings; ignore anything unparseable."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _vocabulary(values: list[str], primary: Optional[str]) -> VocabularyTerms:
    """Combine the API's plural list with its singular 'primary' field.

    The singular value is not always present in the list, so it is folded in
    rather than assumed.
    """
    terms = [v for v in values if v]
    if primary and primary not in terms:
        terms.insert(0, primary)
    return VocabularyTerms(values=terms, primary=primary or (terms[0] if terms else None))


def to_location(job: NvbJob) -> Optional[LocationBase]:
    location = job.work_location
    if location is None or not location.city:
        return None

    country = location.country
    geo = location.geolocation
    return LocationBase(
        city=location.city,
        city_slug=location.city_slug,
        municipality=location.municipality,
        municipality_slug=location.municipality_slug,
        province=location.province,
        province_slug=location.province_slug,
        country_iso=country.iso if country else None,
        country_name=country.name if country else None,
        latitude=_as_decimal(geo.latitude) if geo else None,
        longitude=_as_decimal(geo.longitude) if geo else None,
    )


def to_job_create(job: NvbJob, *, include_raw: bool = True) -> JobCreate:
    """Map one API vacancy onto the application's create payload."""
    salary = job.salary
    hours = job.working_hours
    contact = job.contact
    apply = job.apply
    metadata = job.metadata

    return JobCreate(
        source=SOURCE_NVB,
        external_id=job.id,
        title=job.title or "(untitled)",
        description=job.description,
        status=job.status,
        origin=job.origin,
        company_name=job.company.name,
        company_slug=job.company.slug,
        company_type=job.company.type,
        company_website=job.company.website,
        location=to_location(job),
        function_title=job.function_title,
        function_title_slug=job.function_title_slug,
        dco_title=job.dco_title,
        dco_title_slug=job.dco_title_slug,
        isco=metadata.isco if metadata else None,
        jdco=metadata.jdco if metadata else None,
        job_titles=list(job.job_titles),
        career_level=job.career_level,
        salary_min=salary.min if salary else None,
        salary_max=salary.max if salary else None,
        salary_type=salary.type if salary else None,
        working_hours_min=hours.min if hours else None,
        working_hours_max=hours.max if hours else None,
        street=job.work_location.street if job.work_location else None,
        zip_code=job.work_location.zip_code if job.work_location else None,
        apply_option=apply.option if apply else None,
        apply_url=apply.url if apply else None,
        detail_url=job.url,
        number_of_applies=job.number_of_applies,
        contact_first_name=contact.first_name if contact else None,
        contact_last_name=contact.last_name if contact else None,
        contact_email=contact.email_address if contact else None,
        contact_phone=contact.phone_number if contact else None,
        start_date=_as_date(job.start_date),
        end_date=_as_date(job.end_date),
        expiration_date=_as_date(job.expiration_date),
        recruiter_id=job.recruiter_id,
        reference_id=job.reference_id,
        categories=_vocabulary(job.categories, None),
        industries=_vocabulary(job.industries, None),
        education_levels=_vocabulary(job.education_levels, job.education_level),
        contract_types=_vocabulary(job.contract_types, job.contract_type),
        dco_titles=[
            DcoTitleBase(
                rank=rank,
                title=entry.title,
                slug=entry.slug,
                generic_title=entry.generic_title,
                similarity=entry.similarity,
                source=entry.source,
            )
            for rank, entry in enumerate(job.dco_titles)
            if entry.title
        ],
        raw=job.model_dump(mode="json", by_alias=True) if include_raw else None,
    )


def to_job_creates(payload: dict, *, include_raw: bool = True) -> list[JobCreate]:
    """Map a whole search-result page."""
    page = JobSearchResponse.model_validate(payload)
    return [to_job_create(job, include_raw=include_raw) for job in page.jobs]
