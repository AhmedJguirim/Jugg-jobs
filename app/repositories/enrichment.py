"""CRUD for what the model produced: grades, and the cleaned text they were made from.

`assessment_is_current` is the GPU's off switch: at tens of seconds of GPU time
per posting, the cheapest optimisation is not running the model at all. A posting
is graded again only when its cleaned text or the prompt version changed.

`job_translations` holds the cleaned English text the grade was made from (the
board's detail drawer shows it). The table and its name predate this project's
worldwide focus, when it held machine translations of Dutch vacancies.

Same contract as the other repositories: explicit `Session`, `flush()` but never
`commit()`.
"""

from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.enrichment import JobAssessment, JobTranslation
from app.db.models.job import Job


class EnrichmentRepositoryError(Exception):
    """Base class for enrichment repository failures."""


# ---- translations ----


def get_translation(
    session: Session, job_id: int, target_language: str = "en"
) -> Optional[JobTranslation]:
    return session.scalar(
        select(JobTranslation).where(
            JobTranslation.job_id == job_id,
            JobTranslation.target_language == target_language,
        )
    )


def upsert_translation(
    session: Session,
    *,
    job_id: int,
    source_hash: str,
    prompt_version: str,
    target_language: str = "en",
    source_language: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    skipped: bool = False,
    skip_reason: Optional[str] = None,
    model: Optional[str] = None,
    chunks: int = 0,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int = 0,
) -> JobTranslation:
    """Store or replace the translation for one job and language."""
    values = dict(
        source_hash=source_hash,
        prompt_version=prompt_version,
        source_language=source_language,
        title=title,
        description=description,
        skipped=skipped,
        skip_reason=skip_reason,
        model=model,
        chunks=chunks,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )

    translation = get_translation(session, job_id, target_language)
    if translation is None:
        translation = JobTranslation(
            job_id=job_id, target_language=target_language, **values
        )
        session.add(translation)
    else:
        for field, value in values.items():
            setattr(translation, field, value)

    session.flush()
    return translation


# ---- assessments ----


def get_assessment(
    session: Session, job_id: int, profile_name: str = "default"
) -> Optional[JobAssessment]:
    return session.scalar(
        select(JobAssessment).where(
            JobAssessment.job_id == job_id,
            JobAssessment.profile_name == profile_name,
        )
    )


def upsert_assessment(
    session: Session,
    *,
    job_id: int,
    source_hash: str,
    prompt_version: str,
    fit_score: int,
    verdict: str,
    profile_name: str = "default",
    seniority: Optional[str] = None,
    seniority_fit: Optional[str] = None,
    stack_match: Optional[str] = None,
    remote: Optional[str] = None,
    dutch_required: bool = False,
    sponsorship_required: bool = False,
    eligibility: Optional[str] = None,
    location_policy: Optional[str] = None,
    requires_existing_permit: bool = False,
    offers_sponsorship: bool = False,
    blocked: bool = False,
    summary: Optional[str] = None,
    reasoning: Optional[str] = None,
    matched_requirements: Optional[list[str]] = None,
    missing_requirements: Optional[list[str]] = None,
    red_flags: Optional[list[str]] = None,
    details: Optional[dict] = None,
    model: Optional[str] = None,
    repaired: bool = False,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int = 0,
) -> JobAssessment:
    """Store or replace the assessment for one job and profile."""
    values = dict(
        source_hash=source_hash,
        prompt_version=prompt_version,
        fit_score=fit_score,
        verdict=verdict,
        seniority=seniority,
        seniority_fit=seniority_fit,
        stack_match=stack_match,
        remote=remote,
        dutch_required=dutch_required,
        sponsorship_required=sponsorship_required,
        eligibility=eligibility,
        location_policy=location_policy,
        requires_existing_permit=requires_existing_permit,
        offers_sponsorship=offers_sponsorship,
        blocked=blocked,
        summary=summary,
        reasoning=reasoning,
        matched_requirements=matched_requirements or [],
        missing_requirements=missing_requirements or [],
        red_flags=red_flags or [],
        details=details,
        model=model,
        repaired=repaired,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )

    assessment = get_assessment(session, job_id, profile_name)
    if assessment is None:
        assessment = JobAssessment(job_id=job_id, profile_name=profile_name, **values)
        session.add(assessment)
    else:
        for field, value in values.items():
            setattr(assessment, field, value)

    session.flush()
    return assessment


def assessment_is_current(
    assessment: Optional[JobAssessment],
    *,
    source_hash: str,
    prompt_version: str,
) -> bool:
    if assessment is None:
        return False
    return (
        assessment.source_hash == source_hash
        and assessment.prompt_version == prompt_version
    )


# ---- reading the results ----


def enrichment_progress(session: Session, *, profile_name: str = "default") -> dict:
    """Counts for a progress report: how much of the corpus is graded."""
    total = session.scalar(
        select(func.count()).select_from(Job).where(Job.description.is_not(None))
    ) or 0
    translated = session.scalar(
        select(func.count()).select_from(JobTranslation).where(
            JobTranslation.target_language == "en"
        )
    ) or 0
    assessed = session.scalar(
        select(func.count()).select_from(JobAssessment).where(
            JobAssessment.profile_name == profile_name
        )
    ) or 0
    gpu_ms = session.scalar(
        select(func.coalesce(func.sum(JobTranslation.duration_ms), 0))
    ) or 0
    gpu_ms += session.scalar(
        select(func.coalesce(func.sum(JobAssessment.duration_ms), 0))
    ) or 0

    return {
        "jobs_with_description": total,
        "translated": translated,
        "assessed": assessed,
        "translation_pending": max(0, total - translated),
        "assessment_pending": max(0, total - assessed),
        "gpu_seconds_spent": round(gpu_ms / 1000, 1),
    }
