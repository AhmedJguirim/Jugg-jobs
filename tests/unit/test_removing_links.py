"""Removing a link for good (ADR-0017): from the collected URLs, and from the
waiting list when its page could not be read."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import workspace
from app.repositories import manual_job_listing_url as url_repo
from app.repositories import raw_job_listing as raw_repo
from app.schemas.raw_job_listing import RawJobListingCreate

PHILO = "https://jobs.ashbyhq.com/philo?utm_source=Rho+Capital+Partners+job+board&utm_medium=getro.com"
JOB = "https://jobs.ashbyhq.com/acme/ac527b20-e9fc-41d1-841c-bc881e2abef6"


def raw(session, url, status=None) -> int:
    return raw_repo.create(session, RawJobListingCreate(url=url, html_body="<p>x</p>"), status=status).id


class TestRemovingACollectedUrl:
    def test_a_removed_link_leaves_every_list_and_queue(self, session):
        url_repo.add_many(session, [PHILO, JOB])
        before_count = url_repo.count(session)
        before_waiting = url_repo.count_unprocessed(session)

        assert url_repo.remove(session, PHILO) is True

        shown = {row.url for row in url_repo.list_recent(session)}
        assert PHILO not in shown and JOB in shown
        assert url_repo.count(session) == before_count - 1
        assert url_repo.count_unprocessed(session) == before_waiting - 1
        assert PHILO not in {r.url for r in url_repo.list_recent(session, only_unprocessed=True)}

    def test_it_is_never_offered_as_the_next_link(self, session):
        url_repo.add_many(session, [PHILO, JOB])
        first = url_repo.next_unprocessed(session)
        assert first.url == PHILO

        url_repo.remove(session, PHILO)

        assert url_repo.next_unprocessed(session).url == JOB

    def test_pasting_the_same_google_results_again_does_not_bring_it_back(self, session):
        url_repo.add_many(session, [PHILO])
        url_repo.remove(session, PHILO)

        added = url_repo.add_many(session, [PHILO, JOB])

        assert added == [JOB]
        assert PHILO not in {r.url for r in url_repo.list_recent(session)}

    def test_removing_twice_or_an_unknown_link_reports_nothing_removed(self, session):
        url_repo.add_many(session, [PHILO])

        assert url_repo.remove(session, PHILO) is True
        assert url_repo.remove(session, PHILO) is False
        assert url_repo.remove(session, "https://example.test/never-collected") is False


class TestRemovingAWaitingListing:
    def test_a_removed_listing_never_returns_to_the_waiting_list(self, session):
        listing_id = raw(session, PHILO, status=raw_repo.UNPARSEABLE)
        assert listing_id in {r.id for r in raw_repo.list_recent(session, limit=1000, waiting=True)}

        raw_repo.remove(session, listing_id)

        assert listing_id not in {r.id for r in raw_repo.list_recent(session, limit=1000, waiting=True)}
        assert raw_repo.get(session, listing_id).status == raw_repo.REMOVED  # kept, not deleted

    def test_unknown_listing(self, session):
        with pytest.raises(raw_repo.RawJobListingNotFound):
            raw_repo.remove(session, 999_999)


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
    def test_removing_a_failed_listing_also_removes_its_link(self, client, session):
        url_repo.add_many(session, [PHILO])
        listing_id = raw(session, PHILO, status=raw_repo.UNPARSEABLE)

        response = client.post(f"/api/workspace/raw-job-listings/{listing_id}/remove")

        assert response.status_code == 200 and response.json() == {"id": listing_id, "removed": True}
        waiting = client.get("/api/workspace/raw-job-listings?waiting=true&limit=200").json()["items"]
        assert listing_id not in {i["id"] for i in waiting}
        links = client.get("/api/workspace/manual-job-listing-urls").json()["items"]
        assert PHILO not in {i["url"] for i in links}

    def test_a_listing_with_no_collected_link_can_still_be_removed(self, client, session):
        listing_id = raw(session, "https://jobs.ashbyhq.com/other/12345678-1234-1234-1234-123456789abc",
                         status=raw_repo.UNPARSEABLE)

        assert client.post(f"/api/workspace/raw-job-listings/{listing_id}/remove").status_code == 200

    def test_unknown_listing_is_a_404(self, client):
        assert client.post("/api/workspace/raw-job-listings/999999/remove").status_code == 404

    def test_removing_the_link_shown_on_the_form(self, client, session):
        url_repo.add_many(session, [PHILO, JOB])

        response = client.post("/api/workspace/manual-job-listing-urls/remove", json={"url": PHILO})

        assert response.json() == {"removed": True}
        assert client.get("/api/workspace/manual-job-listing-urls/next").json()["url"] == JOB

    def test_removing_an_unknown_link_says_so(self, client):
        response = client.post("/api/workspace/manual-job-listing-urls/remove", json={"url": "https://example.test/x"})

        assert response.status_code == 200 and response.json() == {"removed": False}

    def test_a_blank_url_is_rejected(self, client):
        assert client.post("/api/workspace/manual-job-listing-urls/remove", json={"url": ""}).status_code == 422
