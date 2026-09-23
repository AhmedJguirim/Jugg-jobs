"""Nationale Vacaturebank integration: HTTP client, response models, mapper."""

from app.integrations.nvb.client import BASE_URL, DEFAULT_HEADERS, fetch_jobs_page
from app.integrations.nvb.mapper import to_job_create, to_job_creates
from app.integrations.nvb.schema import Job, JobSearchResponse

__all__ = [
    "BASE_URL",
    "DEFAULT_HEADERS",
    "fetch_jobs_page",
    "Job",
    "JobSearchResponse",
    "to_job_create",
    "to_job_creates",
]
