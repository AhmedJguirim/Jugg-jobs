"""Raw job listings: repository behaviour and the HTTP surface."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import workspace
from app.repositories import raw_job_listing as repo
from app.repositories.raw_job_listing import RawJobListingNotFound
from app.schemas.raw_job_listing import RawJobListingCreate

BODY = "<html>\n  <body>  <h1>Laravel developer</h1>\n</body></html>\n"


class TestRepository:
    def test_body_is_stored_exactly_as_pasted(self, session):
        listing = repo.create(session, RawJobListingCreate(url="https://x.test/a", html_body=BODY))

        assert repo.get(session, listing.id).html_body == BODY

    def test_url_is_trimmed(self, session):
        listing = repo.create(session, RawJobListingCreate(url="  https://x.test/a \n", html_body=BODY))

        assert listing.url == "https://x.test/a"

    def test_get_missing_raises(self, session):
        with pytest.raises(RawJobListingNotFound):
            repo.get(session, 999_999)

    def test_list_is_newest_first(self, session):
        first = repo.create(session, RawJobListingCreate(url="https://x.test/1", html_body=BODY))
        second = repo.create(session, RawJobListingCreate(url="https://x.test/2", html_body=BODY))

        ids = [row.id for row in repo.list_recent(session)]

        assert ids.index(second.id) < ids.index(first.id)


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
    def test_submit_then_fetch(self, client):
        created = client.post(
            "/api/workspace/raw-job-listings", json={"url": "https://x.test/a", "html_body": BODY}
        )

        assert created.status_code == 201
        fetched = client.get(f"/api/workspace/raw-job-listings/{created.json()['id']}").json()
        assert fetched["html_body"] == BODY
        assert fetched["body_chars"] == len(BODY)

    def test_listing_omits_bodies(self, client):
        client.post("/api/workspace/raw-job-listings", json={"url": "https://x.test/a", "html_body": BODY})

        items = client.get("/api/workspace/raw-job-listings").json()["items"]

        assert items and all("html_body" not in item for item in items)

    @pytest.mark.parametrize(
        "payload",
        [
            {"url": "", "html_body": BODY},
            {"url": "https://x.test", "html_body": ""},
            {"url": "   ", "html_body": BODY},
            {"url": "https://x.test", "html_body": " \n "},
            {"url": "https://x.test"},
        ],
    )
    def test_blank_or_missing_fields_are_rejected(self, client, payload):
        assert client.post("/api/workspace/raw-job-listings", json=payload).status_code == 422

    def test_unknown_id_is_404(self, client):
        assert client.get("/api/workspace/raw-job-listings/999999").status_code == 404
