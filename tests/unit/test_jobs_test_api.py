"""The read-only API behind /jobs-test."""

from __future__ import annotations

import pathlib
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import jobs_test
from app.repositories import raw_job_listing as raw_repo
from app.schemas.raw_job_listing import RawJobListingCreate
from app.services import listing_processing as processing

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "ashbyhq"
LENDABLE = "https://jobs.ashbyhq.com/lendable/ac527b20-e9fc-41d1-841c-bc881e2abef6"


@pytest.fixture
def client(session, monkeypatch):
    @contextmanager
    def scope():
        yield session

    monkeypatch.setattr(jobs_test, "session_scope", scope)
    app = FastAPI()
    app.include_router(jobs_test.router)
    return TestClient(app)


@pytest.fixture
def stored(session):
    html = (FIXTURES / "lendable_php_engineer.html").read_text(encoding="utf-8")
    raw = raw_repo.create(session, RawJobListingCreate(url=LENDABLE, html_body=html))
    return processing.process(session, raw.id)["job_test_id"]


def test_list_has_summaries_without_descriptions(client, stored):
    items = client.get("/api/jobs-test").json()["items"]

    row = next(i for i in items if i["id"] == stored)
    assert row["title"] == "PHP Engineer"
    assert row["company"] == "Lendable"
    assert row["employment_type"] == "Full time" and row["location_type"] == "Remote"
    assert row["url"] == LENDABLE
    assert "description" not in row


def test_detail_has_the_description_and_the_left_pane_facts(client, stored):
    body = client.get(f"/api/jobs-test/{stored}").json()

    assert body["description"].startswith("<h2>About Lendable</h2>")
    assert body["sections"]["Department"] == "Engineering & Product"
    assert body["raw_listing_id"] is not None


def test_unknown_id_is_404(client):
    assert client.get("/api/jobs-test/999999").status_code == 404
