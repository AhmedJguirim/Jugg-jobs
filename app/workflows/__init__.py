"""DBOS workflows. Importing this module registers them with DBOS."""

from app.workflows.enrichment import (
    APPLICATION_VERSION,
    COMPANY_QUEUE,
    INGEST_QUEUE,
    LLM_QUEUE,
    enrich_job_workflow,
    enrich_pending_workflow,
    ingest_page_workflow,
    register_queues,
    scrape_and_enrich_workflow,
    scrape_company_workflow,
    scrape_companies_workflow,
)

__all__ = [
    "APPLICATION_VERSION",
    "COMPANY_QUEUE",
    "INGEST_QUEUE",
    "LLM_QUEUE",
    "enrich_job_workflow",
    "enrich_pending_workflow",
    "ingest_page_workflow",
    "register_queues",
    "scrape_and_enrich_workflow",
    "scrape_company_workflow",
    "scrape_companies_workflow",
]
