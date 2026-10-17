"""Cleaning up the stored HTML of graded listings (ADR-0018)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import workspace
from app.repositories import raw_job_listing as raw_repo
from app.schemas.raw_job_listing import RawJobListingCreate
from app.services import listing_processing as processing
from app.services import listing_progress
from app.workflows import listing_grading as flow

HTML = "<html><body>" + "x" * 1000 + "</body></html>"
URL = "https://jobs.ashbyhq.com/acme/ac527b20-e9fc-41d1-841c-bc881e2abef6"


def add(session, n, status=None, html=HTML) -> int:
    url = f"https://jobs.ashbyhq.com/acme/{n:08d}-0000-0000-0000-000000000000"
    return raw_repo.create(session, RawJobListingCreate(url=url, html_body=html), status=status).id


class TestRepository:
    def test_only_graded_listings_lose_their_html(self, session):
        graded = add(session, 1, status=raw_repo.GRADED)
        waiting = add(session, 2)
        unreadable = add(session, 3, status=raw_repo.UNPARSEABLE)
        gone = add(session, 4, status=raw_repo.NOT_FOUND)
        removed = add(session, 5, status=raw_repo.REMOVED)

        raw_repo.clear_graded_html(session)
        session.expire_all()

        assert raw_repo.get(session, graded).html_body == ""
        for untouched in (waiting, unreadable, gone, removed):
            assert raw_repo.get(session, untouched).html_body == HTML

    def test_the_row_survives_with_its_link_status_and_job(self, session):
        from app.repositories import job as job_repo
        from app.schemas.job import JobCreate

        job, _ = job_repo.upsert(session, JobCreate(source="ashbyhq", external_id="clean-1", title="Dev"))
        listing_id = add(session, 1, status=raw_repo.GRADED)
        raw_repo.mark(session, listing_id, raw_repo.GRADED, job_id=job.id)

        raw_repo.clear_graded_html(session)
        session.expire_all()

        row = raw_repo.get(session, listing_id)
        assert row.url.endswith("00000001-0000-0000-0000-000000000000")
        assert row.status == raw_repo.GRADED and row.job_id == job.id

    def test_it_reports_what_it_cleaned_and_a_second_run_finds_nothing(self, session):
        before_count, before_bytes = raw_repo.graded_html_stats(session)
        add(session, 1, status=raw_repo.GRADED)
        add(session, 2, status=raw_repo.GRADED)

        assert raw_repo.graded_html_stats(session) == (before_count + 2, before_bytes + 2 * len(HTML))

        cleaned, freed = raw_repo.clear_graded_html(session)
        assert cleaned == before_count + 2 and freed == before_bytes + 2 * len(HTML)
        assert raw_repo.graded_html_stats(session) == (0, 0)
        assert raw_repo.clear_graded_html(session) == (0, 0)


class TestACleanedListingIsNotDamagedLater:
    def listing(self, html):
        return {"id": 1, "url": URL, "html": html}

    @pytest.mark.parametrize("html", ["", "   \n"])
    def test_parse_reports_cleaned_html_instead_of_calling_the_page_unreadable(self, html):
        result = processing.parse(self.listing(html))

        assert result["status"] == processing.NO_HTML and "cleaned up" in result["reason"]

    def test_nothing_records_that_status_so_a_graded_listing_stays_graded(self, session, monkeypatch):
        @contextmanager
        def scope():
            yield session

        monkeypatch.setattr(flow, "session_scope", scope)
        listing_id = add(session, 1, status=raw_repo.GRADED)
        raw_repo.clear_graded_html(session)

        flow.mark_step.__wrapped__(listing_id, {"status": processing.NO_HTML})
        session.expire_all()

        assert raw_repo.get(session, listing_id).status == raw_repo.GRADED

    def test_the_progress_line_explains_it(self):
        label, problem = listing_progress.finished_label({"status": "no_html"})

        assert "cleaned up" in label and problem is True


@pytest.fixture
def client(session, monkeypatch):
    @contextmanager
    def scope():
        yield session

    monkeypatch.setattr(workspace, "session_scope", scope)
    app = FastAPI()
    app.include_router(workspace.router)
    return TestClient(app)


class TestApi:
    def test_stats_then_cleanup(self, client, session):
        base = client.get("/api/workspace/raw-job-listings-cleanup").json()
        add(session, 1, status=raw_repo.GRADED)
        add(session, 2)

        stats = client.get("/api/workspace/raw-job-listings-cleanup").json()
        assert stats == {"listings": base["listings"] + 1, "bytes": base["bytes"] + len(HTML)}

        done = client.post("/api/workspace/raw-job-listings-cleanup").json()
        assert done == {"cleaned": base["listings"] + 1, "bytes_freed": base["bytes"] + len(HTML)}
        assert client.get("/api/workspace/raw-job-listings-cleanup").json() == {"listings": 0, "bytes": 0}

    def test_the_cleanup_path_is_not_read_as_a_listing_id(self, client):
        assert client.get("/api/workspace/raw-job-listings-cleanup").status_code == 200
