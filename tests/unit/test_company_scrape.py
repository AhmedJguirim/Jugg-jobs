"""Unit tests for per-company scrape support.

The query extraction runs against 2901 seed links, so it is tested against the
shapes those links actually take - including employer names containing
characters that would break naive string slicing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.db.models.company import Company
from app.services.company_scrape import (
    NoSearchQuery,
    query_for,
    query_from_link,
    record_scrape,
)

BASE = "https://www.nationalevacaturebank.nl/vacature/zoeken"


class TestQueryFromLink:
    def test_plain_query(self):
        assert query_from_link(f"{BASE}?query=Skeye") == "Skeye"

    def test_percent_encoded_spaces(self):
        assert query_from_link(f"{BASE}?query=Proxima%20Beta%20Europe") == "Proxima Beta Europe"

    def test_plus_encoded_spaces(self):
        assert query_from_link(f"{BASE}?query=100+Grams") == "100 Grams"

    def test_ampersand_in_the_name(self):
        """A naive split on '&' would truncate this to "R".'"""
        assert query_from_link(f"{BASE}?query=R%26D%20Partners") == "R&D Partners"

    def test_at_sign_in_the_name(self):
        assert query_from_link(f"{BASE}?query=%40Fentures") == "@Fentures"

    def test_dotted_name(self):
        assert query_from_link(f"{BASE}?query=Confocal.nl") == "Confocal.nl"

    def test_extra_parameters_are_ignored(self):
        assert query_from_link(f"{BASE}?page=2&query=Skeye&sort=date") == "Skeye"

    def test_missing_query_parameter(self):
        assert query_from_link(BASE) is None

    def test_empty_query_parameter(self):
        assert query_from_link(f"{BASE}?query=") is None

    def test_whitespace_only_query(self):
        assert query_from_link(f"{BASE}?query=%20%20") is None

    def test_none_link(self):
        assert query_from_link(None) is None

    def test_empty_link(self):
        assert query_from_link("") is None


class TestQueryForCompany:
    def test_prefers_the_search_link(self):
        company = Company(
            name="Skeye B.V.",
            nationalevacaturebank_jobs_link=f"{BASE}?query=Skeye",
        )

        assert query_for(company) == "Skeye"

    def test_falls_back_to_the_name_without_a_link(self):
        """Employers discovered from a vacancy carry no seed link."""
        assert query_for(Company(name="BrightFlow")) == "BrightFlow"

    def test_falls_back_when_the_link_has_no_query(self):
        company = Company(name="BrightFlow", nationalevacaturebank_jobs_link=BASE)

        assert query_for(company) == "BrightFlow"

    def test_name_is_trimmed(self):
        assert query_for(Company(name="  Spaced Out B.V.  ")) == "Spaced Out B.V."

    def test_raises_without_a_name_or_link(self):
        with pytest.raises(NoSearchQuery):
            query_for(Company(name=""))


class TestRecordScrape:
    @pytest.fixture
    def company(self, session: Session) -> Company:
        company = Company(name="Scrape Me B.V.")
        session.add(company)
        session.flush()
        return company

    def test_stamps_the_time_and_count(self, session: Session, company: Company):
        record_scrape(session, company.id, vacancies=7)

        assert company.last_scraped_at is not None
        assert company.last_scrape_vacancies == 7

    def test_records_a_scrape_that_found_nothing(self, session: Session, company: Company):
        """"Looked and found nothing" must be distinguishable from "never looked"."""
        record_scrape(session, company.id, vacancies=0)

        assert company.last_scraped_at is not None
        assert company.last_scrape_vacancies == 0

    def test_overwrites_an_earlier_scrape(self, session: Session, company: Company):
        earlier = datetime.now(timezone.utc) - timedelta(days=3)
        record_scrape(session, company.id, vacancies=1, when=earlier)

        record_scrape(session, company.id, vacancies=5)

        assert company.last_scrape_vacancies == 5
        assert company.last_scraped_at > earlier

    def test_unknown_company_is_ignored(self, session: Session):
        record_scrape(session, 999_999_999, vacancies=3)  # must not raise

    def test_a_never_scraped_company_has_no_timestamp(self, company: Company):
        assert company.last_scraped_at is None
        assert company.last_scrape_vacancies is None


def page_payload(external_ids: list[str]) -> dict:
    """A minimal but valid Nationale Vacaturebank result page."""
    return {
        "page": 1,
        "limit": len(external_ids),
        "pages": 1,
        "total": len(external_ids),
        "_embedded": {
            "jobs": [
                {
                    "id": external_id,
                    "title": f"Job {external_id}",
                    "company": {"name": "Cap B.V."},
                }
                for external_id in external_ids
            ]
        },
    }


class TestScrapeCaps:
    """Ceilings on what one scrape may cost.

    Both exist because the board searches full text: an employer whose name
    contains a common word matches much of the site. "Service" returns 15,090
    vacancies across 604 pages, none of them necessarily that employer's.
    """

    def test_page_cap_is_twenty(self):
        from app.workflows.enrichment import MAX_PAGES_PER_SCRAPE

        assert MAX_PAGES_PER_SCRAPE == 20

    def test_new_vacancy_cap_is_fifty(self):
        from app.workflows.enrichment import MAX_NEW_VACANCIES_PER_SCRAPE

        assert MAX_NEW_VACANCIES_PER_SCRAPE == 50

    def test_the_vacancy_cap_counts_new_rows_only(self, session, monkeypatch):
        """Re-seen vacancies cost nothing to enrich, so they must not use budget."""
        from contextlib import contextmanager

        from app.repositories import job as job_repo
        from app.schemas.job import JobCreate
        from app.workflows import enrichment

        # store_page opens its own session against the application database.
        # Point it at this test's transaction so nothing escapes the rollback.
        @contextmanager
        def test_scope():
            yield session

        monkeypatch.setattr(enrichment, "session_scope", test_scope)

        for external_id in ("a", "b", "c"):
            job_repo.upsert(session, JobCreate(external_id=external_id, title="known"))

        # Budget of 2: the three known ones are free, then two new ones land
        # and the sixth is left for a later scrape.
        result = enrichment.store_page(page_payload(["a", "b", "c", "d", "e", "f"]), 2)

        assert result["created"] == 2
        assert result["updated"] == 3
        assert result["skipped"] == 1

    def test_no_budget_stores_the_whole_page(self, session, monkeypatch):
        from contextlib import contextmanager

        from app.workflows import enrichment

        @contextmanager
        def test_scope():
            yield session

        monkeypatch.setattr(enrichment, "session_scope", test_scope)

        result = enrichment.store_page(page_payload(["w", "x", "y", "z"]), None)

        assert result["created"] == 4
        assert result["skipped"] == 0


class TestEnrichmentSelection:
    """What a scrape hands to the GPU.

    The storage cap was not enough on its own: re-scraping an employer whose
    vacancies are all already stored adds nothing new, so that cap never fires,
    yet the scrape was still queueing every vacancy it saw. On a 287-result
    query with most vacancies untranslated that is about 95 minutes of GPU from
    one click.
    """

    @pytest.fixture
    def scoped(self, session, monkeypatch):
        from contextlib import contextmanager

        from app.workflows import enrichment

        @contextmanager
        def test_scope():
            yield session

        monkeypatch.setattr(enrichment, "session_scope", test_scope)
        return enrichment

    def make_jobs(self, session, count: int) -> list[int]:
        from app.repositories import job as job_repo
        from app.schemas.job import JobCreate

        ids = []
        for index in range(count):
            job = job_repo.create(
                session,
                JobCreate(
                    external_id=f"sel-{index}",
                    title=f"Job {index}",
                    description="<p>tekst</p>",
                ),
            )
            ids.append(job.id)
        return ids

    def test_unenriched_vacancies_are_selected(self, session, scoped):
        ids = self.make_jobs(session, 3)

        result = scoped.select_for_enrichment(ids, "en", "default", 50)

        assert sorted(result["ids"]) == sorted(ids)
        assert result["needed"] == 3
        assert result["deferred"] == 0

    def test_already_enriched_vacancies_are_skipped(self, session, scoped):
        from app.repositories import enrichment as enrichment_repo
        from app.llm.prompts import ASSESSMENT_PROMPT_VERSION, TRANSLATION_PROMPT_VERSION

        ids = self.make_jobs(session, 3)
        enrichment_repo.upsert_translation(
            session,
            job_id=ids[0],
            source_hash="h",
            prompt_version=TRANSLATION_PROMPT_VERSION,
        )
        enrichment_repo.upsert_assessment(
            session,
            job_id=ids[0],
            source_hash="h",
            prompt_version=ASSESSMENT_PROMPT_VERSION,
            fit_score=50,
            verdict="weak",
        )

        result = scoped.select_for_enrichment(ids, "en", "default", 50)

        assert ids[0] not in result["ids"]
        assert result["skipped_current"] == 1
        assert result["needed"] == 2

    def test_the_selection_is_capped(self, session, scoped):
        ids = self.make_jobs(session, 8)

        result = scoped.select_for_enrichment(ids, "en", "default", 3)

        assert len(result["ids"]) == 3
        assert result["needed"] == 8
        assert result["deferred"] == 5

    def test_the_cap_is_fifty(self):
        from app.workflows.enrichment import MAX_ENRICHED_PER_SCRAPE

        assert MAX_ENRICHED_PER_SCRAPE == 50

    def test_no_vacancies_means_no_work(self, scoped):
        result = scoped.select_for_enrichment([], "en", "default", 50)

        assert result == {"ids": [], "needed": 0, "skipped_current": 0, "deferred": 0}

    def test_vacancies_without_a_description_are_ignored(self, session, scoped):
        from app.repositories import job as job_repo
        from app.schemas.job import JobCreate

        bare = job_repo.create(session, JobCreate(external_id="bare-sel", title="No body"))

        result = scoped.select_for_enrichment([bare.id], "en", "default", 50)

        assert result["ids"] == []
