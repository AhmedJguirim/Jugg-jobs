"""The Greenhouse parser, run against three pages copied from the live site
(tests/fixtures/greenhouse). It is not hooked into the real flow yet."""

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
from app.parsers.greenhouse import _location, parse
from app.text.dom import parse_html

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "greenhouse"

TWO_K = "https://job-boards.greenhouse.io/2k/jobs/8035080003"
AFFIRM = "https://job-boards.greenhouse.io/affirm/jobs/8006331003"
GRAFANA = "https://job-boards.greenhouse.io/grafanalabs/jobs/6111473004-98"


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def job(url: str, name: str):
    result = parse(url, page(name))
    assert isinstance(result, ParsedListing)
    return result.job


@pytest.fixture
def two_k():
    return job(TWO_K, "2k_lead_backend_engineer.html")


@pytest.fixture
def affirm():
    return job(AFFIRM, "affirm_software_engineer_ii.html")


@pytest.fixture
def grafana():
    return job(GRAFANA, "grafana_senior_backend_engineer.html")


class TestTwoK:
    def test_identity_and_title(self, two_k):
        assert (two_k.source, two_k.external_id) == ("greenhouse", "8035080003")
        assert two_k.title == "Lead Backend Engineer" and two_k.job_titles == ["Lead Backend Engineer"]

    def test_company_comes_from_the_alert_box(self, two_k):
        assert two_k.company_name == "2K"

    def test_a_postal_location_becomes_a_place(self, two_k):
        assert (two_k.location.city, two_k.location.province, two_k.location.country_name) == (
            "San Mateo", "California", "United States")

    def test_the_pay_range_is_read(self, two_k):
        assert (two_k.salary_min, two_k.salary_max, two_k.salary_type) == (169100, 250280, "yearly")
        assert two_k.raw["compensation"]["currency"] == "USD"
        assert two_k.raw["compensation"]["currency_source"] == "symbol"

    def test_links(self, two_k):
        assert two_k.detail_url == two_k.apply_url == TWO_K

    def test_the_tag_is_kept_in_raw(self, two_k):
        assert two_k.raw["tags"] == ["New"] and two_k.raw["board"] == "2k"


class TestAffirm:
    def test_equity_amounts_are_not_a_salary_but_the_real_pay_range_is(self, affirm):
        # The page has "New hire equity: $24,000 - $36,000 USD" AND a base pay grade in zloty.
        assert (affirm.salary_min, affirm.salary_max) == (259000, 379000)
        assert affirm.raw["compensation"]["currency"] == "PLN"

    def test_remote_text_is_not_a_city(self, affirm):
        assert affirm.location is None
        assert affirm.raw["sections"] == {"Location": "Remote Poland"}

    def test_company(self, affirm):
        assert affirm.company_name == "Affirm" and affirm.title == "Software Engineer II, Backend (Card)"


class TestGrafana:
    def test_id_with_a_suffix_is_kept_whole(self, grafana):
        assert grafana.external_id == "6111473004-98" and grafana.detail_url == GRAFANA

    def test_range_with_a_currency_code_and_a_space(self, grafana):
        assert (grafana.salary_min, grafana.salary_max, grafana.salary_type) == (164490, 197389, "yearly")
        assert grafana.raw["compensation"]["currency"] == "CAD"
        assert grafana.raw["compensation"]["currency_source"] == "code"

    def test_company_and_remote_location(self, grafana):
        assert grafana.company_name == "Grafana Labs"
        assert grafana.location is None and grafana.raw["sections"]["Location"] == "Canada (Remote)"


class TestDescription:
    @pytest.mark.parametrize("url, name", [
        (TWO_K, "2k_lead_backend_engineer.html"),
        (AFFIRM, "affirm_software_engineer_ii.html"),
        (GRAFANA, "grafana_senior_backend_engineer.html"),
    ])
    def test_every_word_of_the_page_is_kept_and_nothing_else(self, url, name):
        source = parse_html(page(name)).find(cls="job__description").text().split()
        stored = parse_html(job(url, name).description).text().split()

        assert stored == source

    def test_only_formatting_survives(self, grafana):
        assert "<script" not in grafana.description and "style=" not in grafana.description
        assert "data-" not in grafana.description and "class=" not in grafana.description

    def test_links_in_the_description_keep_their_href(self, grafana):
        assert 'href="https://grafana.com/legal/applicant-and-candidate-privacy-policy/"' in grafana.description


def listing_html(title="Backend Engineer", location="Berlin, Germany", body="<p>Build things.</p>"):
    return (
        f'<div><div class="job__header"><div class="job__title"><h1 class="section-header">{title}</h1>'
        f'<div class="job__location"><div>{location}</div></div></div></div>'
        f'<div class="job__description body">{body}</div>'
        '<div class="job-alert"><p>Interested in building your career at Acme Corp? Get future opportunities.</p></div></div>'
    )


class TestPay:
    def parse(self, body):
        return parse("https://job-boards.greenhouse.io/acme/jobs/123", listing_html(body=body)).job

    def test_equity_only_is_no_salary(self):
        job = self.parse("<p>New hire equity: $24,000 - $36,000 USD. Annual refresh: $7,500.</p>")

        assert (job.salary_min, job.salary_max, job.salary_type) == (None, None, None)
        assert job.raw["compensation"] is None

    def test_a_bonus_or_stipend_is_not_salary(self):
        job = self.parse("<p>The annual bonus is up to $20,000. A home office stipend of $1,500 is provided.</p>")

        assert job.salary_min is None

    def test_a_pay_sentence_with_a_euro_range(self):
        job = self.parse("<p>The salary range is €55,000 - €72,000 per year.</p>")

        assert (job.salary_min, job.salary_max, job.salary_type) == (55000, 72000, "yearly")
        assert job.raw["compensation"]["currency"] == "EUR"

    def test_an_hourly_rate(self):
        job = self.parse("<p>The pay range for this role is $45 - $60 per hour.</p>")

        assert (job.salary_min, job.salary_max, job.salary_type) == (45, 60, "hourly")

    def test_the_first_range_wins_when_there_are_several(self):
        job = self.parse(
            "<p>Pay range in New York: $150,000 - $180,000. Pay range in Texas: $120,000 - $150,000.</p>")

        assert (job.salary_min, job.salary_max) == (150000, 180000)

    def test_no_pay_information(self):
        assert self.parse("<p>We pay well and offer great benefits.</p>").salary_min is None


class TestLocation:
    @pytest.mark.parametrize("text, expected", [
        ("San Mateo, California, United States", ("San Mateo", "California", "United States")),
        ("Berlin, Germany", ("Berlin", None, "Germany")),
        ("Austin, TX", ("Austin", "TX", None)),
        ("New York, New York, United States", ("New York", "New York", "United States")),
    ])
    def test_places(self, text, expected):
        place = _location(text)

        assert (place.city, place.province, place.country_name) == expected

    @pytest.mark.parametrize("text", [
        "Remote Poland", "Canada (Remote)", "Remote", "Hybrid - London", "Multiple locations",
        "Anywhere in the world", "London", "", "Worldwide, Remote",
    ])
    def test_not_places(self, text):
        assert _location(text) is None


class TestUrl:
    @pytest.mark.parametrize("url, board, job_id", [
        ("https://boards.greenhouse.io/acme/jobs/4567890", "acme", "4567890"),
        ("https://job-boards.eu.greenhouse.io/acme/jobs/4567890?gh_src=abc", "acme", "4567890"),
        (" https://job-boards.greenhouse.io/acme/jobs/123-45 ", "acme", "123-45"),
    ])
    def test_board_and_id(self, url, board, job_id):
        result = parse(url, listing_html()).job

        assert result.external_id == job_id and result.raw["board"] == board
        assert "?" not in result.detail_url  # tracking parameters are dropped

    @pytest.mark.parametrize("url", [
        "https://job-boards.greenhouse.io/acme",              # a board, not a job
        "https://job-boards.greenhouse.io/acme/jobs/",
        "https://job-boards.greenhouse.io/acme/jobs/not-a-number",
        "https://job-boards.greenhouse.io/acme/other/123",
    ])
    def test_a_url_without_a_job_id(self, url):
        with pytest.raises(ListingParseError):
            parse(url, listing_html())


class TestNotFoundAndUnreadable:
    URL = "https://job-boards.greenhouse.io/acme/jobs/123"

    def test_a_no_longer_open_page(self):
        html = '<div><p class="body">The job you are looking for is no longer open.</p></div>'

        assert isinstance(parse(self.URL, html), ListingNotFound)

    def test_a_redirect_with_an_error_flag(self):
        assert isinstance(parse(self.URL + "?error=true", "<div></div>"), ListingNotFound)

    def test_a_real_job_that_says_no_longer_open_is_still_a_job(self):
        html = listing_html(body="<p>Our previous office is no longer open. Join us.</p>")

        assert isinstance(parse(self.URL, html), ParsedListing)

    @pytest.mark.parametrize("html", ["", "<html><body><h2>Welcome</h2></body></html>"])
    def test_other_pages_are_unreadable(self, html):
        with pytest.raises(ListingParseError):
            parse(self.URL, html)

    def test_a_missing_company_stays_empty(self):
        html = listing_html().replace("Interested in building your career at Acme Corp?", "Get alerts")

        assert parse(self.URL, html).job.company_name is None


class TestNotHookedYet:
    """Greenhouse is not in the platform registry, so no listing reaches this parser
    through the real flow. When it is hooked, replace this class with detection tests."""

    def test_the_platform_is_not_detected(self):
        assert detect_platform(TWO_K) is None

    def test_the_registry_has_no_greenhouse_parser(self):
        with pytest.raises(ListingParseError):
            parse_listing("greenhouse", TWO_K, page("2k_lead_backend_engineer.html"))
