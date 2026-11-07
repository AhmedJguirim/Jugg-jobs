"""Reading a stored raw listing (no DBOS): detect the platform, parse the page.

Creating the job happens only after grading (`test_listing_grading.py`).
"""

from __future__ import annotations

import pathlib

import pytest

from app.repositories import raw_job_listing as raw_repo
from app.schemas.raw_job_listing import RawJobListingCreate
from app.services import listing_processing as processing

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "ashbyhq"
VULTR = "https://jobs.ashbyhq.com/vultr/79d7e2d5-f159-417e-9498-2a3fc11cdbf0"
LENDABLE = "https://jobs.ashbyhq.com/lendable/ac527b20-e9fc-41d1-841c-bc881e2abef6"
DOCPLANNER = "https://jobs.ashbyhq.com/docplanner/50abe173-4ad4-4662-9d74-c7b68d9e17bd"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def read(session, url: str, html: str) -> dict:
    raw_id = raw_repo.create(session, RawJobListingCreate(url=url, html_body=html)).id
    return processing.parse(processing.load_listing(session, raw_id))


class TestParsed:
    def test_a_job_page_becomes_a_job(self, session):
        result = read(session, LENDABLE, fixture("lendable_php_engineer.html"))

        assert result["status"] == processing.STORED and result["platform"] == "ashbyhq"
        job = result["job"]
        assert (job["title"], job["source"], job["company_name"]) == ("PHP Engineer", "ashbyhq", "Lendable")

    def test_the_job_remembers_its_raw_listing(self, session):
        result = read(session, LENDABLE, fixture("lendable_php_engineer.html"))

        assert result["job"]["raw"]["raw_listing_id"] == result["raw_listing_id"]

    def test_location_is_attached_when_the_page_has_an_address(self, session):
        result = read(session, DOCPLANNER, fixture("docplanner_staff_engineer.html"))

        assert result["job"]["location"]["city"] == "Warsaw"


class TestNotAJob:
    def test_removed_posting(self, session):
        assert read(session, VULTR, fixture("not_found_vultr.html"))["status"] == processing.NOT_FOUND

    def test_unsupported_site(self, session):
        result = read(session, "https://careers.smartrecruiters.com/acme/1", "<h1>Engineer</h1>")

        assert result["status"] == processing.UNSUPPORTED

    def test_right_site_wrong_html(self, session):
        result = read(session, LENDABLE, "<html><body>Please enable JavaScript</body></html>")

        assert result["status"] == processing.UNPARSEABLE
        assert "heading" in result["reason"]

    def test_html_cleaned_up_after_grading(self):
        """Not unreadable: it must not put a graded listing back on the waiting list."""
        listing = {"id": 1, "url": LENDABLE, "html": ""}

        assert processing.parse(listing)["status"] == processing.NO_HTML

    def test_unknown_raw_listing_id_raises(self, session):
        with pytest.raises(raw_repo.RawJobListingNotFound):
            processing.load_listing(session, 999_999)
