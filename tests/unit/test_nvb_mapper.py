"""Unit tests for the NVB -> JobCreate mapper.

Fixtures mirror the live payload shape (HAL, camelCase, nullable sub-objects).
No network access.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.integrations.nvb.mapper import to_job_create, to_job_creates
from app.integrations.nvb.schema import Job as NvbJob


@pytest.fixture
def raw_job() -> dict:
    return {
        "id": "2378e7aa-d4ea-4252-b0bf-28f33ecd1ff8",
        "title": "Software Developer (Python)",
        "description": "Bouw mooie dingen.",
        "status": "active",
        "origin": "jobdigger",
        "company": {
            "name": "Acme B.V.",
            "slug": "acme",
            "type": "direct_employer",
            "website": None,
        },
        "contact": {
            "firstName": "Afdeling",
            "lastName": "HR",
            "emailAddress": "info@acme.test",
            "phoneNumber": "",
            "street": "Neptunusstraat 17",
            "zipcode": "2132 JA",
            "city": "Hoofddorp",
            "website": "",
        },
        "apply": {"option": "external", "url": "https://acme.test/apply"},
        "workLocation": {
            "street": "Maanderpoort",
            "zipCode": "6711AB",
            "city": "Ede",
            "citySlug": "ede",
            "municipality": "EDE",
            "municipalitySlug": "ede",
            "province": "Gelderland",
            "provinceSlug": "gelderland",
            "displayName": "Ede",
            "country": {"iso": "NL", "name": "Nederland"},
            "geolocation": {"latitude": "52.040861", "longitude": "5.675403"},
            "source": "default",
        },
        "workingHours": {"min": 32, "max": 40},
        "salary": {"min": 2900, "max": 3250, "type": "monthly"},
        "metadata": {"isco": "2514", "jdco": "Python Developer-2514"},
        "startDate": "2026-09-09T22:00:00Z",
        "endDate": "2026-11-08T23:00:00Z",
        "expirationDate": None,
        "careerLevel": "Ervaren",
        "contractType": "Vast",
        "contractTypes": ["Vast"],
        "educationLevel": "HBO",
        "educationLevels": ["HBO", "WO"],
        "categories": ["Automatisering/Internet"],
        "industries": ["ICT"],
        "jobTitles": ["python developer"],
        "functionTitle": "Software Developer (Python)",
        "functionTitleSlug": "software-developer-python",
        "dcoTitle": "Python Developer",
        "dcoTitleSlug": "python-developer",
        "dcoTitles": [
            {
                "title": "Python Developer",
                "slug": "python-developer",
                "genericTitle": "developer",
                "similarity": 0.8118608,
                "source": "external_classification_vacancy_enricher",
            },
            {
                "title": "Softwareontwikkelaar",
                "slug": "softwareontwikkelaar",
                "genericTitle": "developer",
                "similarity": 0.7949504,
                "source": "external_classification_vacancy_enricher",
            },
        ],
        "recruiterId": "rec-1",
        "referenceId": "ref-1",
        "numberOfApplies": 3,
        "isPbp": False,
        "_links": {
            "self": {"href": "/api/jobs/v3/.../2378e7aa"},
            "detail": {"href": "https://www.nationalevacaturebank.nl/vacature/2378e7aa/x"},
        },
    }


@pytest.fixture
def mapped(raw_job: dict):
    return to_job_create(NvbJob.model_validate(raw_job))


class TestCoreFields:
    def test_external_id_and_source(self, mapped):
        assert mapped.external_id == "2378e7aa-d4ea-4252-b0bf-28f33ecd1ff8"
        assert mapped.source == "nvb"

    def test_keeps_the_employers_own_wording(self, mapped):
        assert mapped.title == "Software Developer (Python)"

    def test_keeps_the_classifier_title_separately(self, mapped):
        assert mapped.dco_title == "Python Developer"
        assert mapped.dco_title_slug == "python-developer"

    def test_maps_company(self, mapped):
        assert mapped.company_name == "Acme B.V."
        assert mapped.company_slug == "acme"
        assert mapped.company_type == "direct_employer"

    def test_maps_salary_and_hours(self, mapped):
        assert (mapped.salary_min, mapped.salary_max, mapped.salary_type) == (2900, 3250, "monthly")
        assert (mapped.working_hours_min, mapped.working_hours_max) == (32, 40)

    def test_maps_isco_and_jdco(self, mapped):
        assert mapped.isco == "2514"
        assert mapped.jdco == "Python Developer-2514"

    def test_datetimes_become_dates(self, mapped):
        assert mapped.start_date == date(2026, 9, 9)
        assert mapped.end_date == date(2026, 11, 8)
        assert mapped.expiration_date is None

    def test_uses_the_public_detail_url(self, mapped):
        assert mapped.detail_url.startswith("https://www.nationalevacaturebank.nl/")

    def test_maps_contact(self, mapped):
        assert mapped.contact_email == "info@acme.test"
        assert mapped.contact_first_name == "Afdeling"

    def test_keeps_raw_payload_by_default(self, mapped):
        assert mapped.raw is not None

    def test_raw_can_be_omitted(self, raw_job):
        assert to_job_create(NvbJob.model_validate(raw_job), include_raw=False).raw is None


class TestLocation:
    def test_maps_place_fields(self, mapped):
        assert mapped.location.city == "Ede"
        assert mapped.location.province == "Gelderland"
        assert mapped.location.country_iso == "NL"

    def test_coordinates_become_decimals(self, mapped):
        assert mapped.location.latitude == Decimal("52.040861")

    def test_street_and_zip_stay_on_the_job(self, mapped):
        """Address detail is per-vacancy, not part of the shared place."""
        assert mapped.street == "Maanderpoort"
        assert mapped.zip_code == "6711AB"

    def test_missing_location_is_tolerated(self, raw_job: dict):
        raw_job["workLocation"] = None

        assert to_job_create(NvbJob.model_validate(raw_job)).location is None

    def test_location_without_city_is_dropped(self, raw_job: dict):
        raw_job["workLocation"]["city"] = None

        assert to_job_create(NvbJob.model_validate(raw_job)).location is None

    def test_unparseable_coordinates_are_ignored(self, raw_job: dict):
        raw_job["workLocation"]["geolocation"] = {"latitude": "n/a", "longitude": ""}

        assert to_job_create(NvbJob.model_validate(raw_job)).location.latitude is None


class TestVocabularies:
    def test_education_levels_keep_the_singular_as_primary(self, mapped):
        assert mapped.education_levels.values == ["HBO", "WO"]
        assert mapped.education_levels.primary == "HBO"

    def test_singular_value_missing_from_the_list_is_folded_in(self, raw_job: dict):
        raw_job["educationLevel"] = "MBO"
        raw_job["educationLevels"] = ["HBO"]

        mapped = to_job_create(NvbJob.model_validate(raw_job))

        assert set(mapped.education_levels.values) == {"MBO", "HBO"}
        assert mapped.education_levels.primary == "MBO"

    def test_primary_defaults_to_the_first_value_when_absent(self, mapped):
        assert mapped.categories.primary == "Automatisering/Internet"

    def test_empty_vocabulary_is_allowed(self, raw_job: dict):
        raw_job["industries"] = []

        mapped = to_job_create(NvbJob.model_validate(raw_job))

        assert mapped.industries.values == []
        assert mapped.industries.primary is None

    def test_free_text_titles_pass_through_as_a_list(self, mapped):
        assert mapped.job_titles == ["python developer"]


class TestDcoTitles:
    def test_entries_are_ranked_in_order(self, mapped):
        assert [(d.rank, d.title) for d in mapped.dco_titles] == [
            (0, "Python Developer"),
            (1, "Softwareontwikkelaar"),
        ]

    def test_carries_score_and_generic_bucket(self, mapped):
        assert mapped.dco_titles[0].similarity == pytest.approx(0.8118608)
        assert mapped.dco_titles[0].generic_title == "developer"

    def test_untitled_entries_are_skipped(self, raw_job: dict):
        raw_job["dcoTitles"].append({"title": None, "similarity": 0.1})

        assert len(to_job_create(NvbJob.model_validate(raw_job)).dco_titles) == 2

    def test_absent_list_is_tolerated(self, raw_job: dict):
        raw_job["dcoTitles"] = []

        assert to_job_create(NvbJob.model_validate(raw_job)).dco_titles == []


class TestPageMapping:
    def test_maps_every_job_on_the_page(self, raw_job: dict):
        payload = {
            "page": 1,
            "limit": 10,
            "pages": 1,
            "total": 2,
            "_embedded": {"jobs": [raw_job, {**raw_job, "id": "other"}]},
        }

        assert len(to_job_creates(payload)) == 2

    def test_empty_page_maps_to_nothing(self):
        payload = {"page": 1, "limit": 10, "pages": 0, "total": 0, "_embedded": {"jobs": []}}

        assert to_job_creates(payload) == []


def test_untitled_vacancy_gets_a_placeholder(raw_job: dict):
    """title is NOT NULL in the database, so a missing one must not crash ingest."""
    raw_job["title"] = None

    assert to_job_create(NvbJob.model_validate(raw_job)).title == "(untitled)"
