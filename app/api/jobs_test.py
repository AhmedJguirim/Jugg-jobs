"""Read-only HTTP API behind the `/jobs-test` page: what the listing parsers stored.

The list leaves out the description (a whole posting each); the detail returns
every column, so the drawer can show what a parser did and did not fill in.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.db.session import session_scope
from app.repositories import job_test as repo

router = APIRouter(tags=["jobs-test"])


def _salary(job) -> str | None:
    if job.salary_min is None:
        return None
    amount = f"{job.salary_min:,}" if job.salary_min == job.salary_max else f"{job.salary_min:,} - {job.salary_max:,}"
    return f"{amount} ({job.salary_type})" if job.salary_type else amount


def _summary(job) -> dict:
    sections = (job.raw or {}).get("sections") or {}
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company.name if job.company else None,
        "city": job.location.city if job.location else None,
        "source": job.source,
        "salary": _salary(job),
        "employment_type": sections.get("Employment Type"),
        "location_type": sections.get("Location Type"),
        "url": job.detail_url,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


@router.get("/api/jobs-test")
def list_jobs_test():
    with session_scope() as session:
        return {"items": [_summary(job) for job in repo.list_all(session)]}


@router.get("/api/jobs-test/{job_id}")
def get_job_test(job_id: int):
    with session_scope() as session:
        job = repo.get(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        raw = job.raw or {}
        return {
            **_summary(job),
            "external_id": job.external_id,
            "status": job.status,
            "origin": job.origin,
            "province": job.location.province if job.location else None,
            "company_website": job.company.website if job.company else None,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "salary_type": job.salary_type,
            "apply_url": job.apply_url,
            "apply_option": job.apply_option,
            "job_titles": job.job_titles,
            "raw_listing_id": raw.get("raw_listing_id"),
            "sections": raw.get("sections") or {},
            "description": job.description,
        }
