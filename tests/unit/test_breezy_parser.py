"""The Breezy parser, run against nine pages copied from the live site
(tests/fixtures/breezy). Every one is only the job's description block, so the title
and company come from the URL. It is not hooked into the real flow yet."""

from __future__ import annotations

import pathlib

import pytest

from app.parsers import (
    ListingNotFound,
    ListingParseError,
    ParsedListing,
    detect_platform,
    parse_listing,
)
from app.parsers.breezy import parse
from app.parsers.common import company_from_slug, slug_words, title_from_slug
from app.text.dom import parse_html

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "breezy"

# url, fixture, title, title source, company, company source, id
CASES = [
    ("https://20four7va.breezy.hr/p/174ef3f9cc50-b-cpt-11601-bilingual-student-operations-administrative-virtual-assistant",
     "20four7va_virtual_assistant.html",
     "B-CPT-11601 Bilingual Student Operations & Administrative Virtual Assistant", "text", "20four7VA", "text", "174ef3f9cc50"),
    ("https://uvation.breezy.hr/p/b36d80580363-senior-frontend-developer-tech-lead",
     "uvation_senior_frontend_developer.html", "Senior Frontend Developer Tech Lead", "slug", "Uvation", "slug", "b36d80580363"),
    ("https://vigil-global.breezy.hr/p/c00f53764a7a01-senior-full-stack-engineer-tech-lead",
     "vigil_global_senior_full_stack_engineer.html", "Senior Full Stack Engineer Tech Lead", "slug", "Vigil Global", "slug", "c00f53764a7a01"),
    ("https://localize.breezy.hr/p/4ba5bc0d9700-senior-platform-engineer",
     "localize_senior_platform_engineer.html", "Senior Platform Engineer", "text", "Localize", "text", "4ba5bc0d9700"),
    ("https://inallmedia-llc.breezy.hr/p/8b4032373b4d-back-end-data-engineer",
     "inallmedia_back_end_data_engineer.html", "Back End Data Engineer", "slug", "Inallmedia LLC", "slug", "8b4032373b4d"),
    ("https://rockcruit.breezy.hr/p/e04e94db14f5-he4-front-end-developer-react-react-router-typescript",
     "rockcruit_front_end_developer.html", "He4 Front End Developer React React Router Typescript", "slug", "Rockcruit", "slug", "e04e94db14f5"),
    ("https://drips.breezy.hr/p/776303d6160f-senior-full-stack-software-engineer",
     "drips_senior_full_stack_engineer.html", "Senior Full Stack Software Engineer", "text", "Drips", "text", "776303d6160f"),
    ("https://vetsez.breezy.hr/p/c6709fcf09f701-applied-ai-product-engineer-remote-opportunity",
     "vetsez_applied_ai_product_engineer.html", "Applied AI Product Engineer Remote Opportunity", "slug", "VetsEZ", "text", "c6709fcf09f701"),
    ("https://atlas-technica.breezy.hr/p/5a285a74ccb1-quality-assurance-engineer",
     "atlas_technica_qa_engineer.html", "Quality Assurance Engineer", "text", "Atlas Technica", "text", "5a285a74ccb1"),
]
URLS = [c[0] for c in CASES]
FILES = {c[0]: c[1] for c in CASES}


def page(url: str) -> str:
    return (FIXTURES / FILES[url]).read_text(encoding="utf-8")


def job(url: str):
    result = parse(url, page(url))
    assert isinstance(result, ParsedListing)
    return result.job


@pytest.mark.parametrize("url, _file, title, title_source, company, company_source, job_id", CASES)
class TestEachSavedPage:
    def test_title_company_and_id(self, url, _file, title, title_source, company, company_source, job_id):
        j = job(url)

        assert (j.source, j.external_id) == ("breezy", job_id)
        assert (j.title, j.job_titles) == (title, [title])
        assert j.company_name == company

    def test_how_each_was_found_is_recorded(self, url, _file, title, title_source, company, company_source, job_id):
        raw = job(url).raw

        assert (raw["title_source"], raw["company_source"]) == (title_source, company_source)
        assert raw["board"] == url.split("//")[1].split(".breezy.hr")[0]

    def test_links_are_the_pages_own_url(self, url, _file, title, title_source, company, company_source, job_id):
        j = job(url)

        assert j.detail_url == j.apply_url == url

    def test_every_word_of_the_description_is_kept(self, url, _file, title, title_source, company, company_source, job_id):
        source = parse_html(page(url)).find(cls="description").text().split()

        assert parse_html(job(url).description).text().split() == source

    def test_only_formatting_survives(self, url, _file, title, title_source, company, company_source, job_id):
        description = job(url).description

        for unsafe in ("<script", "<iframe", "style=", "class=", "data-faitracker", "onclick", "javascript:"):
            assert unsafe not in description

    def test_no_salary_is_invented(self, url, _file, title, title_source, company, company_source, job_id):
        # None of these pages states a figure ("Competitive salary" has none).
        j = job(url)

        assert (j.salary_min, j.salary_max, j.salary_type) == (None, None, None)
        assert j.location is None and j.raw["sections"] == {}


class TestDescriptionDetails:
    def test_links_keep_their_href_and_lose_tracking_attributes(self):
        description = job(URLS[0]).description

        assert 'href="https://20four7va.breezy.hr/team/signin"' in description
        assert "data-faitracker" not in description

    def test_headings_are_kept(self):
        assert "<h1>" in job(URLS[0]).description


def make(body: str) -> str:
    return f'<div class="description">{body}</div>'


UUID = "174ef3f9cc50"
URL = f"https://acme.breezy.hr/p/{UUID}-backend-engineer"


class TestPay:
    def test_a_salary_sentence(self):
        j = parse(URL, make("<p>The salary range for this role is $60,000 - $80,000 per year.</p>")).job

        assert (j.salary_min, j.salary_max, j.salary_type) == (60000, 80000, "yearly")
        assert j.raw["compensation"]["currency"] == "USD"

    def test_an_hourly_rate(self):
        j = parse(URL, make("<p>The hourly rate is $25 - $30 per hour.</p>")).job

        assert (j.salary_min, j.salary_max, j.salary_type) == (25, 30, "hourly")

    def test_competitive_salary_without_a_figure(self):
        body = "<ul><li>5+ years of experience</li><li>Competitive salary and performance-based bonuses.</li></ul>"

        assert parse(URL, make(body)).job.salary_min is None

    def test_a_bonus_is_not_salary(self):
        assert parse(URL, make("<p>An annual bonus of $10,000 is paid.</p>")).job.salary_min is None


class TestUrl:
    @pytest.mark.parametrize("url, company, job_id, detail_url", [
        (f"https://acme.breezy.hr/p/{UUID}-backend-engineer", "acme", UUID,
         f"https://acme.breezy.hr/p/{UUID}-backend-engineer"),
        (f"https://acme.breezy.hr/p/{UUID}-backend-engineer/", "acme", UUID,
         f"https://acme.breezy.hr/p/{UUID}-backend-engineer"),                      # trailing slash dropped
        (f"https://acme.breezy.hr/p/{UUID}-backend-engineer?source=x", "acme", UUID,
         f"https://acme.breezy.hr/p/{UUID}-backend-engineer"),                      # tracking parameter dropped
        ("https://big-co.breezy.hr/p/C00F53764A7A01-backend-engineer", "big-co", "c00f53764a7a01",
         "https://big-co.breezy.hr/p/C00F53764A7A01-backend-engineer"),             # id lower-cased only as the key
        (f"  https://ACME.breezy.hr/p/{UUID}-backend-engineer  ", "acme", UUID,
         f"https://acme.breezy.hr/p/{UUID}-backend-engineer"),
    ])
    def test_company_id_and_canonical_link(self, url, company, job_id, detail_url):
        j = parse(url, make("<p>Build things.</p>")).job

        assert (j.external_id, j.raw["board"], j.detail_url) == (job_id, company, detail_url)
        assert j.title == "Backend Engineer"   # the slug carries no slash or query

    def test_a_url_with_no_title_slug_uses_the_first_heading(self):
        j = parse(f"https://acme.breezy.hr/p/{UUID}", make("<h1>Staff Engineer</h1><p>Text.</p>")).job

        assert j.title == "Staff Engineer" and j.raw["title_source"] == "text"

    def test_a_url_with_no_slug_and_no_heading_is_unreadable(self):
        with pytest.raises(ListingParseError, match="no title"):
            parse(f"https://acme.breezy.hr/p/{UUID}", make("<p>Just text.</p>"))

    @pytest.mark.parametrize("url", [
        "https://breezy.hr/p/174ef3f9cc50-x",                    # no company subdomain
        "https://acme.example.com/p/174ef3f9cc50-x",             # not breezy
        "https://acme.breezy.hr/",
        "https://acme.breezy.hr/p/short-x",
        "https://acme.breezy.hr/positions/174ef3f9cc50",
    ])
    def test_urls_that_are_not_a_breezy_job(self, url):
        with pytest.raises(ListingParseError):
            parse(url, make("<p>x</p>"))


class TestNotFoundAndUnreadable:
    def test_a_page_that_says_the_position_is_gone(self):
        html = '<div class="positions"><p>This position is no longer available.</p></div>'

        result = parse(URL, html)

        assert isinstance(result, ListingNotFound) and result.platform == "breezy"

    def test_a_real_description_that_mentions_the_phrase_is_still_a_job(self):
        assert isinstance(parse(URL, make("<p>Our old office is no longer open.</p>")), ParsedListing)

    @pytest.mark.parametrize("html", ["", "<html><body><h1>Welcome</h1></body></html>", '<div class="description"></div>'])
    def test_other_pages_are_unreadable(self, html):
        with pytest.raises(ListingParseError):
            parse(URL, html)


class TestSlugHelpers:
    @pytest.mark.parametrize("slug, text, expected", [
        ("20four7va", "Welcome to 20four7VA Recruiting", ("20four7VA", "text")),
        ("insiderone", "At Insider One we build", ("Insider One", "text")),
        ("vetsez", "As part of EZLabs of VetsEZ, this role", ("VetsEZ", "text")),   # trailing comma is not part of it
        ("jobgether", "nothing here", ("Jobgether", "slug")),
        ("inallmedia-llc", "nothing", ("Inallmedia LLC", "slug")),
        ("vigil-global", "Vigil Global is a company", ("Vigil Global", "text")),
        ("insiderone", "insider one in lower case", ("Insiderone", "slug")),
    ])
    def test_company(self, slug, text, expected):
        assert company_from_slug(slug, text) == expected

    @pytest.mark.parametrize("slug, text, expected", [
        ("senior-full-stack-engineer-tech-lead", "We want a Senior Full-Stack Engineer - Tech Lead to join",
         ("Senior Full-Stack Engineer - Tech Lead", "text")),
        ("quality-assurance-engineer", "Role: Quality Assurance Engineer.", ("Quality Assurance Engineer", "text")),
        ("b-cpt-11601-bilingual-assistant", "Title: B-CPT-11601 Bilingual Assistant", ("B-CPT-11601 Bilingual Assistant", "text")),
        ("backend-engineer", "We hire people", ("Backend Engineer", "slug")),
        ("applied-ai-product-engineer", "nothing", ("Applied AI Product Engineer", "slug")),
        ("senior-qa-engineer", "nothing", ("Senior QA Engineer", "slug")),
    ])
    def test_title(self, slug, text, expected):
        assert title_from_slug(slug, text) == expected

    def test_a_title_must_match_whole_words_not_a_fragment(self):
        # "engineer" alone is in the text, but the slug is longer: no false match.
        assert title_from_slug("senior-engineer", "We need an engineer", ) == ("Senior Engineer", "slug")

    def test_slug_words(self):
        assert slug_words("senior--ai_engineer") == "Senior AI Engineer"


class TestNotHookedYet:
    """Breezy is not in the platform registry, so no listing reaches this parser
    through the real flow. When it is hooked, replace this class with detection tests."""

    def test_the_platform_is_not_detected(self):
        assert detect_platform(URLS[0]) is None

    def test_the_registry_has_no_breezy_parser(self):
        with pytest.raises(ListingParseError):
            parse_listing("breezy", URLS[0], page(URLS[0]))
