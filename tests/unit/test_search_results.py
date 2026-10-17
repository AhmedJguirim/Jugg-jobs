"""Extracting result links from a pasted Google `#search` block, and storing them."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import workspace
from app.repositories import manual_job_listing_url as repo
from app.repositories import raw_job_listing as raw_repo
from app.schemas.raw_job_listing import RawJobListingCreate
from app.text.search_results import extract_result_urls

A = "https://jobs.ashbyhq.com/easysoft/93884a65"
B = "https://jobs.ashbyhq.com/lendable/ac527b20"
# As written in the page source: `&amp;` is what the parser must decode.
C_SOURCE = "https://jobs.ashbyhq.com/scalar/aa017463?utm_source=General+Catalyst&amp;utm_medium=getro.com"
C = "https://jobs.ashbyhq.com/scalar/aa017463?utm_source=General+Catalyst&utm_medium=getro.com"


def result(href: str, title: str, read_more: str | None = None) -> str:
    more = f'<a class="vzmbzf" href="{read_more}"><span>Read more</span></a>' if read_more else ""
    return (
        f'<div class="MjjYud"><div class="yuRUbf"><a jsname="UWckNb" href="{href}" '
        f'ping="/url?sa=t&amp;url={href}"><h3>{title}<img src="data:image/png;base64,AAAA"></h3>'
        f'<div><cite>https://jobs.ashbyhq.com<span> › x</span></cite></div></a></div>'
        f"<div><span>snippet <em>PHP</em></span>{more}</div></div>"
    )


PAGE = (
    '<a href="https://example.test/outside"><h3>not in the results block</h3></a>'
    '<div id="search"><div id="rso">'
    + result(A, "Senior Backend Engineer", read_more=f"{A}#:~:text=PHP")
    + result(B, "PHP Engineer")
    + result(C_SOURCE, "Senior Fullstack Engineer")
    + result(A, "Same job again")
    + '<a href="https://example.test/no-title"><span>no h3</span></a>'
    + "</div></div>"
    + '<a href="https://example.test/after"><h3>after the block</h3></a>'
)


class TestExtract:
    def test_returns_result_links_in_order_without_duplicates(self):
        assert extract_result_urls(PAGE) == [A, B, C]

    def test_read_more_fragment_links_are_not_separate_results(self):
        assert not any("#" in url for url in extract_result_urls(PAGE))

    def test_links_outside_the_search_div_are_ignored(self):
        urls = extract_result_urls(PAGE)

        assert "https://example.test/outside" not in urls
        assert "https://example.test/after" not in urls
        assert "https://example.test/no-title" not in urls

    def test_without_a_search_div_the_whole_text_is_scanned(self):
        assert extract_result_urls(result(A, "t")) == [A]

    def test_no_links_gives_empty_list(self):
        assert extract_result_urls("<div id='search'><p>nothing</p></div>") == []


class TestRepository:
    def test_duplicates_are_skipped_not_inserted(self, session):
        first = repo.add_many(session, [A, B])
        second = repo.add_many(session, [B, C])

        assert first == [A, B]
        assert second == [C]

    def test_repeats_within_one_call_collapse(self, session):
        assert repo.add_many(session, [A, A]) == [A]

    def test_scraped_at_starts_empty(self, session):
        repo.add_many(session, [A])

        row = next(r for r in repo.list_recent(session) if r.url == A)
        assert row.scraped_at is None


class TestNextUnprocessed:
    def test_walks_in_collection_order(self, session):
        repo.add_many(session, [A, B, C])

        assert repo.next_unprocessed(session).url == A

    def test_a_stored_listing_takes_its_url_out_of_the_queue(self, session):
        repo.add_many(session, [A, B])
        before = repo.count_unprocessed(session)

        raw_repo.create(session, RawJobListingCreate(url=A, html_body="<html></html>"))

        assert repo.count_unprocessed(session) == before - 1
        assert repo.next_unprocessed(session).url == B

    def test_skip_passes_over_links(self, session):
        repo.add_many(session, [A, B])

        assert repo.next_unprocessed(session, skip=1).url == B
        assert repo.next_unprocessed(session, skip=2) is None


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
    def test_second_submission_adds_nothing(self, client):
        url = "/api/workspace/manual-job-listing-urls/extract"

        first = client.post(url, json={"html": PAGE}).json()
        second = client.post(url, json={"html": PAGE}).json()

        assert (first["found"], first["added"], first["skipped"]) == (3, 3, 0)
        assert (second["found"], second["added"], second["skipped"]) == (3, 0, 3)

    def test_listing_shows_stored_urls(self, client):
        client.post("/api/workspace/manual-job-listing-urls/extract", json={"html": PAGE})

        items = client.get("/api/workspace/manual-job-listing-urls").json()["items"]

        assert {A, B} <= {i["url"] for i in items}
        assert all(i["scraped_at"] is None for i in items)

    def test_next_endpoint_reports_url_and_remaining(self, client):
        client.post("/api/workspace/manual-job-listing-urls/extract", json={"html": PAGE})

        body = client.get("/api/workspace/manual-job-listing-urls/next").json()

        assert body["url"] and body["remaining"] >= 3

    def test_html_without_links_is_rejected(self, client):
        response = client.post(
            "/api/workspace/manual-job-listing-urls/extract", json={"html": "<div id='search'></div>"}
        )

        assert response.status_code == 422


class TestProcessedFlag:
    def test_a_url_with_stored_html_is_marked_processed(self, session):
        repo.add_many(session, [A, B])
        raw_repo.create(session, RawJobListingCreate(url=A, html_body="<html></html>"))

        assert repo.processed_urls(session, [A, B]) == {A}

    def test_no_urls_no_query(self, session):
        assert repo.processed_urls(session, []) == set()


class TestHidingProcessed:
    def test_processed_links_are_left_out_on_request(self, session):
        repo.add_many(session, [A, B])
        raw_repo.create(session, RawJobListingCreate(url=A, html_body="<h1>Job not found</h1>"))

        everything = {r.url for r in repo.list_recent(session)}
        waiting = {r.url for r in repo.list_recent(session, only_unprocessed=True)}

        assert {A, B} <= everything and B in waiting and A not in waiting
        assert repo.count(session, only_unprocessed=True) == repo.count_unprocessed(session)

    def test_api_flag(self, client, monkeypatch):
        monkeypatch.setattr(workspace, "_start_processing", lambda *a, **k: "wf")  # no DBOS here
        client.post("/api/workspace/manual-job-listing-urls/extract", json={"html": PAGE})
        client.post("/api/workspace/raw-job-listings", json={"url": A, "html_body": "<p>x</p>"})

        shown = {i["url"] for i in client.get("/api/workspace/manual-job-listing-urls?hide_processed=true").json()["items"]}

        assert A not in shown and B in shown
