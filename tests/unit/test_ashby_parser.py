"""The Ashby parser, run against pages copied from the live site (tests/fixtures/ashbyhq)."""

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
from app.parsers.ashbyhq import parse, parse_compensation

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "ashbyhq"

VULTR = "https://jobs.ashbyhq.com/vultr/79d7e2d5-f159-417e-9498-2a3fc11cdbf0"
DOCPLANNER = "https://jobs.ashbyhq.com/docplanner/50abe173-4ad4-4662-9d74-c7b68d9e17bd"
LENDABLE = "https://jobs.ashbyhq.com/lendable/ac527b20-e9fc-41d1-841c-bc881e2abef6"


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestPlatformDetection:
    @pytest.mark.parametrize("url", [DOCPLANNER, "http://jobs.ashbyhq.com/x/y", "  " + LENDABLE + "  "])
    def test_ashby_urls(self, url):
        assert detect_platform(url) == "ashbyhq"

    @pytest.mark.parametrize(
        "url",
        [
            "https://apply.workable.com/acme/j/1",
            "https://example.com/jobs.ashbyhq.com/x",  # host only, not path
            "https://notashbyhq.com/x/y",
            "not a url",
            "",
        ],
    )
    def test_everything_else_is_unsupported(self, url):
        assert detect_platform(url) is None


class TestNotFound:
    def test_removed_posting_is_detected(self):
        result = parse(VULTR, page("not_found_vultr.html"))

        assert isinstance(result, ListingNotFound)
        assert result.platform == "ashbyhq"

    def test_is_checked_before_anything_else_is_read(self):
        """No job heading and no sections - and still a result, not an error."""
        html = '<div id="root"><h1>Job not found</h1><p>The job you requested was not found.</p></div>'

        assert isinstance(parse(VULTR, html), ListingNotFound)

    def test_detected_by_markup_even_if_the_wording_changes(self):
        html = '<div id="root"><div class="_content_dea4p_70 _notFoundContent_b9ljl_6">gone</div></div>'

        assert isinstance(parse(VULTR, html), ListingNotFound)

    def test_goes_through_the_platform_dispatch(self):
        assert isinstance(parse_listing("ashbyhq", VULTR, page("not_found_vultr.html")), ListingNotFound)


class TestLendable:
    @pytest.fixture
    def job(self):
        result = parse(LENDABLE, page("lendable_php_engineer.html"))
        assert isinstance(result, ParsedListing)
        return result.job

    def test_identity(self, job):
        assert job.source == "ashbyhq"
        assert job.external_id == "ac527b20-e9fc-41d1-841c-bc881e2abef6"

    def test_title(self, job):
        assert job.title == "PHP Engineer"
        assert job.job_titles == ["PHP Engineer"]

    def test_company_comes_from_the_header(self, job):
        assert job.company_name == "Lendable"
        assert job.company_website == "https://www.lendable.co.uk/"

    def test_links(self, job):
        assert job.detail_url == LENDABLE
        assert job.apply_url == LENDABLE + "/application"

    def test_description_is_clean_html(self, job):
        assert job.description.startswith("<h2>About Lendable</h2>")
        assert "PHP 8.0+ is a must" in job.description
        assert "style=" not in job.description
        assert "<p></p>" not in job.description
        assert "Apply for this Job" not in job.description

    def test_links_in_the_description_keep_their_href(self, job):
        assert 'href="https://blog.lendable.co.uk/"' in job.description

    def test_remote_location_text_is_not_turned_into_a_city(self, job):
        assert job.location is None
        assert job.raw["sections"]["Location"] == "EU Remote"

    def test_unmapped_facts_are_kept_in_raw(self, job):
        assert job.raw["sections"] == {
            "Location": "EU Remote",
            "Employment Type": "Full time",
            "Location Type": "Remote",
            "Department": "Engineering & Product",
        }

    def test_nothing_is_invented(self, job):
        assert (job.salary_min, job.salary_max, job.salary_type) == (None, None, None)
        assert job.start_date is None and job.contact_email is None


class TestDocplanner:
    @pytest.fixture
    def job(self):
        result = parse(DOCPLANNER, page("docplanner_staff_engineer.html"))
        assert isinstance(result, ParsedListing)
        return result.job

    def test_title_and_company(self, job):
        assert job.title == "Staff Engineer (100% Remote within Poland)"
        assert job.company_name == "Docplanner"

    def test_postal_address_becomes_a_location(self, job):
        assert job.location.city == "Warsaw"
        # "Warsaw, Warsaw": a region repeating the city adds nothing.
        assert job.location.province is None

    def test_emoji_survive(self, job):
        assert "\U0001f44b" in job.description


class TestUnreadablePages:
    def test_empty_paste(self):
        with pytest.raises(ListingParseError):
            parse(LENDABLE, "")

    def test_some_other_page(self):
        with pytest.raises(ListingParseError):
            parse(LENDABLE, "<html><body><h1>Welcome</h1></body></html>")

    def test_url_without_a_job_id(self):
        with pytest.raises(ListingParseError):
            parse("https://jobs.ashbyhq.com/lendable", page("lendable_php_engineer.html"))

    def test_unknown_platform(self):
        with pytest.raises(ListingParseError):
            parse_listing("nope", LENDABLE, "<p/>")


class TestAddress:
    def test_city_and_region(self):
        html = (
            '<div class="ashby-job-posting-header"></div>'
            '<h1 class="ashby-job-posting-heading">Dev</h1>'
            '<div class="ashby-job-posting-left-pane">'
            '<div><h2>Address</h2><p>Utrecht, Utrecht Province</p></div></div>'
        )

        job = parse(LENDABLE, html).job

        assert (job.location.city, job.location.province) == ("Utrecht", "Utrecht Province")
        assert job.description is None and job.company_name is None


class TestCompensation:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("€55K – €72K • Offers Bonus", (55000, 72000, "yearly")),
            ("$120,000 - $150,000 per year", (120000, 150000, "yearly")),
            ("55.000 - 72.000 EUR", (55000, 72000, "yearly")),
            ("£45 - £60 per hour", (45, 60, "hourly")),
            ("€4,500 - €5,500 per month", (4500, 5500, "monthly")),
            ("€60K", (60000, 60000, "yearly")),
            ("$72K - $55K", (55000, 72000, "yearly")),
            ("Competitive salary", (None, None, None)),
            ("", (None, None, None)),
        ],
    )
    def test_ranges(self, text, expected):
        assert parse_compensation(text) == expected

    def test_a_compensation_section_fills_the_salary_columns(self):
        html = (
            '<h1 class="ashby-job-posting-heading">Dev</h1>'
            '<div class="ashby-job-posting-left-pane">'
            "<div><h2>Compensation</h2><p>€55K – €72K • Offers Bonus</p></div></div>"
        )

        job = parse(LENDABLE, html).job

        assert (job.salary_min, job.salary_max, job.salary_type) == (55000, 72000, "yearly")
        assert "Offers Bonus" in job.raw["sections"]["Compensation"]
