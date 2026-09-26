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
