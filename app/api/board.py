"""API behind the job board view.

Separated from `main.py` so the HTTP surface for the UI has one home and
`main.py` stays the entrypoint plus workflow wiring.

Shape of the contract: `/api/board` returns the whole enriched corpus *without*
the long description bodies, small enough (68 rows today) for the page to filter
and sort client-side with no round trips. The bodies are fetched per vacancy from
`/api/jobs/{id}` when one is opened, which keeps the list payload light while the
detail stays instant enough.

The one write is `PATCH /api/jobs/{id}/marks`: the user's own applied/archived
marks. Filtering on them is client-side like everything else, so the list
always carries every vacancy, marked or not (ADR-0009).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from app.db.models.company import Company
from app.db.models.enrichment import JobAssessment, JobTranslation
from app.db.models.job import Job
from app.db.models.location import Location
from app.config import get_settings
from app.db.session import session_scope
from app.repositories import enrichment as enrichment_repo
from app.repositories import job as job_repo
from app.repositories.job import JobNotFound
from app.text import sanitize_description

router = APIRouter(tags=["board"])


def _job_row(job: Job, assessment: Optional[JobAssessment], translation: Optional[JobTranslation]) -> dict[str, Any]:
    """One list row: everything the card and the filters need, no long text."""
    return {
        "id": job.id,
        "title": (translation.title if translation else None) or job.title,
        "title_original": job.title,
        "retitled": bool(translation and translation.title and translation.title != job.title),
        "company": job.company.name if job.company else None,
        "city": job.location.city if job.location else None,
        "province": job.location.province if job.location else None,
        "salary": job.salary_range,
        "hours": (
            f"{job.working_hours_min}-{job.working_hours_max}"
            if job.working_hours_min is not None
            else None
        ),
        "posted": job.start_date.isoformat() if job.start_date else None,
        "career_level": job.career_level,
        "dco_title": job.dco_title,
        "education": [term.name for term in job.education_levels],
        "contract": [term.name for term in job.contract_types],
        "categories": [term.name for term in job.categories],
        "url": job.detail_url,
        "applied_at": job.applied_at.isoformat() if job.applied_at else None,
        "archived_at": job.archived_at.isoformat() if job.archived_at else None,
        "translated": bool(translation and not translation.skipped),
        "source_language": translation.source_language if translation else None,
        # Enrichment. Absent until the vacancy has been through the GPU queue.
        "assessed": assessment is not None,
        "fit_score": assessment.fit_score if assessment else None,
        "verdict": assessment.verdict if assessment else None,
        "blocked": bool(assessment.blocked) if assessment else False,
        "dutch_required": bool(assessment.dutch_required) if assessment else False,
        "sponsorship_required": bool(assessment.sponsorship_required) if assessment else False,
        "eligibility": (assessment.eligibility if assessment else None) or "unknown",
        "location_policy": assessment.location_policy if assessment else None,
        "requires_existing_permit": bool(assessment.requires_existing_permit) if assessment else False,
        "offers_sponsorship": bool(assessment.offers_sponsorship) if assessment else False,
        "stack_match": assessment.stack_match if assessment else None,
        "seniority_fit": assessment.seniority_fit if assessment else None,
        "seniority": assessment.seniority if assessment else None,
        "remote": assessment.remote if assessment else None,
        "summary": assessment.summary if assessment else None,
        "reasoning": assessment.reasoning if assessment else None,
        "matched": list(assessment.matched_requirements) if assessment else [],
        "missing": list(assessment.missing_requirements) if assessment else [],
        "red_flags": list(assessment.red_flags) if assessment else [],
    }


@router.get("/api/board")
def board(profile: str | None = None) -> dict[str, Any]:
    """The whole enriched corpus plus headline stats, in one request."""
    profile = profile or get_settings().llm_default_profile
    with session_scope() as session:
        rows = session.execute(
            select(Job, JobAssessment, JobTranslation)
            .outerjoin(
                JobAssessment,
                (JobAssessment.job_id == Job.id) & (JobAssessment.profile_name == profile),
            )
            .outerjoin(
                JobTranslation,
                (JobTranslation.job_id == Job.id) & (JobTranslation.target_language == "en"),
            )
            .order_by(JobAssessment.fit_score.desc().nullslast(), Job.id)
        ).unique().all()

        jobs = [_job_row(job, assessment, translation) for job, assessment, translation in rows]
        progress = enrichment_repo.enrichment_progress(session, profile_name=profile)

        companies = session.scalar(select(func.count()).select_from(Company)) or 0
        locations = session.scalar(select(func.count()).select_from(Location)) or 0

    verdicts: dict[str, int] = {}
    for row in jobs:
        if row["verdict"]:
            verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1

    actionable = [r for r in jobs if r["fit_score"] is not None and r["fit_score"] >= 55 and not r["blocked"]]

    return {
        "profile": profile,
        "jobs": jobs,
        "stats": {
            **progress,
            "total_jobs": len(jobs),
            "companies": companies,
            "locations": locations,
            "verdicts": verdicts,
            "actionable": len(actionable),
            "blocked": sum(1 for r in jobs if r["blocked"]),
            "applied": sum(1 for r in jobs if r["applied_at"]),
            "archived": sum(1 for r in jobs if r["archived_at"]),
            "needs_dutch": sum(1 for r in jobs if r["dutch_required"]),
        "needs_sponsorship": sum(1 for r in jobs if r["sponsorship_required"]),
        "eligibility": {
            tier: sum(1 for r in jobs if r["eligibility"] == tier)
            for tier in ("remote_ok", "relocation", "blocked")
        },
        "offers_sponsorship": sum(1 for r in jobs if r["offers_sponsorship"]),
        "stack_tiers": {
            tier: sum(1 for r in jobs if r["stack_match"] == tier)
            for tier in ("core", "proficient", "familiar", "unrelated", "non_technical")
        },
        },
        "facets": {
            "cities": sorted({r["city"] for r in jobs if r["city"]}),
            "companies": sorted({r["company"] for r in jobs if r["company"]}),
            "verdicts": ["strong_match", "possible", "weak", "irrelevant"],
            "seniorities": sorted({r["seniority"] for r in jobs if r["seniority"]}),
            "stack_tiers": ["core", "proficient", "familiar", "unrelated", "non_technical"],
            "eligibility": ["remote_ok", "relocation", "blocked"],
        },
    }


class MarksUpdate(BaseModel):
    """Omitted fields are left alone; `false` clears a mark."""

    applied: bool | None = None
    archived: bool | None = None


@router.patch("/api/jobs/{job_id}/marks")
def update_marks(job_id: int, marks: MarksUpdate) -> dict[str, Any]:
    """Mark a vacancy applied and/or archived, or clear either mark."""
    try:
        with session_scope() as session:
            job = job_repo.set_marks(
                session, job_id, applied=marks.applied, archived=marks.archived
            )
            return {
                "id": job.id,
                "applied_at": job.applied_at.isoformat() if job.applied_at else None,
                "archived_at": job.archived_at.isoformat() if job.archived_at else None,
            }
    except JobNotFound:
        raise HTTPException(status_code=404, detail="job not found")


@router.get("/api/jobs/{job_id}")
def job_detail(job_id: int, profile: str | None = None) -> dict[str, Any]:
    """One vacancy including both texts: the English translation and the original.

    The original is returned sanitised rather than raw, because that is what the
    model actually read - useful when a translation looks wrong.

    `profile` defaults to the configured one, as for `/api/board`. Without that
    default the detail came back with no assessment (nothing matches a NULL
    profile), so the drawer lost the verdict, flags and model it had on the card.
    """
    profile = profile or get_settings().llm_default_profile
    with session_scope() as session:
        row = session.execute(
            select(Job, JobAssessment, JobTranslation)
            .outerjoin(
                JobAssessment,
                (JobAssessment.job_id == Job.id) & (JobAssessment.profile_name == profile),
            )
            .outerjoin(
                JobTranslation,
                (JobTranslation.job_id == Job.id) & (JobTranslation.target_language == "en"),
            )
            .where(Job.id == job_id)
        ).unique().first()

        if row is None:
            raise HTTPException(status_code=404, detail="job not found")

        job, assessment, translation = row
        cleaned = sanitize_description(job.description)

        detail = _job_row(job, assessment, translation)
        detail.update(
            {
                "description_en": translation.description if translation else None,
                "description_original": cleaned.text,
                "original_chars": cleaned.original_chars,
                "clean_chars": cleaned.clean_chars,
                "apply_url": job.apply_url,
                "contact_email": job.contact_email,
                "model": (translation.model if translation else None)
                or (assessment.model if assessment else None),
                "gpu_seconds": round(
                    ((translation.duration_ms if translation else 0)
                     + (assessment.duration_ms if assessment else 0)) / 1000,
                    1,
                ),
                "chunks": translation.chunks if translation else 0,
            }
        )
        return detail
