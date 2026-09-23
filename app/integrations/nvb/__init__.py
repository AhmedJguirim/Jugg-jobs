"""Nationale Vacaturebank integration: HTTP client + response models."""

from app.integrations.nvb.client import BASE_URL, DEFAULT_HEADERS, fetch_jobs_page
from app.integrations.nvb.schema import Job, JobSearchResponse

__all__ = ["BASE_URL", "DEFAULT_HEADERS", "fetch_jobs_page", "Job", "JobSearchResponse"]
