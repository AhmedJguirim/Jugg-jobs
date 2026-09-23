"""Unit tests for the paginating ingest flow.

The page fetcher is stubbed, so these run without network access while still
writing to the test database through the real repository.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.company import Company
from app.db.models.job import Job
from app.db.models.location import Location
from app.repositories import job as job_repo
from app.services.job_ingest import ingest_query, store_page


def make_raw_job(external_id: str, *, company: str = "Odido", city: str = "Den Haag") -> dict:
    return {
        "id": external_id,
        "title": f"Engineer {external_id}",
        "status": "active",
        "company": {"name": company, "slug": company.lower(), "type": "direct_employer"},
        "workLocation": {"city": city, "province": "Zuid-Holland", "citySlug": city.lower()},
        "workingHours": {"min": 32, "max": 40},
        "categories": ["Automatisering/Internet"],
        "industries": ["ICT"],
        "educationLevel": "HBO",
        "educationLevels": ["HBO"],
        "contractType": "Vast",
        "contractTypes": ["Vast"],
        "jobTitles": ["engineer"],
        "dcoTitles": [{"title": "Engineer", "slug": "engineer", "similarity": 0.9}],
        "startDate": "2026-09-01T00:00:00Z",
        "_links": {"detail": {"href": f"https://example.test/{external_id}"}},
    }


def make_page(page: int, pages: int, total: int, job_ids: list[str], **job_kwargs) -> dict:
    return {
        "page": page,
        "limit": len(job_ids) or 10,
        "pages": pages,
        "total": total,
        "_embedded": {"jobs": [make_raw_job(i, **job_kwargs) for i in job_ids]},
    }


@pytest.fixture
def session_factory(session: Session):
    """Hand the service the test's transaction instead of a real session.

    `session_scope` would commit; here the outer test transaction is rolled
    back, so the service must not close or commit the session it is given.
    """

    @contextmanager
    def factory():
        yield session

    return factory


class TestPagination:
    def test_walks_every_page_reported_by_the_first_response(self, session_factory):
        pages = {
            1: make_page(1, 3, 6, ["a", "b"]),
            2: make_page(2, 3, 6, ["c", "d"]),
            3: make_page(3, 3, 6, ["e", "f"]),
        }

        stats = ingest_query(
            "Odido",
            limit=2,
            delay_seconds=0,
            fetch=lambda q, *, page, limit, sort: pages[page],
            session_factory=session_factory,
        )

        assert stats.pages_fetched == 3
        assert stats.jobs_seen == 6
        assert stats.created == 6

    def test_records_the_reported_totals(self, session_factory):
        stats = ingest_query(
            "Odido",
            delay_seconds=0,
            fetch=lambda q, *, page, limit, sort: make_page(1, 1, 43, ["a"]),
            session_factory=session_factory,
        )

        assert stats.total_reported == 43
        assert stats.pages_available == 1

    def test_single_page_stops_after_one_fetch(self, session_factory):
        calls: list[int] = []

        def fetch(query, *, page, limit, sort):
            calls.append(page)
            return make_page(1, 1, 1, ["a"])

        ingest_query("Odido", delay_seconds=0, fetch=fetch, session_factory=session_factory)

        assert calls == [1]

    def test_max_pages_caps_the_crawl(self, session_factory):
        calls: list[int] = []

        def fetch(query, *, page, limit, sort):
            calls.append(page)
            return make_page(page, 10, 100, ["a", "b"])

        stats = ingest_query(
            "Odido", limit=2, max_pages=2, delay_seconds=0, fetch=fetch,
            session_factory=session_factory,
        )

        assert calls == [1, 2]
        assert stats.pages_fetched == 2

    def test_empty_page_ends_the_crawl(self, session_factory):
        """A board that reports more pages than it serves must not loop."""
        calls: list[int] = []

        def fetch(query, *, page, limit, sort):
            calls.append(page)
            return make_page(page, 5, 2, [] if page > 1 else ["a"])

        ingest_query("Odido", delay_seconds=0, fetch=fetch, session_factory=session_factory)

        assert calls == [1, 2]

    def test_rejects_invalid_limit(self, session_factory):
        with pytest.raises(ValueError):
            ingest_query("Odido", limit=0, session_factory=session_factory)

    def test_rejects_invalid_max_pages(self, session_factory):
        with pytest.raises(ValueError):
            ingest_query("Odido", max_pages=0, session_factory=session_factory)


class TestPersistence:
    def test_stores_the_vacancies(self, session: Session, session_factory):
        ingest_query(
            "Odido",
            delay_seconds=0,
            fetch=lambda q, *, page, limit, sort: make_page(1, 1, 2, ["a", "b"]),
            session_factory=session_factory,
        )

        assert session.scalar(select(func.count()).select_from(Job)) == 2

    def test_creates_the_company_alongside_the_job(self, session: Session, session_factory):
        ingest_query(
            "Odido",
            delay_seconds=0,
            fetch=lambda q, *, page, limit, sort: make_page(1, 1, 1, ["a"]),
            session_factory=session_factory,
        )

        company = session.scalar(select(Company).where(Company.name == "Odido"))
        assert company is not None
        assert company.type == "direct_employer"

    def test_creates_the_location_alongside_the_job(self, session: Session, session_factory):
        ingest_query(
            "Odido",
            delay_seconds=0,
            fetch=lambda q, *, page, limit, sort: make_page(1, 1, 1, ["a"]),
            session_factory=session_factory,
        )

        assert session.scalar(select(Location).where(Location.city == "Den Haag")) is not None

    def test_counts_new_companies_and_locations(self, session_factory):
        stats = ingest_query(
            "Odido",
            delay_seconds=0,
            fetch=lambda q, *, page, limit, sort: make_page(1, 1, 1, ["a"]),
            session_factory=session_factory,
        )

        assert stats.companies_created == 1
        assert stats.locations_created == 1

    def test_shared_company_is_not_created_twice(self, session: Session, session_factory):
        pages = {
            1: make_page(1, 2, 4, ["a", "b"]),
            2: make_page(2, 2, 4, ["c", "d"]),
        }

        stats = ingest_query(
            "Odido", limit=2, delay_seconds=0,
            fetch=lambda q, *, page, limit, sort: pages[page],
            session_factory=session_factory,
        )

        assert stats.companies_created == 1
        assert session.scalar(select(func.count()).select_from(Company)) == 1

    def test_rerunning_updates_instead_of_duplicating(self, session: Session, session_factory):
        def fetch(query, *, page, limit, sort):
            return make_page(1, 1, 2, ["a", "b"])

        first = ingest_query("Odido", delay_seconds=0, fetch=fetch, session_factory=session_factory)
        second = ingest_query("Odido", delay_seconds=0, fetch=fetch, session_factory=session_factory)

        assert (first.created, first.updated) == (2, 0)
        assert (second.created, second.updated) == (0, 2)
        assert session.scalar(select(func.count()).select_from(Job)) == 2


class TestFailureHandling:
    def test_a_failing_page_does_not_abort_the_crawl(self, session_factory):
        def fetch(query, *, page, limit, sort):
            if page == 2:
                raise RuntimeError("boom")
            return make_page(page, 3, 6, ["a", "b"] if page == 1 else ["e", "f"])

        stats = ingest_query(
            "Odido", limit=2, delay_seconds=0, fetch=fetch, session_factory=session_factory
        )

        assert 2 in stats.failed_pages
        assert stats.ok is False

    def test_a_store_failure_is_recorded_and_the_walk_continues(
        self, session_factory, monkeypatch
    ):
        pages = {1: make_page(1, 2, 4, ["a", "b"]), 2: make_page(2, 2, 4, ["c", "d"])}
        calls = {"n": 0}
        real_upsert = job_repo.upsert

        def flaky(session, payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("write failed")
            return real_upsert(session, payload)

        monkeypatch.setattr(job_repo, "upsert", flaky)

        stats = ingest_query(
            "Odido", limit=2, delay_seconds=0,
            fetch=lambda q, *, page, limit, sort: pages[page],
            session_factory=session_factory,
        )

        assert stats.failed_pages == [1]
        assert stats.pages_fetched == 2
        assert stats.created == 2  # page 2 still landed

    def test_first_page_fetch_failure_yields_empty_stats(self, session_factory):
        def fetch(query, *, page, limit, sort):
            raise RuntimeError("unreachable")

        stats = ingest_query(
            "Odido", delay_seconds=0, fetch=fetch, session_factory=session_factory
        )

        assert stats.failed_pages == [1]
        assert stats.jobs_seen == 0


class TestStorePage:
    def test_returns_created_and_updated_counts(self, session: Session):
        page = make_page(1, 1, 2, ["a", "b"])

        assert store_page(session, page) == (2, 0)
        assert store_page(session, page) == (0, 2)


class TestStatsReporting:
    def test_summary_mentions_the_query_and_counts(self, session_factory):
        stats = ingest_query(
            "Odido",
            delay_seconds=0,
            fetch=lambda q, *, page, limit, sort: make_page(1, 1, 1, ["a"]),
            session_factory=session_factory,
        )

        summary = stats.summary()
        assert "'Odido'" in summary
        assert "1 created" in summary

    def test_as_dict_is_serialisable_for_a_workflow_result(self, session_factory):
        stats = ingest_query(
            "Odido",
            delay_seconds=0,
            fetch=lambda q, *, page, limit, sort: make_page(1, 1, 1, ["a"]),
            session_factory=session_factory,
        )

        assert stats.as_dict()["query"] == "Odido"
        assert stats.as_dict()["created"] == 1
