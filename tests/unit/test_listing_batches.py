"""Grading on demand: the waiting list, what marks a listing done, and batches (ADR-0016)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import workspace
from app.repositories import raw_job_listing as raw_repo
from app.schemas.raw_job_listing import RawJobListingCreate
from app.services import listing_progress
from app.workflows import listing_grading as flow

ASHBY = "https://jobs.ashbyhq.com/acme/{n}"


def add(session, n: int, status=None) -> int:
    url = f"https://jobs.ashbyhq.com/acme/{n:08d}-0000-0000-0000-000000000000"
    return raw_repo.create(session, RawJobListingCreate(url=url, html_body="<p>x</p>"), status=status).id


class TestWaitingList:
    def test_only_listings_still_to_grade_oldest_first(self, session):
        waiting_a = add(session, 1)
        graded = add(session, 2, status=raw_repo.GRADED)
        gone = add(session, 3, status=raw_repo.NOT_FOUND)
        unsupported = add(session, 4, status=raw_repo.UNSUPPORTED)
        unreadable = add(session, 5, status=raw_repo.UNPARSEABLE)
        waiting_b = add(session, 6)

        ids = [r.id for r in raw_repo.list_recent(session, limit=1000, waiting=True)]

        mine = [i for i in ids if i in {waiting_a, graded, gone, unsupported, unreadable, waiting_b}]
        assert mine == [waiting_a, unreadable, waiting_b]

    def test_count_matches_the_list(self, session):
        before = raw_repo.count(session, waiting=True)
        add(session, 1)
        add(session, 2, status=raw_repo.GRADED)

        assert raw_repo.count(session, waiting=True) == before + 1

    def test_mark_records_the_outcome(self, session):
        listing_id = add(session, 1)

        raw_repo.mark(session, listing_id, raw_repo.NOT_FOUND)
        session.expire_all()

        row = raw_repo.get(session, listing_id)
        assert row.status == raw_repo.NOT_FOUND and row.processed_at is not None


class TestMarkStep:
    @pytest.fixture
    def run(self, session, monkeypatch):
        @contextmanager
        def scope():
            yield session

        monkeypatch.setattr(flow, "session_scope", scope)
        return lambda listing_id, result: flow.mark_step.__wrapped__(listing_id, result)

    @pytest.mark.parametrize("status, expected", [
        ("not_found", "not_found"), ("unparseable", "unparseable"), ("unsupported", "unsupported"),
    ])
    def test_dead_ends_are_recorded(self, session, run, status, expected):
        listing_id = add(session, 1)

        run(listing_id, {"status": status})
        session.expire_all()

        assert raw_repo.get(session, listing_id).status == expected

    @pytest.mark.parametrize("status", ["llm_unavailable", "ungraded", "no_description", "failed"])
    def test_a_run_that_could_not_finish_leaves_the_listing_waiting(self, session, run, status):
        listing_id = add(session, 1)

        run(listing_id, {"status": status})
        session.expire_all()

        assert raw_repo.get(session, listing_id).status is None

    def test_a_graded_listing_remembers_its_job(self, session, run):
        from app.repositories import job as job_repo
        from app.schemas.job import JobCreate

        job, _ = job_repo.upsert(session, JobCreate(source="ashbyhq", external_id="mark-1", title="Dev"))
        listing_id = add(session, 1)

        run(listing_id, {"status": "stored", "job_id": job.id})
        session.expire_all()

        row = raw_repo.get(session, listing_id)
        assert row.status == "graded" and row.job_id == job.id


@pytest.fixture
def client(session, monkeypatch):
    @contextmanager
    def scope():
        yield session

    started = []

    class FakeDbos:
        @staticmethod
        def start_workflow(fn, *args):
            started.append((fn.__name__, args))
            return SimpleNamespace(workflow_id="wf-batch")

    monkeypatch.setattr(workspace, "session_scope", scope)
    monkeypatch.setattr(workspace, "DBOS", FakeDbos)
    app = FastAPI()
    app.include_router(workspace.router)
    test_client = TestClient(app)
    test_client.started = started
    return test_client


class TestSavingOnlyStores:
    def save(self, client, url):
        return client.post("/api/workspace/raw-job-listings", json={"url": url, "html_body": "<p>x</p>"})

    def test_an_ashby_listing_waits_instead_of_grading(self, client):
        response = self.save(client, "https://jobs.ashbyhq.com/acme/ac527b20-e9fc-41d1-841c-bc881e2abef6")

        assert response.status_code == 201
        assert response.json()["status"] is None and "workflow_id" not in response.json()
        assert client.started == []

    def test_a_lever_listing_waits_to_be_graded(self, client):
        response = self.save(client, "https://jobs.lever.co/jobgether/29ff8655-9468-4c31-8804-93b79abeb2a8")

        assert response.json()["status"] is None and client.started == []

    def test_a_greenhouse_listing_waits_to_be_graded(self, client):
        response = self.save(client, "https://job-boards.greenhouse.io/2k/jobs/8035080003")

        assert response.json()["status"] is None and client.started == []

    @pytest.mark.parametrize("url", [
        "https://example.test/job/1",
        "https://www.nationalevacaturebank.nl/vacature/123/dev",
    ])
    def test_a_site_without_a_parser_is_marked_and_never_listed(self, client, url):
        body = self.save(client, url).json()

        assert body["status"] == "unsupported"
        waiting = client.get("/api/workspace/raw-job-listings?waiting=true&limit=200").json()["items"]
        assert body["id"] not in {i["id"] for i in waiting}


class TestWaitingEndpoint:
    def test_lists_waiting_listings_with_a_total(self, client, session):
        waiting = add(session, 1)
        done = add(session, 2, status=raw_repo.GRADED)

        body = client.get("/api/workspace/raw-job-listings?waiting=true&limit=200").json()

        ids = {i["id"] for i in body["items"]}
        assert waiting in ids and done not in ids and body["total"] >= 1


class TestStartingABatch:
    def test_picks_the_first_waiting_listings_in_order(self, client, session):
        first, second = add(session, 1), add(session, 2)
        add(session, 3)
        add(session, 4, status=raw_repo.GRADED)

        body = client.post("/api/workspace/grading-batch", json={"count": 2}).json()

        picked = [l["id"] for l in body["listings"]]
        assert picked == [first, second]  # oldest first; the third and the graded one are left out
        name, args = client.started[0]
        assert name == "grade_listings_workflow" and [l["id"] for l in args[0]] == picked
        assert body["workflow_id"] == "wf-batch"

    def test_a_hundred_is_the_most(self, client):
        assert client.post("/api/workspace/grading-batch", json={"count": 100}).status_code in (200, 400)
        assert client.post("/api/workspace/grading-batch", json={"count": 101}).status_code == 422
        assert client.post("/api/workspace/grading-batch", json={"count": 0}).status_code == 422

    def test_nothing_waiting_is_a_400(self, client, session):
        from sqlalchemy import update

        from app.db.models.raw_job_listing import RawJobListing

        session.execute(update(RawJobListing).values(status="graded"))

        assert client.post("/api/workspace/grading-batch", json={"count": 5}).status_code == 400


class TestProgressLines:
    @pytest.mark.parametrize("grade_state, stage, expected", [
        (None, None, "reading the page"),
        ("waiting", None, "waiting for the GPU"),
        ("running", "GPU at 85°C - cooling down for 3 min", "GPU at 85°C - cooling down for 3 min"),
        ("running", None, "grading"),
        ("done", None, "saving the job"),
    ])
    def test_running(self, grade_state, stage, expected):
        assert listing_progress.running_label(grade_state, stage) == expected

    def test_a_blocked_job_says_why(self):
        label, problem = listing_progress.finished_label({
            "status": "stored", "fit_score": 85, "verdict": "strong_match", "eligibility": "blocked",
            "blocked": True, "red_flags": ["remote only within UK, EU"],
        })

        assert label == "graded: fit 85 (strong match), blocked - remote only within UK, EU" and problem is False

    @pytest.mark.parametrize("status, problem", [
        ("not_found", False), ("up_to_date", False),
        ("llm_unavailable", True), ("ungraded", True), ("unparseable", True), ("unsupported", True),
    ])
    def test_outcomes(self, status, problem):
        assert listing_progress.finished_label({"status": status, "reason": "x"})[1] is problem

    def test_batch_counts(self):
        rows = [{"state": "done"}, {"state": "running"}, {"state": "waiting"}, {"state": "failed"}]

        assert listing_progress.batch_summary(rows) == {
            "total": 4, "waiting": 1, "running": 1, "done": 1, "cancelled": 0, "failed": 1,
        }

    def test_child_ids_are_derivable_from_the_parent(self):
        assert flow.batch_child_id("b1", 7) == "b1-listing-7"
        assert flow.grade_child_id("b1-listing-7") == "b1-listing-7-grade"
