"""Fetching posting pages: the cut-down to what a hand copy would be, the
outcomes, and the pacing. No test makes a real request."""

from __future__ import annotations

import pathlib

import httpx
import pytest

from app.integrations.listing_pages import extract
from app.integrations.listing_pages.client import Page
from app.parsers import ListingNotFound, ParsedListing
from app.parsers.ashbyhq import parse as parse_ashby
from app.parsers.lever import parse
from app.services import listing_fetch

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "lever"
ASHBY_FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "ashbyhq"
ASHBY_URL = "https://jobs.ashbyhq.com/cyberhaven/020d895a-a68e-400d-8b19-71634a331915"
URL = "https://jobs.lever.co/jobgether/29ff8655-9468-4c31-8804-93b79abeb2a8"


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

        assert outcome["status"] == listing_fetch.FETCHED and outcome["http_status"] == 200
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

        assert outcome["status"] == listing_fetch.NOT_FOUND and len(outcome["html_body"]) < 200
        assert isinstance(parse(URL, outcome["html_body"]), ListingNotFound)

    @pytest.mark.parametrize("code", [403, 429, 503])
    def test_a_refusal_is_blocked_and_stores_nothing(self, code):
        outcome = listing_fetch.fetch_listing(URL, fetch=served(code, "denied"))

        assert (outcome["status"], outcome["html_body"], outcome["note"]) == (listing_fetch.BLOCKED, "", f"HTTP {code}")

    def test_a_page_without_the_block_is_no_content(self):
        outcome = listing_fetch.fetch_listing(URL, fetch=served(200, "<html><body>hi</body></html>"))

        assert outcome["status"] == listing_fetch.NO_CONTENT and outcome["html_body"] == ""

    def test_a_network_failure_is_an_outcome_not_a_crash(self):
        def fetch(url, **kw):
            raise httpx.ConnectTimeout("timed out")

        outcome = listing_fetch.fetch_listing(URL, fetch=fetch)

        assert outcome["status"] == listing_fetch.ERROR and "ConnectTimeout" in outcome["note"]

    def test_a_site_without_a_fetcher_makes_no_request(self):
        def fetch(url, **kw):
            raise AssertionError("must not be requested")

        for url in ("https://www.linkedin.com/jobs/view/1", "https://example.com/job/1"):
            outcome = listing_fetch.fetch_listing(url, fetch=fetch)
            assert outcome["status"] == listing_fetch.UNSUPPORTED and outcome["requested"] is False


class TestPacing:
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

        assert outcome["status"] == listing_fetch.NOT_FOUND
        assert isinstance(parse_ashby(ASHBY_URL, outcome["html_body"]), ListingNotFound)

    def test_a_fetched_posting_is_stored_and_parses(self):
        outcome = listing_fetch.fetch_listing(ASHBY_URL, fetch=served(200, ashby_response()))

        assert outcome["status"] == listing_fetch.FETCHED
        assert isinstance(parse_ashby(ASHBY_URL, outcome["html_body"]), ParsedListing)
