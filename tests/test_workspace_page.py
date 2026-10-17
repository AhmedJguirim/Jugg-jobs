"""End-to-end checks for the section switch and the workspace page.

Nothing is submitted here: the form writes to the application database, and the
write path is covered against the rollback-isolated test database in
tests/unit/test_raw_job_listing.py.
"""

from __future__ import annotations

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
