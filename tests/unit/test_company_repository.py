"""Unit tests for the company repository (CRUD)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.db.models.company import Company
from app.repositories import company as repo
from app.repositories.company import CompanyNotFound, DuplicateCompany
from app.schemas.company import CompanyCreate, CompanyUpdate


def make(session: Session, payload: dict, **overrides) -> "repo.Company":
    data = {**payload, **overrides}
    return repo.create(session, CompanyCreate(**data))


# ---- create ----


class TestCreate:
    def test_creates_company_with_all_fields(self, session: Session, company_payload: dict):
        company = make(session, company_payload)

        assert company.id is not None
        assert company.name == "Test Company B.V."
        assert company.province == "Noord-Holland"
        assert company.slug == "test-company"

    def test_sets_timestamps_automatically(self, session: Session, company_payload: dict):
        company = make(session, company_payload)
        session.refresh(company)

        assert company.created_at is not None
        assert company.updated_at is not None

    def test_optional_fields_default_to_none(self, session: Session):
        company = repo.create(session, CompanyCreate(name="Minimal B.V."))

        assert company.type is None
        assert company.website is None
        assert company.slug is None
        assert company.province is None
        assert company.google_jobs_link is None

    def test_duplicate_name_raises(self, session: Session, company_payload: dict):
        make(session, company_payload)

        with pytest.raises(DuplicateCompany) as excinfo:
            make(session, company_payload, slug="another-slug")

        assert excinfo.value.field == "name"

    def test_duplicate_slug_raises(self, session: Session, company_payload: dict):
        make(session, company_payload)

        with pytest.raises(DuplicateCompany):
            make(session, company_payload, name="Different Name B.V.")

    def test_blank_strings_normalise_to_none(self, session: Session):
        company = repo.create(
            session,
            CompanyCreate(name="Blanks B.V.", province="   ", website=""),
        )

        assert company.province is None
        assert company.website is None

    def test_whitespace_is_trimmed(self, session: Session):
        """The seed data has trailing spaces, e.g. "Amsterdam "."""
        company = repo.create(session, CompanyCreate(name="Trim B.V.", province="Amsterdam "))

        assert company.province == "Amsterdam"

    def test_empty_name_is_rejected(self):
        with pytest.raises(ValueError):
            CompanyCreate(name="")


# ---- read ----


class TestRead:
    def test_get_returns_company(self, session: Session, company_payload: dict):
        created = make(session, company_payload)

        assert repo.get(session, created.id).id == created.id

    def test_get_returns_none_when_missing(self, session: Session):
        assert repo.get(session, 999_999_999) is None

    def test_get_or_404_raises_when_missing(self, session: Session):
        with pytest.raises(CompanyNotFound):
            repo.get_or_404(session, 999_999_999)

    def test_get_by_name(self, session: Session, company_payload: dict):
        make(session, company_payload)

        assert repo.get_by_name(session, "Test Company B.V.") is not None
        assert repo.get_by_name(session, "Nope B.V.") is None

    def test_get_by_slug(self, session: Session, company_payload: dict):
        make(session, company_payload)

        assert repo.get_by_slug(session, "test-company") is not None


class TestList:
    @pytest.fixture(autouse=True)
    def seed(self, session: Session):
        for index, (name, province) in enumerate(
            [
                ("Alpha B.V.", "Noord-Holland"),
                ("Beta B.V.", "Noord-Holland"),
                ("Gamma Holding", "Utrecht"),
                ("Delta Works", "Zuid-Holland"),
            ]
        ):
            repo.create(session, CompanyCreate(name=name, province=province, slug=f"s-{index}"))

    def test_lists_all(self, session: Session):
        assert len(repo.list_companies(session)) == 4

    def test_orders_by_name_by_default(self, session: Session):
        names = [c.name for c in repo.list_companies(session)]

        assert names == sorted(names)

    def test_limit_and_offset_paginate(self, session: Session):
        first = repo.list_companies(session, limit=2, offset=0)
        second = repo.list_companies(session, limit=2, offset=2)

        assert len(first) == 2 and len(second) == 2
        assert {c.id for c in first}.isdisjoint({c.id for c in second})

    def test_search_is_case_insensitive_substring(self, session: Session):
        results = repo.list_companies(session, search="b.v.")

        assert {c.name for c in results} == {"Alpha B.V.", "Beta B.V."}

    def test_filter_by_province(self, session: Session):
        results = repo.list_companies(session, province="Noord-Holland")

        assert len(results) == 2

    def test_search_and_province_combine(self, session: Session):
        results = repo.list_companies(session, search="alpha", province="Noord-Holland")

        assert len(results) == 1

    def test_count_ignores_pagination(self, session: Session):
        assert repo.count(session) == 4
        assert repo.count(session, province="Utrecht") == 1
        assert repo.count(session, search="holding") == 1

    def test_invalid_limit_raises(self, session: Session):
        with pytest.raises(ValueError):
            repo.list_companies(session, limit=0)

    def test_invalid_offset_raises(self, session: Session):
        with pytest.raises(ValueError):
            repo.list_companies(session, offset=-1)

    def test_unknown_order_column_raises(self, session: Session):
        with pytest.raises(ValueError):
            repo.list_companies(session, order_by="drop_table")


# ---- update ----


class TestUpdate:
    def test_updates_supplied_field(self, session: Session, company_payload: dict):
        company = make(session, company_payload)

        updated = repo.update(session, company.id, CompanyUpdate(province="Utrecht"))

        assert updated.province == "Utrecht"

    def test_leaves_unset_fields_untouched(self, session: Session, company_payload: dict):
        company = make(session, company_payload)

        repo.update(session, company.id, CompanyUpdate(province="Utrecht"))

        assert company.name == "Test Company B.V."
        assert company.website == "https://example.test"

    def test_explicit_none_clears_a_field(self, session: Session, company_payload: dict):
        company = make(session, company_payload)

        updated = repo.update(session, company.id, CompanyUpdate(website=None))

        assert updated.website is None

    def test_missing_company_raises(self, session: Session):
        with pytest.raises(CompanyNotFound):
            repo.update(session, 999_999_999, CompanyUpdate(province="Utrecht"))

    def test_rename_to_existing_name_raises(self, session: Session, company_payload: dict):
        make(session, company_payload)
        other = repo.create(session, CompanyCreate(name="Other B.V."))

        with pytest.raises(DuplicateCompany):
            repo.update(session, other.id, CompanyUpdate(name="Test Company B.V."))


# ---- delete ----


class TestDelete:
    def test_deletes_company(self, session: Session, company_payload: dict):
        company = make(session, company_payload)

        repo.delete(session, company.id)

        assert repo.get(session, company.id) is None

    def test_missing_company_raises(self, session: Session):
        with pytest.raises(CompanyNotFound):
            repo.delete(session, 999_999_999)

    def test_delete_is_not_cascading_to_others(self, session: Session, company_payload: dict):
        first = make(session, company_payload)
        repo.create(session, CompanyCreate(name="Survivor B.V."))

        repo.delete(session, first.id)

        assert repo.count(session) == 1


# ---- bulk upsert (used by the ingest script) ----


class TestBulkUpsert:
    def test_inserts_new_rows(self, session: Session):
        rows = [
            {"name": "Bulk One", "province": "Utrecht"},
            {"name": "Bulk Two", "province": "Utrecht"},
        ]

        written = repo.bulk_upsert(session, rows)

        assert written == 2
        assert repo.count(session) == 2

    def test_is_idempotent_on_repeat(self, session: Session):
        rows = [{"name": "Bulk One", "province": "Utrecht"}]

        repo.bulk_upsert(session, rows)
        repo.bulk_upsert(session, rows)

        assert repo.count(session) == 1

    def test_updates_changed_fields_on_conflict(self, session: Session):
        repo.bulk_upsert(session, [{"name": "Bulk One", "province": "Utrecht"}])

        repo.bulk_upsert(session, [{"name": "Bulk One", "province": "Limburg"}])

        assert repo.get_by_name(session, "Bulk One").province == "Limburg"

    def test_preserves_explicit_ids(self, session: Session):
        repo.bulk_upsert(session, [{"id": 4242, "name": "Fixed Id B.V."}])

        assert repo.get(session, 4242) is not None

    def test_empty_input_is_a_noop(self, session: Session):
        assert repo.bulk_upsert(session, []) == 0


# ---- browse: due for another scrape ----


class TestBrowseScrapedBefore:
    """The filter behind "scrape the first N last scraped before <date>"."""

    CUTOFF = datetime(2026, 9, 28, tzinfo=timezone.utc)

    @pytest.fixture(autouse=True)
    def companies(self, session: Session):
        session.add_all(
            [
                Company(name="Old", last_scraped_at=self.CUTOFF - timedelta(days=3)),
                Company(name="Fresh", last_scraped_at=self.CUTOFF + timedelta(hours=1)),
                Company(name="Never"),
            ]
        )
        session.flush()

    def names(self, session: Session, **filters) -> list[str]:
        rows = repo.browse(session, limit=10, search=None, **filters)
        return [company.name for company, *_ in rows if company.name in {"Old", "Fresh", "Never"}]

    def test_keeps_employers_scraped_before_the_cutoff(self, session: Session):
        assert "Old" in self.names(session, scraped_before=self.CUTOFF)

    def test_drops_employers_scraped_since(self, session: Session):
        assert "Fresh" not in self.names(session, scraped_before=self.CUTOFF)

    def test_never_scraped_counts_by_default(self, session: Session):
        assert "Never" in self.names(session, scraped_before=self.CUTOFF)

    def test_never_scraped_can_be_left_out(self, session: Session):
        names = self.names(session, scraped_before=self.CUTOFF, include_never_scraped=False)

        assert names == ["Old"]

    def test_no_cutoff_means_no_filter(self, session: Session):
        assert sorted(self.names(session)) == ["Fresh", "Never", "Old"]

    def test_count_agrees_with_the_listing(self, session: Session):
        count = repo.browse_count(session, scraped_before=self.CUTOFF, include_never_scraped=False, search="Old")

        assert count == 1
