import re

import pytest
from playwright.sync_api import Page, expect

# Whole module is end-to-end: deselect with `-m "not e2e"`.
pytestmark = pytest.mark.e2e


def test_homepage_loads(app_server: str, page: Page):
    page.goto(app_server)
    expect(page).to_have_title(re.compile("DBOS"))
    expect(page.get_by_role("heading", name=re.compile("Build reliable software"))).to_be_visible()


def test_tabs_switch(app_server: str, page: Page):
    page.goto(app_server)
    for tab in ["Schedules", "Queues", "Communication", "Conductor"]:
        page.get_by_role("button", name=tab, exact=True).click()
        expect(page.locator(f"#tab-{tab.lower()}")).to_have_class(re.compile(r"\bactive\b"))


def test_schedule_status_endpoint(app_server: str, page: Page):
    response = page.request.get(f"{app_server}/schedule/status")
    assert response.ok
    body = response.json()
    assert {"cron", "schedule_status", "workflow_counts"} <= body.keys()


def test_workflow_demo_completes_first_step(app_server: str, page: Page):
    page.goto(app_server)
    page.get_by_role("button", name="Launch durable workflow").click()
    # step_one sleeps 5s, so give the first row room to flip out of "pending"
    expect(page.locator("#step-row-1")).not_to_have_class(re.compile(r"\bpending\b"), timeout=20_000)
