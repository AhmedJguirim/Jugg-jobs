"""End-to-end checks for the section switch and the workspace page.

Nothing is submitted here: the form writes to the application database, and the
write path is covered against the rollback-isolated test database in
tests/unit/test_raw_job_listing.py.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_workspace_page_offers_both_ways_to_process(app_server: str, page: Page):
    page.goto(f"{app_server}/workspace")

    expect(page.get_by_role("heading", name="Raw listings")).to_be_visible()
    expect(page.get_by_role("link", name="Process existing listings")).to_be_visible()
    expect(page.get_by_role("link", name="Process other url")).to_be_visible()


def test_other_url_gives_the_blank_form(app_server: str, page: Page):
    page.goto(f"{app_server}/workspace")
    page.get_by_role("link", name="Process other url").click()

    expect(page.get_by_label("Link")).to_have_value("")
    expect(page.get_by_label("HTML body")).to_be_visible()
    expect(page.get_by_role("button", name="Save listing")).to_be_visible()


def test_existing_listings_prefills_the_link(app_server: str, page: Page):
    next_url = page.request.get(f"{app_server}/api/workspace/manual-job-listing-urls/next").json()["url"]
    page.goto(f"{app_server}/workspace/raw/existing")

    if next_url is None:
        expect(page.get_by_text("Every collected URL has its HTML stored")).to_be_visible()
    else:
        expect(page.get_by_label("Link")).to_have_value(next_url)


def test_switch_moves_between_sections(app_server: str, page: Page):
    page.goto(f"{app_server}/board")
    switch = page.get_by_role("switch")
    expect(switch).to_have_attribute("aria-checked", "false")

    switch.click()
    page.wait_for_url("**/workspace")
    expect(page.get_by_role("switch")).to_have_attribute("aria-checked", "true")

    page.get_by_role("switch").click()
    page.wait_for_url("**/board")


def test_each_section_has_its_own_navigation(app_server: str, page: Page):
    page.goto(f"{app_server}/companies")
    nav = page.get_by_role("navigation", name="Section navigation")
    expect(nav.get_by_role("link", name="Employers")).to_have_attribute("aria-current", "page")
    expect(nav.get_by_role("link", name="Raw listings")).to_have_count(0)

    page.goto(f"{app_server}/workspace")
    nav = page.get_by_role("navigation", name="Section navigation")
    expect(nav.get_by_role("link", name="Raw listings")).to_have_attribute("aria-current", "page")
    expect(nav.get_by_role("link", name="Employers")).to_have_count(0)


def test_jobs_test_lists_rows_and_opens_a_drawer(app_server: str, page: Page):
    items = page.request.get(f"{app_server}/api/jobs-test").json()["items"]
    if not items:
        pytest.skip("jobs_test is empty")
    first = items[0]

    page.goto(f"{app_server}/jobs-test")
    page.locator(f"li[data-job-id='{first['id']}']").click()

    drawer = page.get_by_label("Job details")
    expect(drawer.get_by_role("heading", name=first["title"])).to_be_visible()
    expect(drawer.get_by_role("link", name="Open vacancy")).to_have_attribute("href", first["url"])
    expect(drawer.get_by_role("heading", name="Description")).to_be_visible()
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()


def test_jobs_test_is_in_the_workspace_navigation(app_server: str, page: Page):
    page.goto(f"{app_server}/jobs-test")
    nav = page.get_by_role("navigation", name="Section navigation")

    expect(nav.get_by_role("link", name="Jobs test")).to_have_attribute("aria-current", "page")


def test_listing_urls_hides_processed_links(app_server: str, page: Page):
    items = page.request.get(f"{app_server}/api/workspace/manual-job-listing-urls").json()["items"]
    if not items:
        pytest.skip("no collected urls")

    page.goto(f"{app_server}/workspace/urls")
    expect(page.get_by_role("heading", name="Listing URLs")).to_be_visible()
    page.wait_for_load_state("networkidle")
    for item in items[:10]:
        row = page.locator("tr", has_text=item["url"])
        if item["processed"]:
            expect(row).to_have_count(0)
        else:
            expect(row.get_by_role("button", name="Process")).to_be_enabled()


def test_form_page_lists_only_waiting_listings_and_offers_bulk_grading(app_server: str, page: Page):
    waiting = page.request.get(f"{app_server}/api/workspace/raw-job-listings?waiting=true&limit=200").json()

    page.goto(f"{app_server}/workspace/raw/other")

    expect(page.get_by_role("region", name="Bulk grading")).to_be_visible()
    expect(page.get_by_role("spinbutton", name="How many listings")).to_have_value("10")
    section = page.get_by_role("region", name="Waiting to be graded")
    expect(section.get_by_role("heading")).to_contain_text(f"({waiting['total']})")
    for item in waiting["items"][:5]:
        row = section.locator(f"li[data-listing-id='{item['id']}']")
        # "Retry" instead of "Grade" for a page that could not be read last time.
        expect(row.get_by_role("button", name=re.compile(r"^(Grade|Retry)$"))).to_be_enabled()
    for graded in page.request.get(f"{app_server}/api/workspace/raw-job-listings?limit=50").json()["items"]:
        if graded["status"] == "graded":
            expect(section.locator(f"li[data-listing-id='{graded['id']}']")).to_have_count(0)


def test_form_offers_remove_next_to_skip(app_server: str, page: Page):
    """Nothing is clicked: removing a link changes the real data."""
    nxt = page.request.get(f"{app_server}/api/workspace/manual-job-listing-urls/next").json()
    if nxt["url"] is None:
        pytest.skip("no collected url is waiting")

    page.goto(f"{app_server}/workspace/raw/existing")

    expect(page.get_by_role("button", name="Skip this link")).to_be_visible()
    expect(page.get_by_role("button", name="Remove this link")).to_be_enabled()


def test_failed_rows_offer_remove_and_retry_but_healthy_rows_only_grade(app_server: str, page: Page):
    waiting = page.request.get(f"{app_server}/api/workspace/raw-job-listings?waiting=true&limit=200").json()["items"]
    if not waiting:
        pytest.skip("nothing is waiting")

    page.goto(f"{app_server}/workspace/raw/other")
    section = page.get_by_role("region", name="Waiting to be graded")
    expect(section.locator("li").first).to_be_visible()
    for item in waiting[:10]:
        row = section.locator(f"li[data-listing-id='{item['id']}']")
        if item["status"] == "unparseable":
            expect(row.get_by_role("button", name="Remove link")).to_be_visible()
            expect(row.get_by_role("button", name="Retry")).to_be_visible()
        else:
            expect(row.get_by_role("button", name="Remove link")).to_be_hidden()
            expect(row.get_by_role("button", name="Grade", exact=True)).to_be_visible()
