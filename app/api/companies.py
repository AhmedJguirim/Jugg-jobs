"""Read-only API behind the companies browser.

Paginated server-side, unlike `/api/board`: there are 3000+ employers, which is
past the point where shipping the whole table to the browser and filtering it
there is reasonable.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.db.session import session_scope
from app.repositories import company as company_repo
from app.repositories.company import CompanyNotFound

router = APIRouter(tags=["companies"])

# How the seed file and the vacancy feed differ, which is the main thing a
# reader needs to understand about this table.
SOURCE_SEED = "seed"
SOURCE_VACANCY = "vacancy"


def _job_board_links(company) -> dict[str, Optional[str]]:
    return {
        "google": company.google_jobs_link,
        "linkedin": company.linkedin_jobs_link,
        "indeed": company.indeed_jobs_link,
        "glassdoor": company.glassdoor_jobs_link,
        "nationalevacaturebank": company.nationalevacaturebank_jobs_link,
    }


def _company_row(company, vacancies: int, best_fit, latest) -> dict[str, Any]:
    links = _job_board_links(company)
    return {
        "id": company.id,
        "name": company.name,
        "type": company.type,
        "slug": company.slug,
        "website": company.website,
        "province": company.province,
        "vacancies": vacancies,
        "best_fit": best_fit,
        "latest_posting": latest.isoformat() if latest else None,
        # Which feed this employer came from: the seed file brought search
        # links, vacancies brought type/slug. Useful for filtering and for
        # explaining why half the columns are empty on any given row.
        "origin": SOURCE_SEED if links["google"] else SOURCE_VACANCY,
        "has_links": bool(links["google"]),
        "links": links,
    }


@router.get("/api/companies")
def list_companies(
    q: Optional[str] = None,
    province: Optional[str] = None,
    has_jobs: Optional[bool] = None,
    has_links: Optional[bool] = None,
    order_by: str = "name",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    profile: Optional[str] = None,
) -> dict[str, Any]:
    """A page of employers with their vacancy statistics."""
    profile = profile or get_settings().llm_default_profile

    filters = {
        "search": q,
        "province": province,
        "has_jobs": has_jobs,
        "has_links": has_links,
    }

    try:
        with session_scope() as session:
            rows = company_repo.browse(
                session,
                limit=limit,
                offset=offset,
                order_by=order_by,
                profile_name=profile,
                **filters,
            )
            total = company_repo.browse_count(session, profile_name=profile, **filters)
            items = [_company_row(*row) for row in rows]
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
    }


@router.get("/api/companies/facets")
def company_facets() -> dict[str, Any]:
    """Values for the filter controls, plus headline counts."""
    with session_scope() as session:
        return {
            "provinces": list(company_repo.provinces(session)),
            "total": company_repo.browse_count(session),
            "with_jobs": company_repo.browse_count(session, has_jobs=True),
            "with_links": company_repo.browse_count(session, has_links=True),
        }


@router.get("/api/companies/{company_id}")
def company_detail(company_id: int, profile: Optional[str] = None) -> dict[str, Any]:
    """One employer, their job-board links, and every vacancy ingested for them."""
    profile = profile or get_settings().llm_default_profile

    with session_scope() as session:
        try:
            company, rows = company_repo.with_vacancies(
                session, company_id, profile_name=profile
            )
        except CompanyNotFound as error:
            raise HTTPException(status_code=404, detail="company not found") from error

        vacancies = [
            {
                "id": job.id,
                "title": job.title,
                "city": job.location.city if job.location else None,
                "salary": job.salary_range,
                "posted": job.start_date.isoformat() if job.start_date else None,
                "url": job.detail_url,
                "fit_score": assessment.fit_score if assessment else None,
                "verdict": assessment.verdict if assessment else None,
                "stack_match": assessment.stack_match if assessment else None,
                "blocked": bool(assessment.blocked) if assessment else False,
                "summary": assessment.summary if assessment else None,
            }
            for job, assessment in rows
        ]

        best = [v["fit_score"] for v in vacancies if v["fit_score"] is not None]
        detail = _company_row(company, len(vacancies), max(best) if best else None, None)
        detail.update(
            {
                "vacancy_list": vacancies,
                "created_at": company.created_at.isoformat() if company.created_at else None,
                "updated_at": company.updated_at.isoformat() if company.updated_at else None,
            }
        )
        return detail
