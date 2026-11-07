"""Fetching posting pages into `job_listings_test`: the cut-down, the outcomes,
the pacing, and the queue of links. No test makes a real request."""

from __future__ import annotations

import pathlib
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app.db.models.job_listing_test import JobListingTest
from app.db.models.manual_job_listing_url import ManualJobListingUrl
from app.db.models.raw_job_listing import RawJobListing
from app.integrations.listing_pages import extract
from app.integrations.listing_pages.client import Page
from app.parsers import ListingNotFound, ParsedListing
from app.parsers.ashbyhq import parse as parse_ashby
from app.parsers.lever import parse
from app.repositories import job_listing_test as repo
from app.services import listing_fetch

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "lever"
ASHBY_FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "ashbyhq"
ASHBY_URL = "https://jobs.ashbyhq.com/cyberhaven/020d895a-a68e-400d-8b19-71634a331915"
URL = "https://jobs.lever.co/jobgether/29ff8655-9468-4c31-8804-93b79abeb2a8"
OTHER = "https://jobs.lever.co/jobgether/5e8a2b39-fa91-4a63-895e-31411cc57f6f"


def asked_for(platform: str, url: str) -> list[str]:
    """The addresses requested for a collected link."""
    asked: list[str] = []

    def get(address, **kw):
        asked.append(address)
        return Page(200, "", 0, address)

    extract.request(platform, url, get)
    return asked


def cut(platform: str, html: str, final_url: str = "", get=None):
    """The block a page is cut down to (the step after the request)."""
    return extract.extract_posting(platform, Page(200, html, len(html), final_url), get or (lambda url, **kw: pytest.fail(f"unexpected request {url}")))


def block() -> str:
    return (FIXTURES / "jobgether_full_stack_microsoft_developer.html").read_text(encoding="utf-8")


def whole_page(inner: str) -> str:
    """What the server sends: the posting block buried in a lot of page. A decoy
    `content` div comes first, as on the live site."""
    return (
        "<!DOCTYPE html><html><head><title>x</title><script>var a = '<div class=\"content\">';</script>"
        "<style>.content{}</style></head><body><div class=\"content\"><p>cookie banner</p></div>"
        f"<div class=\"page\">{inner}</div><footer>links</footer></body></html>"
    )


def served(status: int = 200, text: str | None = None):
    body = whole_page(block()) if text is None else text
    return lambda url, **kw: Page(status_code=status, text=body, size=len(body), final_url=url)


class TestExtract:
    def test_every_platform_with_a_parser_is_supported(self):
        from app.parsers import PLATFORM_HOSTS

        assert all(extract.supported(p) for p in set(PLATFORM_HOSTS.values()))
        assert not extract.supported(None)

    def test_the_block_is_cut_out_of_the_page(self):
        cut_block = cut("lever", whole_page(block()))

        assert cut_block.startswith('<div class="content">') and "posting-headline" in cut_block
        assert "cookie banner" not in cut_block and "footer" not in cut_block

    def test_it_parses_to_the_same_job_as_the_hand_copy(self):
        by_hand = parse(URL, block())
        fetched = parse(URL, cut("lever", whole_page(block())))

        assert isinstance(by_hand, ParsedListing) and isinstance(fetched, ParsedListing)
        assert fetched.job.model_dump() == by_hand.job.model_dump()

    def test_a_page_without_a_posting_gives_none(self):
        assert cut("lever", whole_page("<p>nothing</p>")) is None

    def test_the_application_form_link_asks_for_the_posting_page(self):
        assert asked_for("lever", URL + "/apply") == [URL]
        assert asked_for("lever", URL + "?lever-source=x#top") == [URL]


class TestFetchListing:
    def test_a_posting_is_stored_as_its_block(self):
        outcome = listing_fetch.fetch_listing(URL, fetch=served())

        assert outcome["status"] == repo.FETCHED and outcome["http_status"] == 200
        assert outcome["html_body"].startswith('<div class="content">')
        assert outcome["page_bytes"] > len(outcome["html_body"])

    def test_it_asks_for_the_posting_not_the_apply_form(self):
        asked = []

        def fetch(url, **kw):
            asked.append(url)
            return Page(200, whole_page(block()), 1, url)

        listing_fetch.fetch_listing(URL + "/apply", fetch=fetch)

        assert asked == [URL]

    def test_a_gone_posting_is_stored_small_and_the_parser_reads_it_as_gone(self):
        page = "<html>" + "<p>Sorry, we couldn't find anything here</p>" + "x" * 300_000 + "</html>"

        outcome = listing_fetch.fetch_listing(URL, fetch=served(404, page))

        assert outcome["status"] == repo.NOT_FOUND and len(outcome["html_body"]) < 200
        assert isinstance(parse(URL, outcome["html_body"]), ListingNotFound)

    @pytest.mark.parametrize("code", [403, 429, 503])
    def test_a_refusal_is_blocked_and_stores_nothing(self, code):
        outcome = listing_fetch.fetch_listing(URL, fetch=served(code, "denied"))

        assert (outcome["status"], outcome["html_body"], outcome["note"]) == (repo.BLOCKED, "", f"HTTP {code}")

    def test_a_page_without_the_block_is_no_content(self):
        outcome = listing_fetch.fetch_listing(URL, fetch=served(200, "<html><body>hi</body></html>"))

        assert outcome["status"] == repo.NO_CONTENT and outcome["html_body"] == ""

    def test_a_network_failure_is_an_outcome_not_a_crash(self):
        def fetch(url, **kw):
            raise httpx.ConnectTimeout("timed out")

        outcome = listing_fetch.fetch_listing(URL, fetch=fetch)

        assert outcome["status"] == repo.ERROR and "ConnectTimeout" in outcome["note"]

    def test_a_site_without_a_fetcher_makes_no_request(self):
        def fetch(url, **kw):
            raise AssertionError("must not be requested")

        for url in ("https://www.linkedin.com/jobs/view/1", "https://example.com/job/1"):
            outcome = listing_fetch.fetch_listing(url, fetch=fetch)
            assert outcome["status"] == repo.UNSUPPORTED and outcome["requested"] is False


@pytest.fixture
def links(session):
    """Two live Lever links, a removed one, one already pasted by hand, one Ashby."""
    session.add_all(
        [
            ManualJobListingUrl(url=URL),
            ManualJobListingUrl(url=OTHER),
            ManualJobListingUrl(url="https://jobs.lever.co/x/removed", removed_at=datetime.now(timezone.utc)),
            ManualJobListingUrl(url="https://jobs.lever.co/x/by-hand"),
            ManualJobListingUrl(url="https://www.linkedin.com/jobs/view/1"),
            RawJobListing(url="https://jobs.lever.co/x/by-hand", html_body="<div/>"),
        ]
    )
    session.flush()
    return session


class TestPending:
    def test_removed_hand_pasted_and_fetched_links_are_left_out(self, links):
        repo.upsert(links, URL, status=repo.FETCHED, html_body="<div/>")

        pending = repo.pending_urls(links, 10)

        assert pending == [OTHER, "https://www.linkedin.com/jobs/view/1"]

    def test_include_pasted_adds_the_links_already_stored_by_hand(self, links):
        assert "https://jobs.lever.co/x/by-hand" not in repo.pending_urls(links, 10)
        assert "https://jobs.lever.co/x/by-hand" in repo.pending_urls(links, 10, include_pasted=True)

    def test_hosts_keep_only_that_platforms_links(self, links):
        assert repo.pending_urls(links, 10, hosts=["jobs.lever.co"]) == [URL, OTHER]
        assert repo.pending_urls(links, 10, hosts=["linkedin.com"]) == ["https://www.linkedin.com/jobs/view/1"]

    def test_retry_brings_back_only_outcomes_that_may_change(self, links):
        repo.upsert(links, URL, status=repo.BLOCKED)
        repo.upsert(links, OTHER, status=repo.NOT_FOUND)

        assert URL in repo.pending_urls(links, 10, retry=True)
        assert OTHER not in repo.pending_urls(links, 10, retry=True)
        assert URL not in repo.pending_urls(links, 10)

    def test_the_limit_is_respected_oldest_first(self, links):
        assert repo.pending_urls(links, 1) == [URL]

    def test_upsert_replaces_the_earlier_outcome(self, links):
        repo.upsert(links, URL, status=repo.BLOCKED, http_status=403)
        repo.upsert(links, URL, status=repo.FETCHED, html_body="<div>x</div>", http_status=200)

        rows = links.scalars(select(JobListingTest).where(JobListingTest.url == URL)).all()
        assert len(rows) == 1 and (rows[0].status, rows[0].html_body) == (repo.FETCHED, "<div>x</div>")


class TestFetchPending:
    def run(self, links, **kwargs):
        sleeps: list[float] = []

        @contextmanager
        def open_session():
            yield links

        results = listing_fetch.fetch_pending(
            10,
            fetch=kwargs.pop("fetch", served()),
            sleep=sleeps.append,
            delay=lambda: 2.0,
            open_session=open_session,
            **kwargs,
        )
        return results, sleeps

    def test_each_outcome_is_stored(self, links):
        results, _ = self.run(links)

        assert [r["status"] for r in results] == [repo.FETCHED, repo.FETCHED, repo.UNSUPPORTED]
        stored = {row.url: row.status for row in links.scalars(select(JobListingTest))}
        assert stored == {URL: "fetched", OTHER: "fetched", "https://www.linkedin.com/jobs/view/1": "unsupported"}

    def test_requests_are_spaced_and_never_before_the_first(self, links):
        _, sleeps = self.run(links)

        # Two real requests -> one pause between them. The unsupported link
        # makes no request, so it neither waits nor makes the next one wait.
        assert sleeps == [2.0]

    def test_a_second_run_has_nothing_left(self, links):
        self.run(links)

        results, sleeps = self.run(links)

        assert results == [] and sleeps == []

    def test_the_default_pause_is_random_between_one_and_three_seconds(self):
        draws = [listing_fetch.random_delay() for _ in range(200)]

        assert all(1.0 <= d <= 3.0 for d in draws)
        assert len(set(draws)) > 1


def ashby_response() -> str:
    """A real server response (Cyberhaven): the shell plus `window.__appData`."""
    return (ASHBY_FIXTURES / "cyberhaven_server_response.html").read_text(encoding="utf-8")


def app_data_page(posting, organization=None) -> str:
    import json

    data = {"organization": organization or {"name": "Acme", "publicWebsite": "https://acme.test/", "theme": {}}, "posting": posting}
    return f"<html><script>\n window.__appData = {json.dumps(data)};\n // more script\n</script></html>"


class TestAshby:
    def test_the_shell_has_no_posting_but_the_block_is_rebuilt_from_its_data(self):
        shell = ashby_response()
        assert "ashby-job-posting-heading" not in shell

        block = cut("ashbyhq", shell)

        assert 'data-synthetic="appData"' in block and "ashby-job-posting-heading" in block
        assert len(block) < len(shell)

    def test_the_parser_reads_the_rebuilt_block(self):
        result = parse_ashby(ASHBY_URL, cut("ashbyhq", ashby_response()))

        assert isinstance(result, ParsedListing)
        job = result.job
        assert (job.source, job.external_id) == ("ashbyhq", "020d895a-a68e-400d-8b19-71634a331915")
        assert job.title == "Senior/Staff Software Engineer (Content Processing)"
        assert job.company_name == "Cyberhaven" and job.company_website == "https://www.cyberhaven.com/"
        assert job.raw["sections"] == {
            "Location": "EU Remote; Bangalore", "Employment Type": "Full time",
            "Location Type": "Remote", "Department": "Development",
        }
        assert "About the role" in job.description

    def test_address_and_compensation_become_the_labelled_facts_the_parser_reads(self):
        page = app_data_page({
            "title": "Dev", "descriptionHtml": "<p>Hi</p>", "locationName": "New York",
            "secondaryLocationNames": ["Remote"], "locationAddress": "Warsaw, Poland",
            "workplaceType": "OnSite", "employmentType": "PartTime", "departmentName": "Eng",
            "compensationTierSummary": "$250K – $400K",
        })

        job = parse_ashby(ASHBY_URL, cut("ashbyhq", page)).job

        assert job.raw["sections"]["Location"] == "New York; Remote"
        assert (job.raw["sections"]["Employment Type"], job.raw["sections"]["Location Type"]) == ("Part time", "On-site")
        assert (job.location.city, job.location.province) == ("Warsaw", "Poland")
        assert (job.salary_min, job.salary_max) == (250_000, 400_000)

    def test_values_are_escaped_not_injected(self):
        page = app_data_page({"title": '<script>alert(1)</script> & "x"', "locationName": "<b>x</b>"})

        block = cut("ashbyhq", page)

        assert "<script>" not in block and "<b>x</b>" not in block

    def test_no_posting_in_the_data_is_no_block(self):
        assert cut("ashbyhq", app_data_page(None)) is None
        assert cut("ashbyhq", "<html>no data</html>") is None
        assert cut("ashbyhq", "<script>window.__appData = {broken</script>") is None

    def test_the_application_form_link_asks_for_the_posting(self):
        assert asked_for("ashbyhq", ASHBY_URL + "/application?utm=x") == [ASHBY_URL]

    def test_a_removed_posting_is_a_404_the_parser_reads_as_gone(self):
        shell = app_data_page(None)

        outcome = listing_fetch.fetch_listing(ASHBY_URL, fetch=served(404, shell))

        assert outcome["status"] == repo.NOT_FOUND
        assert isinstance(parse_ashby(ASHBY_URL, outcome["html_body"]), ListingNotFound)

    def test_a_fetched_posting_is_stored_and_parses(self):
        outcome = listing_fetch.fetch_listing(ASHBY_URL, fetch=served(200, ashby_response()))

        assert outcome["status"] == repo.FETCHED
        assert isinstance(parse_ashby(ASHBY_URL, outcome["html_body"]), ParsedListing)
