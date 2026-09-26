"""Scrape one employer's own job-board search.

Every seed company carries a pre-built Nationale Vacaturebank search URL, e.g.

    https://www.nationalevacaturebank.nl/vacature/zoeken?query=Proxima%20Beta

The query inside it is what a person would have typed to find that employer's
vacancies, so it is exactly what the ingest flow needs. This module extracts it
and records the outcome; the ordering of the work is the workflow's job.

Kept free of DBOS so the extraction can be tested without a runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy.orm import Session

from app.db.models.company import Company
from app.logging_setup import log

# The parameter the board uses for a free-text search.
QUERY_PARAM = "query"


class NoSearchQuery(ValueError):
    """The employer has no usable search query."""


def query_from_link(link: Optional[str]) -> Optional[str]:
    """Pull the search term out of a Nationale Vacaturebank search URL.

    All 2901 seed links share the shape `?query=<urlencoded>`, but this parses
    the URL properly rather than slicing strings: an employer name legitimately
    contains `&`, `+` and `#` (e.g. "R&D Partners"), any of which would break a
    naive split.

    Returns None when there is no link or no query in it.
    """
    if not link:
        return None

    parsed = urlparse(link)
    values = parse_qs(parsed.query).get(QUERY_PARAM)
    if not values:
        return None

    # parse_qs already decodes; unquote again is wrong, so only strip.
    query = values[0].strip()
    return query or None


def query_for(company: Company) -> str:
    """The search term to scrape this employer with.

    Falls back to the company name when no link is on file - employers
    discovered from a vacancy have no seed link, and their name is a reasonable
    search term. Raises when neither is usable.
    """
    query = query_from_link(company.nationalevacaturebank_jobs_link)
    if query:
        return query

    name = (company.name or "").strip()
    if name:
        log.info(
            "company %s has no search link; falling back to its name", company.id
        )
        return name

    raise NoSearchQuery(f"company {company.id} has neither a search link nor a name")


def record_scrape(
    session: Session,
    company_id: int,
    *,
    vacancies: int,
    when: Optional[datetime] = None,
) -> None:
    """Stamp when this employer was last scraped and how much it yielded.

    Written even when the scrape found nothing: "we looked and found nothing" is
    a different fact from "we never looked", and only the timestamp separates
    them.
    """
    company = session.get(Company, company_id)
    if company is None:
        return
    company.last_scraped_at = when or datetime.now(timezone.utc)
    company.last_scrape_vacancies = vacancies
    session.flush()
