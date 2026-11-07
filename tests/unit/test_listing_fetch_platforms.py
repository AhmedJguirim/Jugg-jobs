"""Greenhouse and Workable in the page fetcher, and the pacing of requests.
Same rules as test_listing_fetch.py: no test makes a real request. The pages and
JSON are real responses saved under tests/fixtures."""

from __future__ import annotations

import pathlib

import pytest

from app.integrations.listing_pages import extract, workable
from app.integrations.listing_pages.client import Page
from app.parsers import ListingNotFound, ParsedListing
from app.parsers.greenhouse import parse as parse_greenhouse
from app.parsers.workable import parse as parse_workable
from app.repositories import job_listing_test as repo
from app.services import listing_fetch
from app.text.dom import parse_html
from tests.unit.test_listing_fetch import asked_for, cut, served

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
GH_URL = "https://job-boards.greenhouse.io/shifttechnology/jobs/8044723003"
WK_URL = "https://apply.workable.com/total-life/j/617A4D5C9F"
WK_JOB_API = "https://apply.workable.com/api/v2/accounts/total-life/jobs/617A4D5C9F"
WK_ACCOUNT_API = "https://apply.workable.com/api/v1/accounts/total-life"


def greenhouse_response() -> str:
    """A real server response (Shift Technology): the whole rendered page."""
    return (FIXTURES / "greenhouse" / "shift_server_response.html").read_text(encoding="utf-8")


class TestGreenhouse:
    def test_the_block_is_cut_out_in_the_shape_of_a_hand_copy(self):
        page = greenhouse_response()
        block = cut("greenhouse", page)
        top = [c for c in parse_html(block).children if hasattr(c, "tag")]

        assert len(top) == 1 and top[0].tag == "div" and not top[0].classes
        classes = {c for child in top[0].children if hasattr(child, "classes") for c in child.classes}
        assert {"job__header", "job__description", "job-alert"} <= classes
        assert len(block) < len(page) / 4

    def test_the_parser_reads_the_cut_block(self):
        job = parse_greenhouse(GH_URL, cut("greenhouse", greenhouse_response())).job

        assert (job.source, job.external_id) == ("greenhouse", "8044723003")
        assert job.title == "Lead AI Engineer - ML/LLM Ops & Agentic AI"
        assert job.company_name == "Shift Technology" and job.description

    def test_the_old_board_link_is_requested_as_it_is_and_tracking_is_dropped(self):
        old = "https://boards.greenhouse.io/shifttechnology/jobs/8044723003?gh_src=abc#x"

        # The server answers it with a 301 to job-boards.; the client follows it.
        assert asked_for("greenhouse", old) == ["https://boards.greenhouse.io/shifttechnology/jobs/8044723003"]

    def test_a_removed_job_redirects_to_its_board_and_is_read_as_gone(self):
        board = "<html><body><h1>Current openings at Shift</h1></body></html>"

        def fetch(url, **kw):
            return Page(200, board, len(board), "https://job-boards.greenhouse.io/shifttechnology?error=true")

        outcome = listing_fetch.fetch_listing(GH_URL, fetch=fetch)

        assert (outcome["status"], outcome["http_status"]) == (repo.NOT_FOUND, 200)
        assert isinstance(parse_greenhouse(GH_URL, outcome["html_body"]), ListingNotFound)

    def test_a_page_with_no_job_and_no_error_redirect_is_no_content(self):
        outcome = listing_fetch.fetch_listing(GH_URL, fetch=served(200, "<html><body>hi</body></html>"))

        assert outcome["status"] == repo.NO_CONTENT

    def test_a_fetched_job_is_stored(self):
        outcome = listing_fetch.fetch_listing(GH_URL, fetch=served(200, greenhouse_response()))

        assert outcome["status"] == repo.FETCHED and "job__description" in outcome["html_body"]


def workable_get(log=None, account_status=200, job_status=200):
    """A getter answering Workable's endpoints from the saved real responses."""
    job = (FIXTURES / "workable" / "total_life_job.json").read_text(encoding="utf-8")
    account = (FIXTURES / "workable" / "total_life_account.json").read_text(encoding="utf-8")

    def get(url, **kw):
        if log is not None:
            log.append((url, kw.get("json", False)))
        if url == WK_JOB_API:
            return Page(job_status, job if job_status == 200 else "Not Found", len(job), url)
        if url == WK_ACCOUNT_API:
            return Page(account_status, account if account_status == 200 else "", len(account), url)
        if url == "https://apply.workable.com/j/617A4D5C9F":  # a link with no company redirects
            return Page(200, "<html>shell</html>", 18, WK_URL)
        return Page(404, "Not Found", 9, url)

    return get


@pytest.fixture(autouse=True)
def _fresh_workable_accounts():
    workable._accounts.clear()
    yield
    workable._accounts.clear()


class TestWorkable:
    def test_the_job_and_the_company_are_asked_for_as_json(self):
        log: list = []

        outcome = listing_fetch.fetch_listing(WK_URL, fetch=workable_get(log))

        assert outcome["status"] == repo.FETCHED
        assert log == [(WK_JOB_API, True), (WK_ACCOUNT_API, True)]

    def test_the_parser_reads_the_rebuilt_block(self):
        outcome = listing_fetch.fetch_listing(WK_URL, fetch=workable_get())
        result = parse_workable(WK_URL, outcome["html_body"])

        assert isinstance(result, ParsedListing)
        job = result.job
        assert (job.source, job.external_id) == ("workable", "617A4D5C9F")
        assert job.title == "Founding Head of Engineering and AI of a Seed Stage Startup"
        assert job.company_name == "Total Life, Inc."
        assert job.raw["sections"] == {
            "Location Type": "Hybrid", "Department": "Engineering",
            "Employment Type": "Full time", "Location": "Vancouver, British Columbia, Canada",
        }
        assert (job.location.city, job.location.province) == ("Vancouver", "British Columbia")
        assert job.raw["board"] == "total-life"
        assert "What You Bring" in job.description and "Why Total Life" in job.description

    def test_each_section_has_the_heading_the_page_shows(self):
        block = listing_fetch.fetch_listing(WK_URL, fetch=workable_get())["html_body"]

        for heading in ("<h2>Description</h2>", "<h2>Requirements</h2>", "<h2>Benefits</h2>"):
            assert heading in block
        assert 'data-synthetic="api"' in block

    def test_a_company_is_asked_about_once_however_many_of_its_jobs(self):
        log: list = []
        get = workable_get(log)

        listing_fetch.fetch_listing(WK_URL, fetch=get)
        listing_fetch.fetch_listing(WK_URL + "/apply", fetch=get)

        assert [url for url, _ in log].count(WK_ACCOUNT_API) == 1

    def test_a_link_without_a_company_learns_it_from_the_redirect(self):
        log: list = []

        outcome = listing_fetch.fetch_listing("https://apply.workable.com/j/617A4D5C9F", fetch=workable_get(log))

        assert outcome["status"] == repo.FETCHED
        assert [url for url, _ in log][:2] == ["https://apply.workable.com/j/617A4D5C9F", WK_JOB_API]

    def test_the_application_form_link_asks_for_the_job(self):
        log: list = []
        listing_fetch.fetch_listing(WK_URL + "/apply/", fetch=workable_get(log))

        assert log[0] == (WK_JOB_API, True)

    def test_a_removed_job_is_a_404_the_parser_reads_as_gone(self):
        outcome = listing_fetch.fetch_listing(WK_URL, fetch=workable_get(job_status=404))

        assert (outcome["status"], outcome["http_status"]) == (repo.NOT_FOUND, 404)
        assert isinstance(parse_workable(WK_URL, outcome["html_body"]), ListingNotFound)

    def test_without_the_company_the_job_is_still_stored_just_unnamed(self):
        outcome = listing_fetch.fetch_listing(WK_URL, fetch=workable_get(account_status=500))

        assert outcome["status"] == repo.FETCHED
        assert parse_workable(WK_URL, outcome["html_body"]).job.company_name is None

    def test_a_refused_job_request_is_blocked(self):
        outcome = listing_fetch.fetch_listing(WK_URL, fetch=workable_get(job_status=429))

        assert outcome["status"] == repo.BLOCKED

    def test_the_shell_the_page_serves_has_nothing_the_parser_reads(self):
        shell = (FIXTURES / "workable" / "total_life_server_shell.html").read_text(encoding="utf-8")

        assert "data-ui" not in shell and "<h1" not in shell


class TestPacing:
    def test_every_request_is_spaced_even_the_ones_for_the_same_link(self):
        sleeps: list[float] = []
        log: list = []

        get = listing_fetch.paced(workable_get(log), sleeps.append, lambda: 1.5)
        listing_fetch.fetch_listing(WK_URL, fetch=get)  # job + company = 2 requests

        assert len(log) == 2 and sleeps == [1.5]

    def test_the_first_request_of_a_run_is_not_delayed(self):
        sleeps: list[float] = []

        get = listing_fetch.paced(served(), sleeps.append, lambda: 1.5)
        get("https://jobs.lever.co/a/b")

        assert sleeps == []
