"""Read-only API behind the companies browser.

Paginated server-side, unlike `/api/board`: there are 3000+ employers, which is
past the point where shipping the whole table to the browser and filtering it
there is reasonable.
"""

from __future__ import annotations

from typing import Any, Optional

from dbos import DBOS
from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.db.session import session_scope
from app.repositories import company as company_repo
from app.repositories.company import CompanyNotFound
from app.services.company_scrape import query_for
from app.workflows import enrichment as flow

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
        # Scrape bookkeeping: "never looked" and "looked, found nothing" are
        # different states, and only the timestamp distinguishes them.
        "last_scraped_at": company.last_scraped_at.isoformat() if company.last_scraped_at else None,
        "last_scrape_vacancies": company.last_scrape_vacancies,
        "search_query": query_for(company) if (company.name or links["nationalevacaturebank"]) else None,
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


# ---- scraping one employer ----


@router.post("/api/companies/{company_id}/scrape")
def scrape_company(company_id: int, body: Optional[dict] = None) -> dict[str, Any]:
    """Run this employer's own job-board search, then translate and grade.

    Returns immediately with a workflow id; the work runs durably in the
    background. Enrichment is queued one vacancy at a time on the `llm` queue,
    so pressing this for several employers queues the work rather than starting
    several models.
    """
    body = body or {}

    with session_scope() as session:
        try:
            company = company_repo.get_or_404(session, company_id)
        except CompanyNotFound as error:
            raise HTTPException(status_code=404, detail="company not found") from error
        query = query_for(company)
        name = company.name

    handle = DBOS.start_workflow(
        flow.scrape_company_workflow,
        company_id,
        int(body.get("limit", 50)),
        int(body["max_pages"]) if body.get("max_pages") else None,
        body.get("target_language", "en"),
        body.get("profile") or get_settings().llm_default_profile,
        bool(body.get("enrich", True)),
    )
    return {"workflow_id": handle.workflow_id, "company": name, "query": query}


@router.get("/api/companies/scrape/{workflow_id}")
def scrape_status(workflow_id: str) -> dict[str, Any]:
    """Poll a running company scrape."""
    handle = DBOS.retrieve_workflow(workflow_id)
    status = handle.get_status()
    if status.status != "SUCCESS":
        return {"status": status.status}
    return {"status": status.status, "result": handle.get_result()}


@router.post("/api/companies/scrape/{workflow_id}/cancel")
def cancel_scrape(workflow_id: str) -> dict[str, Any]:
    """Stop a running scrape and everything it queued.

    `cancel_children=True` matters: the scrape's real cost is the enrichment
    workflows it put on the `llm` queue, and cancelling only the parent would
    leave those grinding through the GPU one by one.

    Whatever was already stored stays stored. Pages are committed as they are
    fetched, so cancelling keeps the vacancies found so far rather than
    discarding the work.
    """
    # DBOS treats cancelling an unknown id as a no-op, so existence is checked
    # first - otherwise a typo in a workflow id would look like a success.
    try:
        status = DBOS.retrieve_workflow(workflow_id).get_status()
    except Exception as error:
        raise HTTPException(status_code=404, detail="workflow not found") from error
    if status is None:
        raise HTTPException(status_code=404, detail="workflow not found")

    DBOS.cancel_workflow(workflow_id, cancel_children=True)
    return {"workflow_id": workflow_id, "status": "CANCELLED"}
