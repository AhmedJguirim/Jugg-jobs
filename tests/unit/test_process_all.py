"""The "Process all" button: fetch every waiting link's page and put it on the waiting
list (ADR-0024). No test makes a real request or starts DBOS."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import workspace
from app.db.models.manual_job_listing_url import ManualJobListingUrl
from app.integrations.listing_pages.client import Page
from app.repositories import manual_job_listing_url as url_repo
from app.repositories import raw_job_listing as raw_repo
from app.schemas.raw_job_listing import RawJobListingCreate
from app.services import listing_fetch as fetching
from app.workflows import listing_fetch as flow
from tests.unit.test_listing_fetch import block, whole_page

LEVER = "https://jobs.lever.co/jobgether/29ff8655-9468-4c31-8804-93b79abeb2a8"
LEVER_2 = "https://jobs.lever.co/jobgether/5e8a2b39-fa91-4a63-895e-31411cc57f6f"
LINKEDIN = "https://www.linkedin.com/jobs/view/1"


@pytest.fixture
def links(session):
    session.add_all(
        [
            ManualJobListingUrl(url=LEVER),
            ManualJobListingUrl(url=LINKEDIN),
            ManualJobListingUrl(url=LEVER_2),
            ManualJobListingUrl(url="https://jobs.lever.co/x/removed", removed_at=datetime.now(timezone.utc)),
            ManualJobListingUrl(url="https://jobs.lever.co/x/by-hand"),
        ]
    )
    raw_repo.create(session, RawJobListingCreate(url="https://jobs.lever.co/x/by-hand", html_body="<div/>"))
    session.flush()
    return session


@pytest.fixture
def scope(session):
    @contextmanager
    def open_session():
        yield session

    return open_session


def served(status=200, text=None, final_url=None):
    body = whole_page(block()) if text is None else text
    return lambda url, **kw: Page(status, body, len(body), final_url or url)


class TestPickingLinks:
    def test_only_live_unprocessed_links_on_a_fetchable_host_oldest_first(self, links):
        urls = url_repo.unprocessed_urls(links, 100, fetching.fetchable_hosts())

        mine = [u for u in urls if "jobgether" in u or "linkedin" in u]
        assert mine == [LEVER, LEVER_2]  # linkedin has no fetcher; removed and pasted ones are out

    def test_without_hosts_every_waiting_link_counts(self, links):
        assert LINKEDIN in url_repo.unprocessed_urls(links, 1000)

    def test_the_count_matches_the_list(self, links):
        hosts = fetching.fetchable_hosts()

        assert url_repo.count_unprocessed(links, hosts) == len(url_repo.unprocessed_urls(links, 100000, hosts))

    def test_the_limit_is_respected(self, links):
        assert len(url_repo.unprocessed_urls(links, 1, fetching.fetchable_hosts())) == 1


class TestStoringTheOutcome:
    def outcome(self, **kw):
        return {"url": LEVER, "status": "fetched", "html_body": "<div>x</div>", **kw}

    def test_a_fetched_page_waits_to_be_graded(self, session):
        assert fetching.store_as_raw(session, self.outcome()) == fetching.ADDED

        waiting = raw_repo.list_recent(session, limit=1000, waiting=True)
        assert LEVER in {r.url for r in waiting}

    def test_a_removed_posting_is_stored_as_not_found_and_never_listed(self, session):
        action = fetching.store_as_raw(session, self.outcome(status="not_found", html_body="<p>gone</p>"))

        assert action == fetching.GONE
        row = next(r for r in raw_repo.list_recent(session, limit=1000) if r.url == LEVER)
        assert row.status == raw_repo.NOT_FOUND
        assert LEVER not in {r.url for r in raw_repo.list_recent(session, limit=1000, waiting=True)}

    @pytest.mark.parametrize("status", ["blocked", "error", "no_content", "unsupported"])
    def test_a_page_that_could_not_be_had_stores_nothing(self, session, status):
        assert fetching.store_as_raw(session, self.outcome(status=status, html_body="")) == fetching.RETRY

        assert url_repo.processed_urls(session, [LEVER]) == set()

    def test_a_link_that_already_has_a_page_is_left_alone(self, session):
        raw_repo.create(session, RawJobListingCreate(url=LEVER, html_body="<p>by hand</p>"))

        assert fetching.store_as_raw(session, self.outcome()) == fetching.EXISTS

        rows = [r for r in raw_repo.list_recent(session, limit=1000) if r.url == LEVER]
        assert len(rows) == 1

    def test_the_stored_page_is_the_block_the_parser_reads(self, session, scope):
        from app.parsers.lever import parse

        result = fetching.process_link(LEVER, fetch=served(), open_session=scope)

        assert result["action"] == fetching.ADDED
        row = next(r for r in raw_repo.list_recent(session, limit=1000) if r.url == LEVER)
        assert parse(LEVER, raw_repo.get(session, row.id).html_body).job.title


class TestProcessLink:
    def test_a_blocked_link_stays_waiting_so_the_next_run_tries_it_again(self, links, scope):
        result = fetching.process_link(LEVER, fetch=served(429, "slow down"), open_session=scope)

        assert (result["status"], result["action"]) == ("blocked", fetching.RETRY)
        assert LEVER in url_repo.unprocessed_urls(links, 100, fetching.fetchable_hosts())

    def test_a_processed_link_leaves_the_waiting_links(self, links, scope):
        fetching.process_link(LEVER, fetch=served(), open_session=scope)

        assert LEVER not in url_repo.unprocessed_urls(links, 100, fetching.fetchable_hosts())

    def test_nothing_is_graded_or_created(self, links, scope):
        from sqlalchemy import func, select

        from app.db.models.job import Job

        def jobs() -> int:
            return links.scalar(select(func.count()).select_from(Job))

        before = jobs()

        fetching.process_link(LEVER, fetch=served(), open_session=scope)

        assert jobs() == before

    def test_it_pauses_first_only_when_asked(self, scope):
        sleeps: list[float] = []

        fetching.process_link(LEVER, fetch=served(), sleep=sleeps.append, delay=lambda: 2.0, open_session=scope)
        assert sleeps == []

        fetching.process_link(LEVER_2, pause=True, fetch=served(), sleep=sleeps.append, delay=lambda: 2.0, open_session=scope)
        assert sleeps == [2.0]


class FakeDbos:
    """Just enough of DBOS to run the workflow body."""

    def __init__(self):
        self.events: list[dict] = []
        self.workflow_id = "wf-fetch"

    def set_event(self, key, value):
        self.events.append({"key": key, "value": {**value, "problems": list(value["problems"])}})


class TestTheWorkflow:
    @pytest.fixture
    def run(self, monkeypatch):
        def go(results):
            """`results`: what each link's step returns, in order."""
            fake = FakeDbos()
            calls: list[tuple[str, bool]] = []
            queue = iter(results)

            def step(url, pause):
                calls.append((url, pause))
                result = next(queue)
                if isinstance(result, Exception):
                    raise result
                return {"url": url, "note": None, "requested": True, "status": "x", **result}

            monkeypatch.setattr(flow, "DBOS", fake)
            monkeypatch.setattr(flow, "fetch_link_step", step)
            return flow.fetch_listings_workflow.__wrapped__, fake, calls

        return go

    def test_counts_and_summary(self, run):
        wf, fake, _ = run([{"action": "added"}, {"action": "added"}, {"action": "gone"}, {"action": "retry", "status": "blocked", "note": "HTTP 429"}])

        summary = wf(["a", "b", "c", "d"])

        assert summary == {"total": 4, "done": 4, "added": 2, "gone": 1, "retry": 1, "exists": 0}
        last = fake.events[-1]["value"]
        assert last["current"] is None and last["problems"] == [{"url": "d", "status": "blocked", "note": "HTTP 429"}]

    def test_progress_is_published_before_and_after_each_link(self, run):
        wf, fake, _ = run([{"action": "added"}, {"action": "added"}])

        wf(["a", "b"])

        # start, then (before, after) for each link, with the final "idle" one last
        assert [e["value"]["done"] for e in fake.events] == [0, 0, 1, 1, 2, 2]
        assert [e["value"]["current"] for e in fake.events] == [None, "a", "a", "b", "b", None]

    def test_a_pause_is_asked_for_only_after_a_link_that_made_a_request(self, run):
        wf, _, calls = run(
            [{"action": "added"}, {"action": "retry", "requested": False}, {"action": "added"}, {"action": "added"}]
        )

        wf(["a", "b", "c", "d"])

        assert calls == [("a", False), ("b", True), ("c", False), ("d", True)]

    def test_a_crashing_link_is_counted_and_the_run_goes_on(self, run):
        wf, _, calls = run([RuntimeError("boom"), {"action": "added"}])

        summary = wf(["a", "b"])

        assert (summary["added"], summary["retry"], summary["done"]) == (1, 1, 2)
        assert [c[0] for c in calls] == ["a", "b"]

    def test_only_the_last_few_problems_are_remembered(self, run):
        wf, fake, _ = run([{"action": "retry", "status": "blocked", "note": str(i)} for i in range(flow.KEEP_PROBLEMS + 5)])

        wf([f"u{i}" for i in range(flow.KEEP_PROBLEMS + 5)])

        problems = fake.events[-1]["value"]["problems"]
        assert len(problems) == flow.KEEP_PROBLEMS and problems[-1]["url"] == f"u{flow.KEEP_PROBLEMS + 4}"

    def test_the_queue_runs_one_run_at_a_time(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(flow.DBOS, "register_queue", lambda name, **kw: seen.update(name=name, **kw))
        monkeypatch.setattr(flow, "_queue_registered", False)

        flow.register_queue()

        assert seen["name"] == flow.FETCH_QUEUE
        assert seen["worker_concurrency"] == 1 and seen["global_concurrency"] == 1


@pytest.fixture
def client(session, monkeypatch):
    @contextmanager
    def scope():
        yield session

    class Dbos:
        enqueued: list = []
        active: list = []
        statuses: dict = {}
        events: dict = {}
        cancelled: list = []

        @classmethod
        def list_workflows(cls, **kw):
            return [SimpleNamespace(workflow_id=i) for i in cls.active]

        @classmethod
        def enqueue_workflow(cls, queue, fn, *args):
            cls.enqueued.append((queue, fn.__name__, args))
            return SimpleNamespace(workflow_id="wf-run")

        @classmethod
        def get_workflow_status(cls, wid):
            return SimpleNamespace(status=cls.statuses[wid]) if wid in cls.statuses else None

        @classmethod
        def get_event(cls, wid, key, timeout_seconds=0):
            return cls.events.get((wid, key))

        @classmethod
        def cancel_workflow(cls, wid):
            cls.cancelled.append(wid)

    Dbos.enqueued, Dbos.active, Dbos.statuses, Dbos.events, Dbos.cancelled = [], [], {}, {}, []
    monkeypatch.setattr(workspace, "session_scope", scope)
    monkeypatch.setattr(workspace, "DBOS", Dbos)
    app = FastAPI()
    app.include_router(workspace.router)
    test_client = TestClient(app)
    test_client.dbos = Dbos
    return test_client


class TestEndpoints:
    def test_starting_takes_the_waiting_fetchable_links_in_order(self, client, links):
        response = client.post("/api/workspace/manual-job-listing-urls/process-all")

        assert response.status_code == 202 and response.json()["workflow_id"] == "wf-run"
        queue, name, args = client.dbos.enqueued[0]
        assert (queue, name) == (flow.FETCH_QUEUE, "fetch_listings_workflow")
        picked = [u for u in args[0] if "jobgether" in u or "linkedin" in u]
        assert picked == [LEVER, LEVER_2]
        assert response.json()["total"] == len(args[0])

    def test_a_run_in_progress_is_not_started_twice(self, client, links):
        client.dbos.active = ["wf-old"]

        response = client.post("/api/workspace/manual-job-listing-urls/process-all")

        assert response.status_code == 409 and client.dbos.enqueued == []

    def test_nothing_to_fetch_is_a_400(self, client, session):
        from sqlalchemy import update

        session.execute(update(ManualJobListingUrl).values(removed_at=datetime.now(timezone.utc)))

        assert client.post("/api/workspace/manual-job-listing-urls/process-all").status_code == 400

    def test_the_active_run_can_be_found_again_after_a_reload(self, client):
        assert client.get("/api/workspace/manual-job-listing-urls/process-all").json() == {"workflow_id": None}

        client.dbos.active = ["wf-7"]

        assert client.get("/api/workspace/manual-job-listing-urls/process-all").json() == {"workflow_id": "wf-7"}

    def test_the_status_carries_the_state_and_the_counts(self, client):
        client.dbos.statuses["wf-1"] = "PENDING"
        client.dbos.events[("wf-1", "progress")] = {"total": 5, "done": 2, "added": 2}

        body = client.get("/api/workspace/manual-job-listing-urls/process-all/wf-1").json()

        assert body["state"] == "running" and body["progress"]["done"] == 2

    def test_a_finished_run_reports_done(self, client):
        client.dbos.statuses["wf-1"] = "SUCCESS"

        assert client.get("/api/workspace/manual-job-listing-urls/process-all/wf-1").json()["state"] == "done"

    def test_an_unknown_run_is_a_404(self, client):
        assert client.get("/api/workspace/manual-job-listing-urls/process-all/nope").status_code == 404
        assert client.post("/api/workspace/manual-job-listing-urls/process-all/nope/cancel").status_code == 404

    def test_a_run_can_be_cancelled(self, client):
        client.dbos.statuses["wf-1"] = "PENDING"

        response = client.post("/api/workspace/manual-job-listing-urls/process-all/wf-1/cancel")

        assert response.status_code == 200 and client.dbos.cancelled == ["wf-1"]

    def test_the_list_says_how_many_can_be_fetched(self, client, links):
        body = client.get("/api/workspace/manual-job-listing-urls?hide_processed=true").json()

        assert body["fetchable"] >= 2
        assert body["fetchable"] < body["total"]  # the linkedin link is waiting but cannot be fetched
