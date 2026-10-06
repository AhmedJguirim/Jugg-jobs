"""Unit tests for the job repository (CRUD + related tables)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.company import Company
from app.db.models.location import Location
from app.db.models.taxonomy import Category, job_categories
from app.repositories import job as repo
from app.repositories.job import JobNotFound
from app.schemas.job import (
    DcoTitleBase,
    JobCreate,
    JobUpdate,
    LocationBase,
    VocabularyTerms,
)


@pytest.fixture
def job_payload() -> JobCreate:
    return JobCreate(
        source="nvb",
        external_id="ext-1",
        title="Python Developer",
        description="Build things in Python.",
        status="active",
        origin="jobdigger",
        company_name="Acme B.V.",
        company_slug="acme",
        company_type="direct_employer",
        company_website="https://acme.test",
        location=LocationBase(
            city="Amsterdam",
            city_slug="amsterdam",
            municipality="AMSTERDAM",
            province="Noord-Holland",
            country_iso="NL",
            country_name="Nederland",
            latitude="52.370216",
            longitude="4.895168",
        ),
        function_title="Python Developer",
        dco_title="Python Developer",
        dco_title_slug="python-developer",
        isco="2514",
        jdco="Python Developer-2514",
        job_titles=["python developer", "backend developer"],
        career_level="Ervaren",
        salary_min=4000,
        salary_max=6000,
        salary_type="monthly",
        working_hours_min=32,
        working_hours_max=40,
        street="Keizersgracht",
        zip_code="1015CJ",
        apply_option="external",
        apply_url="https://acme.test/apply",
        detail_url="https://www.nationalevacaturebank.nl/vacature/ext-1",
        start_date=date(2026, 9, 1),
        expiration_date=date(2026, 11, 1),
        categories=VocabularyTerms(values=["Automatisering/Internet"], primary="Automatisering/Internet"),
        industries=VocabularyTerms(values=["ICT"], primary="ICT"),
        education_levels=VocabularyTerms(values=["HBO", "WO"], primary="HBO"),
        contract_types=VocabularyTerms(values=["Vast"], primary="Vast"),
        dco_titles=[
            DcoTitleBase(rank=0, title="Python Developer", slug="python-developer",
                         generic_title="developer", similarity=0.85),
            DcoTitleBase(rank=1, title="Backend Developer", slug="backend-developer",
                         generic_title="developer", similarity=0.82),
        ],
        raw={"id": "ext-1"},
    )


# ---- create ----


class TestCreate:
    def test_creates_job_with_core_fields(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        assert job.id is not None
        assert job.title == "Python Developer"
        assert job.external_id == "ext-1"
        assert job.salary_range == "4000-6000 (monthly)"

    def test_creates_the_company(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        assert job.company is not None
        assert job.company.name == "Acme B.V."

    def test_fills_company_columns_the_seed_file_left_empty(
        self, session: Session, job_payload: JobCreate
    ):
        """Vacancies carry slug/type/website, which companies.json lacked."""
        job = repo.create(session, job_payload)

        assert job.company.slug == "acme"
        assert job.company.type == "direct_employer"
        assert job.company.website == "https://acme.test"

    def test_reuses_an_existing_company(self, session: Session, job_payload: JobCreate):
        session.add(Company(name="Acme B.V."))
        session.flush()

        job = repo.create(session, job_payload)

        assert session.scalar(select(Company).where(Company.name == "Acme B.V.")) is job.company
        assert len(session.scalars(select(Company)).all()) == 1

    def test_does_not_overwrite_existing_company_values(
        self, session: Session, job_payload: JobCreate
    ):
        session.add(Company(name="Acme B.V.", website="https://original.test"))
        session.flush()

        job = repo.create(session, job_payload)

        assert job.company.website == "https://original.test"

    def test_creates_the_location(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        assert job.location.city == "Amsterdam"
        assert job.location.province == "Noord-Holland"
        assert job.city == "Amsterdam"

    def test_deduplicates_locations_across_jobs(self, session: Session, job_payload: JobCreate):
        first = repo.create(session, job_payload)
        second = repo.create(
            session, job_payload.model_copy(update={"external_id": "ext-2"})
        )

        assert first.location_id == second.location_id
        assert len(session.scalars(select(Location)).all()) == 1

    def test_links_vocabularies(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        assert {c.name for c in job.categories} == {"Automatisering/Internet"}
        assert {e.name for e in job.education_levels} == {"HBO", "WO"}

    def test_records_which_vocabulary_value_is_primary(
        self, session: Session, job_payload: JobCreate
    ):
        job = repo.create(session, job_payload)

        rows = session.execute(
            select(job_categories.c.is_primary).where(job_categories.c.job_id == job.id)
        ).scalars().all()
        assert rows == [True]

    def test_reuses_vocabulary_terms_across_jobs(self, session: Session, job_payload: JobCreate):
        repo.create(session, job_payload)
        repo.create(session, job_payload.model_copy(update={"external_id": "ext-2"}))

        assert len(session.scalars(select(Category)).all()) == 1

    def test_stores_ranked_dco_titles(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        assert [d.title for d in job.dco_titles] == ["Python Developer", "Backend Developer"]
        assert job.dco_titles[0].similarity == pytest.approx(0.85)

    def test_stores_free_text_job_titles_as_an_array(
        self, session: Session, job_payload: JobCreate
    ):
        job = repo.create(session, job_payload)

        assert job.job_titles == ["python developer", "backend developer"]

    def test_stores_raw_payload(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        assert job.raw == {"id": "ext-1"}

    def test_slug_clash_between_employers_does_not_fail_ingest(
        self, session: Session, job_payload: JobCreate
    ):
        """Two employers can arrive carrying the same slug; the vacancy still loads."""
        repo.create(session, job_payload)

        other = repo.create(
            session,
            job_payload.model_copy(
                update={"external_id": "ext-2", "company_name": "Other B.V."}
            ),
        )

        assert other.company.name == "Other B.V."
        assert other.company.slug is None

    def test_job_without_company_or_location_is_allowed(self, session: Session):
        job = repo.create(session, JobCreate(external_id="bare", title="Bare Job"))

        assert job.company is None
        assert job.location is None


# ---- upsert ----


class TestUpsert:
    def test_inserts_when_new(self, session: Session, job_payload: JobCreate):
        job, created = repo.upsert(session, job_payload)

        assert created is True
        assert job.id is not None

    def test_updates_when_external_id_is_known(self, session: Session, job_payload: JobCreate):
        repo.upsert(session, job_payload)

        job, created = repo.upsert(
            session, job_payload.model_copy(update={"title": "Senior Python Developer"})
        )

        assert created is False
        assert job.title == "Senior Python Developer"
        assert repo.count(session) == 1

    def test_same_external_id_from_another_source_is_a_different_job(
        self, session: Session, job_payload: JobCreate
    ):
        repo.upsert(session, job_payload)

        repo.upsert(session, job_payload.model_copy(update={"source": "indeed"}))

        assert repo.count(session) == 2

    def test_replaces_vocabularies_rather_than_appending(
        self, session: Session, job_payload: JobCreate
    ):
        repo.upsert(session, job_payload)

        job, _ = repo.upsert(
            session,
            job_payload.model_copy(
                update={"education_levels": VocabularyTerms(values=["MBO"], primary="MBO")}
            ),
        )

        assert {e.name for e in job.education_levels} == {"MBO"}

    def test_replaces_dco_titles_rather_than_appending(
        self, session: Session, job_payload: JobCreate
    ):
        repo.upsert(session, job_payload)

        job, _ = repo.upsert(
            session,
            job_payload.model_copy(
                update={"dco_titles": [DcoTitleBase(rank=0, title="Data Engineer")]}
            ),
        )

        assert [d.title for d in job.dco_titles] == ["Data Engineer"]


# ---- read ----


class TestRead:
    def test_get_returns_job(self, session: Session, job_payload: JobCreate):
        created = repo.create(session, job_payload)

        assert repo.get(session, created.id).id == created.id

    def test_get_returns_none_when_missing(self, session: Session):
        assert repo.get(session, 999_999_999) is None

    def test_get_or_404_raises(self, session: Session):
        with pytest.raises(JobNotFound):
            repo.get_or_404(session, 999_999_999)

    def test_get_by_external_id(self, session: Session, job_payload: JobCreate):
        repo.create(session, job_payload)

        assert repo.get_by_external_id(session, "nvb", "ext-1") is not None
        assert repo.get_by_external_id(session, "nvb", "nope") is None


class TestList:
    @pytest.fixture(autouse=True)
    def seed(self, session: Session, job_payload: JobCreate):
        repo.create(session, job_payload)
        repo.create(
            session,
            job_payload.model_copy(
                update={
                    "external_id": "ext-2",
                    "title": "Verpleegkundige",
                    "dco_title": "Verpleegkundige",
                    "company_name": "Zorg B.V.",
                    "company_slug": "zorg",
                    "career_level": "Starter",
                    "salary_min": 2000,
                    "salary_max": 2500,
                    "start_date": date(2026, 8, 1),
                    "location": LocationBase(city="Utrecht", province="Utrecht"),
                    "categories": VocabularyTerms(values=["Medisch/Zorg"], primary="Medisch/Zorg"),
                    "education_levels": VocabularyTerms(values=["MBO"], primary="MBO"),
                }
            ),
        )

    def test_lists_all(self, session: Session):
        assert len(repo.list_jobs(session)) == 2

    def test_orders_newest_first_by_default(self, session: Session):
        titles = [j.title for j in repo.list_jobs(session)]

        assert titles == ["Python Developer", "Verpleegkundige"]

    def test_search_matches_the_classifier_title_too(self, session: Session):
        """A search for the normalised title finds the vacancy."""
        results = repo.list_jobs(session, search="verpleegkundige")

        assert len(results) == 1

    def test_filter_by_city(self, session: Session):
        assert len(repo.list_jobs(session, city="Utrecht")) == 1

    def test_filter_by_province(self, session: Session):
        assert len(repo.list_jobs(session, province="Noord-Holland")) == 1

    def test_filter_by_category(self, session: Session):
        assert len(repo.list_jobs(session, category="Medisch/Zorg")) == 1

    def test_filter_by_education_level(self, session: Session):
        assert len(repo.list_jobs(session, education_level="WO")) == 1

    def test_filter_by_career_level(self, session: Session):
        assert len(repo.list_jobs(session, career_level="Starter")) == 1

    def test_filter_by_minimum_salary(self, session: Session):
        assert len(repo.list_jobs(session, min_salary=3000)) == 1

    def test_filter_by_posted_since(self, session: Session):
        assert len(repo.list_jobs(session, posted_since=date(2026, 8, 15))) == 1

    def test_filters_combine(self, session: Session):
        assert len(repo.list_jobs(session, city="Amsterdam", education_level="HBO")) == 1
        assert len(repo.list_jobs(session, city="Utrecht", education_level="HBO")) == 0

    def test_pagination(self, session: Session):
        first = repo.list_jobs(session, limit=1, offset=0)
        second = repo.list_jobs(session, limit=1, offset=1)

        assert first[0].id != second[0].id

    def test_count_ignores_pagination(self, session: Session):
        assert repo.count(session) == 2
        assert repo.count(session, city="Utrecht") == 1

    def test_invalid_limit_raises(self, session: Session):
        with pytest.raises(ValueError):
            repo.list_jobs(session, limit=0)

    def test_unknown_order_column_raises(self, session: Session):
        with pytest.raises(ValueError):
            repo.list_jobs(session, order_by="; drop table jobs")

    def test_list_vocabulary_returns_known_values(self, session: Session):
        assert repo.list_vocabulary(session, "education_levels") == ["HBO", "MBO", "WO"]

    def test_list_vocabulary_rejects_unknown_field(self, session: Session):
        with pytest.raises(ValueError):
            repo.list_vocabulary(session, "nonsense")


# ---- update ----


class TestUpdate:
    def test_updates_supplied_field(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        updated = repo.update(session, job.id, JobUpdate(status="expired"))

        assert updated.status == "expired"

    def test_leaves_unset_fields_untouched(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        repo.update(session, job.id, JobUpdate(status="expired"))

        assert job.title == "Python Developer"

    def test_missing_job_raises(self, session: Session):
        with pytest.raises(JobNotFound):
            repo.update(session, 999_999_999, JobUpdate(status="expired"))


class TestMarks:
    def test_new_job_is_unmarked(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        assert job.applied_at is None
        assert job.archived_at is None

    def test_marks_applied(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        repo.set_marks(session, job.id, applied=True)

        assert job.applied_at is not None
        assert job.archived_at is None

    def test_marks_archived(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        repo.set_marks(session, job.id, archived=True)

        assert job.archived_at is not None
        assert job.applied_at is None

    def test_false_clears_a_mark(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)
        repo.set_marks(session, job.id, applied=True, archived=True)

        repo.set_marks(session, job.id, archived=False)

        assert job.archived_at is None
        assert job.applied_at is not None

    def test_setting_again_keeps_the_original_timestamp(
        self, session: Session, job_payload: JobCreate
    ):
        job = repo.create(session, job_payload)
        repo.set_marks(session, job.id, applied=True)
        first = job.applied_at

        repo.set_marks(session, job.id, applied=True)

        assert job.applied_at == first

    def test_reingest_keeps_the_marks(self, session: Session, job_payload: JobCreate):
        """The board re-lists vacancies on every search; that must not undo a mark."""
        job = repo.create(session, job_payload)
        repo.set_marks(session, job.id, applied=True, archived=True)

        repo.upsert(session, job_payload.model_copy(update={"title": "Senior Python Developer"}))

        assert job.applied_at is not None
        assert job.archived_at is not None

    def test_missing_job_raises(self, session: Session):
        with pytest.raises(JobNotFound):
            repo.set_marks(session, 999_999_999, applied=True)


# ---- delete ----


class TestDelete:
    def test_deletes_job(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        repo.delete(session, job.id)

        assert repo.get(session, job.id) is None

    def test_removes_association_rows(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)
        job_id = job.id

        repo.delete(session, job_id)

        rows = session.execute(
            select(job_categories).where(job_categories.c.job_id == job_id)
        ).all()
        assert rows == []

    def test_keeps_shared_company_and_location(self, session: Session, job_payload: JobCreate):
        job = repo.create(session, job_payload)

        repo.delete(session, job.id)

        assert session.scalars(select(Company)).all() != []
        assert session.scalars(select(Location)).all() != []

    def test_missing_job_raises(self, session: Session):
        with pytest.raises(JobNotFound):
            repo.delete(session, 999_999_999)

    def test_delete_expired_removes_only_stale_rows(
        self, session: Session, job_payload: JobCreate
    ):
        repo.create(session, job_payload)  # expires 2026-11-01
        repo.create(
            session,
            job_payload.model_copy(
                update={"external_id": "ext-2", "expiration_date": date(2026, 1, 1)}
            ),
        )

        removed = repo.delete_expired(session, before=date(2026, 6, 1))

        assert removed == 1
        assert repo.count(session) == 1
