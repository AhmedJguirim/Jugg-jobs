"""The Workable parser, run against pages copied from the live site
(tests/fixtures/workable): two job pages and a company careers page, which is what
Workable shows when a job has been removed. It is not hooked into the real flow yet."""

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
from app.parsers.workable import parse
from app.text.dom import parse_html

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "workable"

DACODES = "https://apply.workable.com/dacodes-mx/j/5E3ED2DFA7"
PEARL = "https://apply.workable.com/j/928130F67F"  # a job URL with no company in it
GONE = "https://apply.workable.com/supportyourapp/j/1C3E8E25DA"

PAGES = {
    DACODES: "dacodes_machine_learning_engineer.html",
    PEARL: "pearl_remote_backend_engineer.html",
    GONE: "supportyourapp_careers_page_job_gone.html",
}


def page(url: str) -> str:
    return (FIXTURES / PAGES[url]).read_text(encoding="utf-8")


def job(url: str):
    result = parse(url, page(url))
    assert isinstance(result, ParsedListing)
    return result.job


class TestDaCodes:
    def test_identity_title_and_company(self):
        j = job(DACODES)

        assert (j.source, j.external_id) == ("workable", "5E3ED2DFA7")
        assert j.title == "Machine Learning Engineer (Semi-Senior / Senior)" and j.job_titles == [j.title]
        assert j.company_name == "DaCodes"

    def test_the_facts_under_the_title(self):
        sections = job(DACODES).raw["sections"]

        assert sections["Location Type"] == "Remote"
        assert sections["Department"] == "Talent Augmentation (Leads)"
        assert sections["Employment Type"] == "Full time"

    def test_several_locations_are_joined_and_are_not_a_place(self):
        j = job(DACODES)

        assert j.raw["sections"]["Location"] == "Buenos Aires, Buenos Aires, Argentina; Mexico; Colombia; Uruguay; Peru"
        assert j.location is None

    def test_links(self):
        j = job(DACODES)

        assert j.detail_url == DACODES
        assert j.apply_url == DACODES + "/apply/"
        assert j.raw["board"] == "dacodes-mx"

    def test_no_salary_is_stated(self):
        j = job(DACODES)

        assert (j.salary_min, j.salary_max, j.salary_type) == (None, None, None) and j.raw["compensation"] is None


class TestPearl:
    def test_company_and_board_come_from_the_logo_when_the_url_has_none(self):
        j = job(PEARL)

        assert j.company_name == "Pearl" and j.raw["board"] == "pearltalent"
        assert j.detail_url == "https://apply.workable.com/pearltalent/j/928130F67F"
        assert j.apply_url == "https://apply.workable.com/pearltalent/j/928130F67F/apply/"

    def test_the_job_code_is_kept(self):
        assert job(PEARL).raw["job_code"] == "A444"

    def test_accents_survive_in_locations(self):
        assert "Bogotá, Colombia" in job(PEARL).raw["sections"]["Location"]

    def test_competitive_salary_without_a_figure_is_not_a_salary(self):
        # "5+ years of backend engineering" sits near "Competitive Salary: Based on experience".
        j = job(PEARL)

        assert (j.salary_min, j.salary_max, j.salary_type) == (None, None, None)

    def test_a_funding_figure_is_not_a_salary(self):
        # "...$5B in aggregate and are backed by..." is in the description.
        assert job(PEARL).salary_min is None


class TestDescription:
    @pytest.mark.parametrize("url", [DACODES, PEARL])
    def test_every_word_of_the_posting_sections_is_kept_and_nothing_else(self, url):
        root = parse_html(page(url))
        blocks = [n for ui in ("job-description", "job-requirements", "job-benefits") for n in root.find_all_by_attr("data-ui", ui)]
        expected = " ".join(b.text() for b in blocks).split()

        assert parse_html(job(url).description).text().split() == expected

    @pytest.mark.parametrize("url", [DACODES, PEARL])
    def test_the_sections_keep_their_headings_and_only_formatting_survives(self, url):
        description = job(url).description

        for heading in ("Description", "Requirements", "Benefits"):
            assert f">{heading}</h2>" in description
        for unsafe in ("<script", "<iframe", "style=", "class=", "data-ui", "onclick", "javascript:"):
            assert unsafe not in description

    def test_the_apply_and_share_controls_are_not_in_the_description(self):
        description = job(PEARL).description

        assert "Apply for this job" not in description and "Share this job" not in description


class TestNotFound:
    def test_the_careers_page_workable_shows_for_a_removed_job(self):
        # The saved URL is the job's; only the HTML (and its "no longer available"
        # message) shows the job was gone.
        result = parse(GONE, page(GONE))

        assert isinstance(result, ListingNotFound) and result.platform == "workable"
        assert "no longer available" in result.reason

    def test_the_redirect_url_alone_is_enough(self):
        result = parse("https://apply.workable.com/supportyourapp/?not_found=true", "<div></div>")

        assert isinstance(result, ListingNotFound) and "not_found" in result.reason

    def test_a_job_url_marked_not_found_with_a_job_page_is_still_not_found(self):
        assert isinstance(parse(DACODES + "?not_found=true", page(DACODES)), ListingNotFound)

    def test_it_is_judged_before_the_url_is_read(self):
        # No job id at all in this URL, and still a result rather than an error.
        assert isinstance(parse("https://apply.workable.com/acme/", page(GONE)), ListingNotFound)

    def test_a_careers_page_without_the_message_is_the_wrong_page_not_a_removal(self):
        html = page(GONE).replace("This job is no longer available.", "").replace("no longer available", "")

        with pytest.raises(ListingParseError, match="careers page"):
            parse(GONE, html)

    def test_the_list_cards_titles_are_not_mistaken_for_the_jobs_title(self):
        # The careers page marks every card in its jobs list `data-ui=job-title` too.
        with pytest.raises(ListingParseError):
            parse(GONE, "<div data-ui='careers-page-content'><h3 data-ui='job-title'>Some job</h3></div>")


def listing_html(title="Backend Engineer", body="<p>Build things.</p>", locations=("Berlin, Germany",), extra_sections=""):
    # A location is one string, or a tuple of the spans Workable splits it into.
    def spans(loc):
        return "".join(f"<span>{part}</span>" for part in ((loc,) if isinstance(loc, str) else loc))

    tooltips = "".join(f'<div data-ui="job-location-tooltip">{spans(loc)}</div>' for loc in locations)
    return (
        '<div data-ui="careers-page-content"><header>'
        '<a data-ui="company-logo" href="/acme/"><img alt="Acme"></a>'
        f'<h1 data-ui="job-title">{title}</h1>'
        '<p><span data-ui="job-workplace"><strong>On-site</strong></span><span data-ui="job-department">Eng</span>'
        '<span data-ui="job-type">Full time</span></p>'
        f'<div data-ui="job-location">{tooltips}</div></header>'
        f'<section data-ui="job-description"><h2>Description</h2><div>{body}</div></section>{extra_sections}'
        '<a data-ui="apply-button" href="/acme/j/ABCDEF1234/apply/">Apply</a></div>'
    )


URL = "https://apply.workable.com/acme/j/ABCDEF1234"


class TestPay:
    def test_a_salary_sentence(self):
        j = parse(URL, listing_html(body="<p>The salary range is €60,000 - €80,000 per year.</p>")).job

        assert (j.salary_min, j.salary_max, j.salary_type) == (60000, 80000, "yearly")
        assert j.raw["compensation"]["currency"] == "EUR"

    def test_a_bullet_list_without_full_stops_is_read_one_item_at_a_time(self):
        body = "<ul><li>5+ years of experience</li><li>Hourly work</li><li>Competitive salary: based on experience</li></ul>"

        assert parse(URL, listing_html(body=body)).job.salary_min is None

    def test_a_label_and_its_value_in_separate_blocks(self):
        body = "<p>Base Pay Grade</p><p>$90,000 - $110,000</p>"

        j = parse(URL, listing_html(body=body)).job

        assert (j.salary_min, j.salary_max) == (90000, 110000)

    def test_a_small_bare_number_is_never_pay(self):
        body = "<p>The hourly rate is negotiable, around 5 to 10 depending on the project.</p>"

        assert parse(URL, listing_html(body=body)).job.salary_min is None

    def test_a_pay_range_in_the_benefits_section(self):
        extra = '<section data-ui="job-benefits"><h2>Benefits</h2><ul><li>Salary range: $70,000 - $85,000 per year</li></ul></section>'

        j = parse(URL, listing_html(extra_sections=extra)).job

        assert (j.salary_min, j.salary_max) == (70000, 85000)


class TestLocation:
    def test_a_single_postal_location_becomes_a_place(self):
        j = parse(URL, listing_html(locations=("Berlin, Germany",))).job

        assert (j.location.city, j.location.country_name) == ("Berlin", "Germany")

    def test_trailing_commas_between_the_spans_are_cleaned(self):
        j = parse(URL, listing_html(locations=(("Mexico City, ", "Mexico"),))).job

        assert j.raw["sections"]["Location"] == "Mexico City, Mexico"

    def test_duplicates_are_listed_once(self):
        j = parse(URL, listing_html(locations=("Lima, Peru", "Lima, Peru", "Bogota, Colombia"))).job

        assert j.raw["sections"]["Location"] == "Lima, Peru; Bogota, Colombia" and j.location is None

    def test_no_location(self):
        j = parse(URL, listing_html(locations=())).job

        assert "Location" not in j.raw["sections"] and j.location is None


class TestUrl:
    @pytest.mark.parametrize("url, slug, job_id", [
        ("https://apply.workable.com/acme/j/ABCDEF1234", "acme", "ABCDEF1234"),
        ("https://apply.workable.com/acme/j/ABCDEF1234/apply/", "acme", "ABCDEF1234"),
        ("https://apply.workable.com/acme/j/ABCDEF1234?utm_source=x", "acme", "ABCDEF1234"),
        ("https://apply.workable.com/j/ABCDEF1234", None, "ABCDEF1234"),
        ("  https://apply.workable.com/acme/j/abcdef1234  ", "acme", "abcdef1234"),
    ])
    def test_company_and_job_id(self, url, slug, job_id):
        j = parse(url, listing_html()).job

        assert j.external_id == job_id
        assert j.detail_url.endswith(f"/j/{job_id}")  # no query, no /apply

    @pytest.mark.parametrize("url", [
        "https://apply.workable.com/acme/",
        "https://apply.workable.com/acme/j/",
        "https://apply.workable.com/acme/j/short",
        "https://apply.workable.com/a/b/j/ABCDEF1234",
    ])
    def test_a_url_without_a_job_id(self, url):
        with pytest.raises(ListingParseError):
            parse(url, listing_html())

    @pytest.mark.parametrize("html", ["", "<html><body><h1>Welcome</h1></body></html>"])
    def test_other_pages_are_unreadable(self, html):
        with pytest.raises(ListingParseError):
            parse(URL, html)


class TestNotHookedYet:
    """Workable is not in the platform registry, so no listing reaches this parser
    through the real flow. When it is hooked, replace this class with detection tests."""

    def test_the_platform_is_not_detected(self):
        assert detect_platform(DACODES) is None

    def test_the_registry_has_no_workable_parser(self):
        with pytest.raises(ListingParseError):
            parse_listing("workable", DACODES, page(DACODES))
