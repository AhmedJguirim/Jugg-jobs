"""Breezy in the page fetcher. No test makes a real request; the page is a real
server response saved under tests/fixtures/breezy."""

from __future__ import annotations

import pathlib

from app.integrations.listing_pages import extract
from app.integrations.listing_pages.client import Page
from app.parsers import ListingNotFound, ParsedListing
from app.parsers.breezy import parse
from app.repositories import job_listing_test as repo
from app.services import listing_fetch
from app.text.dom import parse_html
from tests.unit.test_listing_fetch import asked_for, cut, served

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "breezy"
URL = "https://aimpoint-digital.breezy.hr/p/f116e82e2b81-lead-ai-engineer-2026-us-col-uk"


def response() -> str:
    """A real server response (Aimpoint Digital): the whole page."""
    return (FIXTURES / "aimpoint_server_response.html").read_text(encoding="utf-8")


def test_breezy_is_supported():
    assert extract.supported("breezy")


def test_the_description_block_is_cut_out_like_a_hand_copy():
    page = response()
    block = cut("breezy", page)
    top = [c for c in parse_html(block).children if hasattr(c, "tag")]

    assert len(top) == 1 and top[0].tag == "div" and top[0].classes == {"description"}
    assert len(block) < len(page) / 3


def test_the_hand_copies_have_the_same_shape():
    for path in FIXTURES.glob("*_*.html"):
        if path.name == "aimpoint_server_response.html":
            continue
        top = [c for c in parse_html(path.read_text(encoding="utf-8")).children if hasattr(c, "tag")]
        assert [(c.tag, c.classes) for c in top] == [("div", {"description"})], path.name


def test_the_parser_reads_the_cut_block():
    result = parse(URL, cut("breezy", response()))

    assert isinstance(result, ParsedListing)
    job = result.job
    assert (job.source, job.external_id) == ("breezy", "f116e82e2b81")
    assert job.raw["board"] == "aimpoint-digital" and job.company_name
    assert job.description and "Aimpoint Digital" in job.description


def test_the_link_is_requested_without_its_tracking():
    assert asked_for("breezy", URL + "?source=google#apply") == [URL]


def test_a_removed_position_redirects_to_the_company_page_and_is_read_as_gone():
    company_page = "<html><body><h1>Open positions</h1></body></html>"

    def fetch(url, **kw):
        return Page(200, company_page, len(company_page), "https://aimpoint-digital.breezy.hr/")

    outcome = listing_fetch.fetch_listing(URL, fetch=fetch)

    assert outcome["status"] == repo.NOT_FOUND
    assert isinstance(parse(URL, outcome["html_body"]), ListingNotFound)


def test_a_closed_position_that_stays_on_its_link_is_read_as_gone():
    closed = (
        "<html><head><title>Senior Engineer at Drips</title></head><body>"
        "<h1>Position Closed</h1><p>Sorry, this position is no longer accepting candidates.</p></body></html>"
    )

    outcome = listing_fetch.fetch_listing(URL, fetch=served(200, closed))

    assert outcome["status"] == repo.NOT_FOUND
    assert isinstance(parse(URL, outcome["html_body"]), ListingNotFound)


def test_a_live_position_whose_text_says_closed_is_still_a_job():
    page = response().replace("Aimpoint Digital is", "Position Closed is", 1)

    assert listing_fetch.fetch_listing(URL, fetch=served(200, page))["status"] == repo.FETCHED


def test_a_page_that_stays_on_the_position_but_has_no_description_is_no_content():
    outcome = listing_fetch.fetch_listing(URL, fetch=served(200, "<html><body>hi</body></html>"))

    assert outcome["status"] == repo.NO_CONTENT


def test_a_fetched_position_is_stored():
    outcome = listing_fetch.fetch_listing(URL, fetch=served(200, response()))

    assert outcome["status"] == repo.FETCHED
    assert outcome["html_body"].startswith('<div class="description">')
