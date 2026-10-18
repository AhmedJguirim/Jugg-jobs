"""The Lever parser, run against four pages copied from the live site
(tests/fixtures/lever). It is not hooked into the real flow yet."""

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
from app.parsers.lever import _company_name, parse
from app.text.dom import parse_html

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "lever"

FULL_STACK = "https://jobs.lever.co/jobgether/29ff8655-9468-4c31-8804-93b79abeb2a8"
STAFF = "https://jobs.lever.co/jobgether/5e8a2b39-fa91-4a63-895e-31411cc57f6f"
QA = "https://jobs.lever.co/jobgether/5d24b642-8c9c-4274-bfe9-5dfbe2fc48ae"
INSIDER = "https://jobs.lever.co/insiderone/97e2350d-5abc-4869-9337-07d3831e7841"

PAGES = {
    FULL_STACK: "jobgether_full_stack_microsoft_developer.html",
    STAFF: "jobgether_staff_backend_engineer.html",
    QA: "jobgether_backend_qa_engineer.html",
    INSIDER: "insiderone_backend_developer_php.html",
}


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def job(url: str):
    result = parse(url, page(PAGES[url]))
    assert isinstance(result, ParsedListing)
    return result.job


class TestJobgetherFullStack:
    def test_identity_and_title(self):
        j = job(FULL_STACK)

        assert (j.source, j.external_id) == ("lever", "29ff8655-9468-4c31-8804-93b79abeb2a8")
        assert j.title == "Full Stack Microsoft Developer" and j.job_titles == [j.title]

    def test_company_is_the_slug_spelt_as_the_page_spells_it(self):
        assert job(FULL_STACK).company_name == "Jobgether"

    def test_the_tags_become_labelled_sections(self):
        # Lever writes "Full-time /"; the slash is a separator, not part of the value.
        assert job(FULL_STACK).raw["sections"] == {
            "Location": "US",
            "Department": "Security & IT – IT",
            "Employment Type": "Full-time",
            "Location Type": "Remote",
        }

    def test_the_salary_is_read_from_the_sentence_about_pay(self):
        j = job(FULL_STACK)

        assert (j.salary_min, j.salary_max, j.salary_type) == (100000, 130000, "yearly")
        assert j.raw["compensation"]["currency"] == "USD"

    def test_links(self):
        j = job(FULL_STACK)

        assert j.detail_url == FULL_STACK and j.apply_url == FULL_STACK + "/apply"

    def test_a_lone_country_is_not_made_a_place(self):
        assert job(FULL_STACK).location is None


class TestTheOthers:
    def test_staff_engineer_has_no_salary(self):
        j = job(STAFF)

        assert j.title == "Staff Backend Engineer, Money Movement"
        assert (j.salary_min, j.salary_max, j.salary_type) == (None, None, None)
        assert j.raw["compensation"] is None and j.raw["sections"]["Location"] == "Netherlands"

    def test_a_days_off_policy_is_not_pay(self):
        # "Take up to 28 unavailable days per year" mentions "per year" and a number.
        assert job(QA).salary_min is None

    def test_company_name_is_recovered_with_its_spacing(self):
        j = job(INSIDER)

        assert j.company_name == "Insider One" and j.raw["board"] == "insiderone"

    def test_a_funding_round_is_not_a_salary(self):
        # "...a $500M Series E led by General Atlantic" is in the About us text.
        j = job(INSIDER)

        assert (j.salary_min, j.salary_max) == (None, None)
        assert j.title == "Backend Software Developer (PHP/Laravel) – Remote"
        assert j.raw["sections"]["Employment Type"] == "Full-Time (Remote)"


class TestDescription:
    @pytest.mark.parametrize("url", list(PAGES))
    def test_every_word_of_the_posting_is_kept_and_nothing_else(self, url):
        root = parse_html(page(PAGES[url]))
        opening = next(n for n in root.iter() if n.attrs.get("data-qa") == "job-description")
        furniture = {"ai-disclaimer", "btn-apply-bottom"}
        expected = " ".join(
            c.text() for c in opening.parent.children
            if hasattr(c, "attrs") and "section" in c.classes and c.attrs.get("data-qa") not in furniture
        ).split()

        assert parse_html(job(url).description).text().split() == expected

    @pytest.mark.parametrize("url", list(PAGES))
    def test_page_furniture_is_not_in_the_description(self, url):
        description = job(url).description

        assert "artificial intelligence (AI) tools" not in description
        assert "apply for this job" not in description.lower()

    @pytest.mark.parametrize("url", list(PAGES))
    def test_only_formatting_survives(self, url):
        description = job(url).description

        for unsafe in ("<script", "<iframe", "style=", "class=", "data-qa", "onclick", "javascript:"):
            assert unsafe not in description

    def test_the_titled_sections_are_included(self):
        description = job(FULL_STACK).description

        assert "Accountabilities:" in description and "Requirements:" in description and "Benefits:" in description


def listing_html(title="Backend Engineer", categories=None, body="<p>Build things.</p>", extra_sections=""):
    categories = categories or [("location", "London, UK"), ("commitment", "Full-time /")]
    tags = "".join(f'<div class="posting-category {css}">{text}</div>' for css, text in categories)
    return (
        '<div class="content"><div class="section-wrapper"><div class="section posting-header">'
        f'<div class="posting-headline"><h2>{title}</h2><div class="posting-categories">{tags}</div></div>'
        '<div class="postings-btn-wrapper"><a class="postings-btn" href="https://jobs.lever.co/acme/x/apply">apply</a></div>'
        '</div></div><div class="section-wrapper">'
        f'<div class="section" data-qa="job-description"><div>{body}</div></div>{extra_sections}'
        '<div class="section" data-qa="btn-apply-bottom"><a href="#">apply for this job</a></div>'
        '</div></div>'
    )


UUID = "ac527b20-e9fc-41d1-841c-bc881e2abef6"


class TestPay:
    def parse(self, body):
        return parse(f"https://jobs.lever.co/acme/{UUID}", listing_html(body=body)).job

    def test_a_salary_sentence(self):
        j = self.parse("<p>The salary range for this role is €60,000 - €80,000 per year.</p>")

        assert (j.salary_min, j.salary_max, j.salary_type) == (60000, 80000, "yearly")
        assert j.raw["compensation"]["currency"] == "EUR"

    def test_equity_and_bonus_are_not_salary(self):
        j = self.parse("<p>Equity of $50,000 - $80,000 and an annual bonus of $10,000.</p>")

        assert j.salary_min is None

    def test_a_sentence_in_a_later_titled_section(self):
        extra = '<div class="section"><h3>Benefits</h3><div><ul><li>Base salary: $90,000 - $110,000 per year.</li></ul></div></div>'

        j = parse(f"https://jobs.lever.co/acme/{UUID}", listing_html(extra_sections=extra)).job

        assert (j.salary_min, j.salary_max) == (90000, 110000)


class TestLocation:
    def test_a_postal_location_becomes_a_place(self):
        j = parse(f"https://jobs.lever.co/acme/{UUID}", listing_html()).job

        assert (j.location.city, j.location.country_name) == ("London", "UK")
        assert j.raw["sections"] == {"Location": "London, UK", "Employment Type": "Full-time"}

    def test_remote_text_is_not_a_place(self):
        html = listing_html(categories=[("location", "Remote - EMEA")])

        assert parse(f"https://jobs.lever.co/acme/{UUID}", html).job.location is None


class TestCompanyName:
    @pytest.mark.parametrize("slug, text, expected", [
        ("insiderone", "At Insider One we build things", "Insider One"),
        ("jobgether", "Jobgether is a platform", "Jobgether"),
        ("my-company", "Welcome to My Company, a great place", "My Company"),
        ("acme", "We build rockets", "Acme"),                       # not on the page: the slug, capitalised
        ("some-co", "nothing here", "Some Co"),
        ("insiderone", "insider one is lowercase here", "Insiderone"),  # only a capitalised spelling counts
    ])
    def test_recovered_from_the_text_or_the_slug(self, slug, text, expected):
        assert _company_name(slug, text) == expected

    def test_a_url_encoded_slug(self):
        j = parse(f"https://jobs.lever.co/My%20Co/{UUID}", listing_html(body="<p>Join My Co today.</p>")).job

        assert j.company_name == "My Co"


class TestUrl:
    @pytest.mark.parametrize("url", [
        f"https://jobs.lever.co/acme/{UUID}",
        f"https://jobs.lever.co/acme/{UUID}/apply",
        f"https://jobs.lever.co/acme/{UUID}?lever-source=LinkedIn",
        f"https://jobs.eu.lever.co/acme/{UUID.upper()}",
        f"  https://jobs.lever.co/acme/{UUID}  ",
    ])
    def test_the_job_is_identified_by_its_uuid_alone(self, url):
        j = parse(url, listing_html()).job

        assert j.external_id == UUID
        assert j.detail_url.endswith(f"/acme/{UUID}")  # no /apply, no query

    @pytest.mark.parametrize("url", [
        "https://jobs.lever.co/acme",
        "https://jobs.lever.co/acme/not-a-uuid",
        "https://jobs.lever.co/",
    ])
    def test_a_url_without_a_job_id(self, url):
        with pytest.raises(ListingParseError):
            parse(url, listing_html())


class TestNotFoundAndUnreadable:
    URL = f"https://jobs.lever.co/acme/{UUID}"

    def test_a_page_that_says_it_found_nothing(self):
        html = '<div class="content"><h1>Sorry, we couldn\'t find anything here</h1></div>'

        assert isinstance(parse(self.URL, html), ListingNotFound)

    def test_a_real_job_that_mentions_the_phrase_is_still_a_job(self):
        html = listing_html(body="<p>Our old office is no longer open.</p>")

        assert isinstance(parse(self.URL, html), ParsedListing)

    @pytest.mark.parametrize("html", ["", "<html><body><h1>Welcome</h1></body></html>"])
    def test_other_pages_are_unreadable(self, html):
        with pytest.raises(ListingParseError):
            parse(self.URL, html)

    def test_a_page_with_a_title_but_no_description_has_none(self):
        html = '<div class="content"><div class="posting-headline"><h2>Engineer</h2></div></div>'

        j = parse(self.URL, html).job

        assert j.title == "Engineer" and j.description is None


class TestRegistered:
    @pytest.mark.parametrize("url", [
        FULL_STACK,
        f"https://jobs.eu.lever.co/acme/{UUID}",
        f"https://jobs.lever.co/acme/{UUID}/apply",
        f" https://JOBS.LEVER.CO/acme/{UUID} ",
    ])
    def test_lever_urls_are_detected(self, url):
        assert detect_platform(url) == "lever"

    @pytest.mark.parametrize("url", [
        "https://lever.co/acme/x",                      # the marketing site, not a job board
        "https://example.com/jobs.lever.co/acme/x",     # host only, not path
        "https://notlever.co/acme/x",
    ])
    def test_lookalikes_are_not(self, url):
        assert detect_platform(url) is None

    def test_the_dispatch_reaches_the_parser(self):
        result = parse_listing("lever", FULL_STACK, page(PAGES[FULL_STACK]))

        assert isinstance(result, ParsedListing) and result.job.title == "Full Stack Microsoft Developer"

    def test_the_other_platforms_are_still_detected(self):
        assert detect_platform("https://jobs.ashbyhq.com/acme/ac527b20-e9fc-41d1-841c-bc881e2abef6") == "ashbyhq"
        assert detect_platform("https://job-boards.greenhouse.io/2k/jobs/8035080003") == "greenhouse"
