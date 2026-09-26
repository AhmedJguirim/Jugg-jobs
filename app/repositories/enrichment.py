"""CRUD for LLM-derived data, plus the queries that decide what needs the GPU.

The `pending_*` functions are the important part: at ~16s of GPU time per
vacancy, the cheapest optimisation is not running the model at all. A job needs
work only when it has no result, or when the result was produced from different
source text or an older prompt version.

Same contract as the other repositories: explicit `Session`, `flush()` but never
`commit()`.
"""

from typing import Optional, Sequence

from sqlalchemy import Select, and_, or_, select
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


def _needs_translation_clause(prompt_version: Optional[str], target_language: str):
    """A job needs translating when no row exists, the hash moved, or the prompt aged."""
    existing = select(JobTranslation).where(
        JobTranslation.job_id == Job.id,
        JobTranslation.target_language == target_language,
    )
    fresh_conditions = [JobTranslation.job_id == Job.id]
    if prompt_version:
        fresh_conditions.append(JobTranslation.prompt_version == prompt_version)
    return ~existing.where(and_(*fresh_conditions)).exists()


def pending_translation(
    session: Session,
    *,
    target_language: str = "en",
    prompt_version: Optional[str] = None,
    limit: int = 100,
    only_with_description: bool = True,
) -> Sequence[Job]:
    """Jobs with no current translation, oldest postings first.

    Staleness from a changed description is detected by the caller, which knows
    the freshly computed hash; this query covers "never done" and "done by an
    older prompt".
    """
    query = select(Job).where(_needs_translation_clause(prompt_version, target_language))
    if only_with_description:
        query = query.where(Job.description.is_not(None))
    query = query.order_by(Job.start_date.desc().nullslast(), Job.id).limit(limit)
    return session.scalars(query).unique().all()


def translation_is_current(
    translation: Optional[JobTranslation],
    *,
    source_hash: str,
    prompt_version: str,
) -> bool:
    """True when a stored translation still matches the text and the prompt.

    This is the GPU's off switch: re-ingesting a vacancy whose text did not
    change must not re-run the model.
    """
    if translation is None:
        return False
    return (
        translation.source_hash == source_hash
        and translation.prompt_version == prompt_version
    )


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
    remote: Optional[str] = None,
    dutch_required: bool = False,
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
        remote=remote,
        dutch_required=dutch_required,
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


def pending_assessment(
    session: Session,
    *,
    profile_name: str = "default",
    prompt_version: Optional[str] = None,
    limit: int = 100,
    only_with_description: bool = True,
) -> Sequence[Job]:
    """Jobs not yet scored against this profile (or scored by an older prompt)."""
    existing = select(JobAssessment).where(
        JobAssessment.job_id == Job.id,
        JobAssessment.profile_name == profile_name,
    )
    conditions = [JobAssessment.job_id == Job.id]
    if prompt_version:
        conditions.append(JobAssessment.prompt_version == prompt_version)

    query = select(Job).where(~existing.where(and_(*conditions)).exists())
    if only_with_description:
        query = query.where(Job.description.is_not(None))
    query = query.order_by(Job.start_date.desc().nullslast(), Job.id).limit(limit)
    return session.scalars(query).unique().all()


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


def shortlist(
    session: Session,
    *,
    profile_name: str = "default",
    min_score: int = 55,
    include_blocked: bool = False,
    limit: int = 25,
    offset: int = 0,
) -> Sequence[tuple[Job, JobAssessment, Optional[JobTranslation]]]:
    """The point of the whole pipeline: best-fitting vacancies, in English.

    `include_blocked=False` hides vacancies that hit a dealbreaker (such as
    requiring fluent Dutch) while leaving their fit score intact, so turning the
    flag on shows the good roles that are out of reach rather than pretending
    they scored badly.
    """
    query = (
        select(Job, JobAssessment, JobTranslation)
        .join(JobAssessment, JobAssessment.job_id == Job.id)
        .outerjoin(
            JobTranslation,
            and_(
                JobTranslation.job_id == Job.id,
                JobTranslation.target_language == "en",
            ),
        )
        .where(
            JobAssessment.profile_name == profile_name,
            JobAssessment.fit_score >= min_score,
        )
    )
    if not include_blocked:
        query = query.where(JobAssessment.blocked.is_(False))

    query = (
        query.order_by(JobAssessment.fit_score.desc(), Job.start_date.desc().nullslast())
        .limit(limit)
        .offset(offset)
    )
    return [tuple(row) for row in session.execute(query).unique().all()]


def enrichment_progress(session: Session, *, profile_name: str = "default") -> dict:
    """Counts for a progress report: how much of the corpus is done."""
    from sqlalchemy import func

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
