"""Processing a stored raw listing end to end (no DBOS): detect, parse, store in jobs_test."""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import func, select

from app.db.models.job import Job
from app.db.models.job_test import JobTest
from app.repositories import job_test as job_test_repo
from app.repositories import raw_job_listing as raw_repo
from app.schemas.raw_job_listing import RawJobListingCreate
from app.services import listing_processing as processing

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "ashbyhq"
VULTR = "https://jobs.ashbyhq.com/vultr/79d7e2d5-f159-417e-9498-2a3fc11cdbf0"
LENDABLE = "https://jobs.ashbyhq.com/lendable/ac527b20-e9fc-41d1-841c-bc881e2abef6"
DOCPLANNER = "https://jobs.ashbyhq.com/docplanner/50abe173-4ad4-4662-9d74-c7b68d9e17bd"


def add_raw(session, url: str, html: str) -> int:
    return raw_repo.create(session, RawJobListingCreate(url=url, html_body=html)).id


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


class TestStored:
    def test_a_job_page_becomes_a_jobs_test_row(self, session):
        raw_id = add_raw(session, LENDABLE, fixture("lendable_php_engineer.html"))

        result = processing.process(session, raw_id)

        assert result["status"] == processing.STORED and result["created"] is True
        row = session.get(JobTest, result["job_test_id"])
        assert row.title == "PHP Engineer"
        assert row.source == "ashbyhq"
        assert row.company.name == "Lendable"
        assert row.raw["raw_listing_id"] == raw_id

    def test_location_is_attached_when_the_page_has_an_address(self, session):
        raw_id = add_raw(session, DOCPLANNER, fixture("docplanner_staff_engineer.html"))

        result = processing.process(session, raw_id)

        assert session.get(JobTest, result["job_test_id"]).location.city == "Warsaw"

    def test_the_jobs_table_is_never_touched(self, session):
        before = count(session, Job)

        processing.process(session, add_raw(session, LENDABLE, fixture("lendable_php_engineer.html")))

        assert count(session, Job) == before

    def test_processing_twice_updates_one_row(self, session):
        raw_id = add_raw(session, LENDABLE, fixture("lendable_php_engineer.html"))

        first = processing.process(session, raw_id)
        second = processing.process(session, raw_id)

        assert first["created"] is True and second["created"] is False
        assert first["job_test_id"] == second["job_test_id"]

    def test_the_same_job_from_two_pastes_is_one_row(self, session):
        a = add_raw(session, LENDABLE, fixture("lendable_php_engineer.html"))
        b = add_raw(session, LENDABLE + "/application", fixture("lendable_php_engineer.html"))

        assert processing.process(session, a)["job_test_id"] == processing.process(session, b)["job_test_id"]

    def test_user_marks_survive_a_reparse(self, session):
        raw_id = add_raw(session, LENDABLE, fixture("lendable_php_engineer.html"))
        row = session.get(JobTest, processing.process(session, raw_id)["job_test_id"])
        row.archived_at = func.now()
        session.flush()

        processing.process(session, raw_id)

        session.refresh(row)
        assert row.archived_at is not None


class TestNothingToStore:
    def test_removed_posting_is_reported_and_stores_nothing(self, session):
        raw_id = add_raw(session, VULTR, fixture("not_found_vultr.html"))
        before = count(session, JobTest)

        result = processing.process(session, raw_id)

        assert result["status"] == processing.NOT_FOUND
        assert count(session, JobTest) == before

    def test_unsupported_site(self, session):
        raw_id = add_raw(session, "https://apply.workable.com/acme/j/1", "<h1>Engineer</h1>")

        assert processing.process(session, raw_id)["status"] == processing.UNSUPPORTED

    def test_right_site_wrong_html(self, session):
        raw_id = add_raw(session, LENDABLE, "<html><body>Please enable JavaScript</body></html>")

        result = processing.process(session, raw_id)

        assert result["status"] == processing.UNPARSEABLE
        assert "heading" in result["reason"]

    def test_unknown_raw_listing_id_raises(self, session):
        with pytest.raises(raw_repo.RawJobListingNotFound):
            processing.process(session, 999_999)


def test_upsert_returns_created_flag(session):
    from app.parsers import ParsedListing, parse_listing

    job = parse_listing("ashbyhq", LENDABLE, fixture("lendable_php_engineer.html"))
    assert isinstance(job, ParsedListing)

    assert job_test_repo.upsert(session, job.job)[1] is True
    assert job_test_repo.upsert(session, job.job)[1] is False
