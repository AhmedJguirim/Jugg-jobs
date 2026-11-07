"""End-to-end checks for the navigation and the workspace pages.

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


def test_the_navigation_marks_the_current_page(app_server: str, page: Page):
    for path, name in (("/workspace/urls", "Listing URLs"), ("/workspace", "Raw listings"), ("/board", "Job board")):
        page.goto(f"{app_server}{path}")
        nav = page.get_by_role("navigation", name="Main navigation")
        expect(nav.get_by_role("link", name=name)).to_have_attribute("aria-current", "page")
        expect(nav.get_by_role("link")).to_have_count(3)


def test_the_form_pages_belong_to_raw_listings(app_server: str, page: Page):
    page.goto(f"{app_server}/workspace/raw/other")
    nav = page.get_by_role("navigation", name="Main navigation")

    expect(nav.get_by_role("link", name="Raw listings")).to_have_attribute("aria-current", "page")


def test_the_root_opens_the_board(app_server: str, page: Page):
    page.goto(f"{app_server}/")

    page.wait_for_url("**/board")


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


def test_cleanup_button_reflects_what_there_is_to_clean(app_server: str, page: Page):
    """Nothing is clicked: cleaning up empties real data."""
    stats = page.request.get(f"{app_server}/api/workspace/raw-job-listings-cleanup").json()

    page.goto(f"{app_server}/workspace/raw/other")

    section = page.get_by_role("region", name="Cleanup")
    button = section.get_by_role("button", name="Cleanup graded raw listings")
    expect(button).to_be_visible()
    if stats["listings"]:
        expect(button).to_be_enabled()
        expect(section).to_contain_text(f"{stats['listings']} graded listing(s)")
    else:
        expect(button).to_be_disabled()


def _mock_urls_api(page: Page, fetchable: int, statuses: list[dict]):
    """Stand in for the server: the list, starting a run, and its progress. Nothing
    is fetched or written - "Process all" does both."""
    polled = {"n": 0}

    def handle(route):
        request = route.request
        path = request.url.split("?")[0]
        if path.endswith("/manual-job-listing-urls"):
            route.fulfill(json={"total": fetchable, "fetchable": fetchable, "items": []})
        elif path.endswith("/process-all") and request.method == "POST":
            route.fulfill(status=202, json={"workflow_id": "wf-1", "total": 3})
        elif path.endswith("/process-all"):
            route.fulfill(json={"workflow_id": None})
        elif path.endswith("/process-all/wf-1"):
            body = statuses[min(polled["n"], len(statuses) - 1)]
            polled["n"] += 1
            route.fulfill(json={"workflow_id": "wf-1", **body})
        else:
            route.continue_()

    page.route(re.compile(r".*/api/workspace/manual-job-listing-urls.*"), handle)


def test_process_all_is_disabled_when_nothing_can_be_fetched(app_server: str, page: Page):
    _mock_urls_api(page, 0, [])

    page.goto(f"{app_server}/workspace/urls")

    expect(page.locator("#process-all")).to_be_disabled()
    expect(page.locator("#process-all")).to_contain_text("Process all (0)")


def test_process_all_shows_its_progress_and_what_it_did(app_server: str, page: Page):
    running = {"state": "running", "progress": {
        "total": 3, "done": 1, "added": 1, "gone": 0, "retry": 0, "exists": 0,
        "current": "https://jobs.lever.co/acme/2", "problems": [],
    }}
    done = {"state": "done", "progress": {
        "total": 3, "done": 3, "added": 2, "gone": 0, "retry": 1, "exists": 0, "current": None,
        "problems": [{"url": "https://jobs.lever.co/acme/3", "status": "blocked", "note": "HTTP 429"}],
    }}
    _mock_urls_api(page, 3, [running, done])

    page.goto(f"{app_server}/workspace/urls")
    button = page.locator("#process-all")
    expect(button).to_contain_text("Process all (3)")
    expect(button).to_be_enabled()
    button.click()

    progress = page.locator("#process-progress")
    expect(progress).to_contain_text("1 of 3 done")
    expect(progress.get_by_role("button", name="Cancel")).to_be_visible()
    expect(progress).to_contain_text("https://jobs.lever.co/acme/2")
    expect(button).to_be_disabled()

    expect(progress).to_contain_text("Finished", timeout=8000)
    expect(progress).to_contain_text("2 added to the waiting list")
    expect(progress.get_by_role("button", name="Cancel")).to_be_hidden()
    expect(progress.get_by_role("link", name=re.compile("Open the waiting list"))).to_be_visible()
    expect(progress).to_contain_text("HTTP 429")
