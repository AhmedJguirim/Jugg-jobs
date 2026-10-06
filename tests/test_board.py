"""End-to-end tests for the job board view.

These drive the real page against the real database, so they check the thing the
user actually looks at: that enriched vacancies render, that the filters do what
they claim, and that the detail drawer shows the English translation.

Marked `e2e`; deselect with -m "not e2e".
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


@pytest.fixture
def board(app_server: str, page: Page) -> Page:
    page.goto(f"{app_server}/board")
    # Wait for the cards themselves, not just the stat tiles. The board sends
    # every vacancy in one payload and renders them client-side, so as the
    # corpus grows the tiles appear well before the list does - waiting on the
    # tiles alone made tests that count cards flaky.
    expect(page.locator("ul > li").first).to_be_visible(timeout=30_000)
    return page


class TestApi:
    def test_board_endpoint_returns_jobs_and_stats(self, app_server: str, page: Page):
        response = page.request.get(f"{app_server}/api/board")

        assert response.ok
        body = response.json()
        assert {"jobs", "stats", "facets", "profile"} <= body.keys()

    def test_stats_carry_the_headline_numbers(self, app_server: str, page: Page):
        stats = page.request.get(f"{app_server}/api/board").json()["stats"]

        for key in ("total_jobs", "actionable", "blocked", "needs_dutch", "gpu_seconds_spent"):
            assert key in stats

    def test_list_rows_omit_the_long_text(self, app_server: str, page: Page):
        """The list payload stays light; bodies come from the detail endpoint."""
        jobs = page.request.get(f"{app_server}/api/board").json()["jobs"]
        if not jobs:
            pytest.skip("no vacancies ingested")

        assert "description_en" not in jobs[0]

    def test_detail_endpoint_includes_both_texts(self, app_server: str, page: Page):
        jobs = page.request.get(f"{app_server}/api/board").json()["jobs"]
        if not jobs:
            pytest.skip("no vacancies ingested")

        detail = page.request.get(f"{app_server}/api/jobs/{jobs[0]['id']}").json()

        assert "description_en" in detail
        assert "description_original" in detail

    def test_unknown_job_is_a_404(self, app_server: str, page: Page):
        assert page.request.get(f"{app_server}/api/jobs/99999999").status == 404


class TestPage:
    def test_page_loads_with_a_title(self, board: Page):
        expect(board).to_have_title("Job board - job-hunt")

    def test_heading_is_visible(self, board: Page):
        expect(board.get_by_role("heading", name="Job board")).to_be_visible()

    def test_stat_tiles_render(self, board: Page):
        expect(board.locator("text=worth a look")).to_be_visible()
        expect(board.locator("text=GPU seconds")).to_be_visible()

    def test_vacancies_are_listed(self, board: Page):
        count = board.locator("ul > li").count()

        assert count > 0, "expected at least one vacancy card"

    def test_no_raw_alpine_markup_leaks(self, board: Page):
        """x-cloak must hide the template until Alpine has rendered it."""
        body = board.locator("body").inner_text()

        assert "x-text" not in body
        assert "${" not in body


class TestFilters:
    def test_search_narrows_the_list(self, board: Page):
        cards = board.locator("ul > li")
        expect(cards).not_to_have_count(0)

        board.get_by_placeholder("title, company, summary…").fill("zzzzzznotathing")

        # The input is debounced, so assert with polling rather than immediately.
        expect(cards).to_have_count(0)

    def test_no_match_shows_the_empty_state(self, board: Page):
        board.get_by_placeholder("title, company, summary…").fill("zzzzzznotathing")

        expect(board.locator("text=No vacancy matches these filters")).to_be_visible()

    def test_reset_restores_the_list(self, board: Page):
        cards = board.locator("ul > li")
        before = cards.count()
        board.get_by_placeholder("title, company, summary…").fill("zzzzzznotathing")
        expect(cards).to_have_count(0)

        board.get_by_role("button", name="Reset them").click()

        expect(cards).to_have_count(before)

    def test_hiding_blocked_reduces_the_count(self, board: Page):
        """Most of this corpus is blocked, so unticking the box must cut the list."""
        cards = board.locator("ul > li")
        before = cards.count()

        board.locator('input[type="checkbox"]').first.uncheck()

        expect(cards).not_to_have_count(before)

    def test_raising_the_minimum_score_reduces_the_list(self, app_server: str, board: Page):
        # Wait for the full list to render before counting: reading the count
        # mid-render made this flaky once the corpus grew past 100 vacancies.
        # The default view hides applied and archived vacancies.
        jobs = board.request.get(f"{app_server}/api/board").json()["jobs"]
        total = sum(1 for job in jobs if not job["applied_at"] and not job["archived_at"])
        cards = board.locator("ul > li")
        expect(cards).to_have_count(total)

        board.locator('input[type="range"]').fill("90")

        expect(cards).not_to_have_count(total)


class TestDetailDrawer:
    def test_clicking_a_vacancy_opens_the_drawer(self, board: Page):
        board.locator("ul > li").first.click()

        expect(board.get_by_role("button", name="Show Dutch original")).to_be_visible()

    def test_drawer_shows_the_english_description(self, board: Page):
        board.locator("ul > li").first.click()

        expect(board.locator("text=Description (English)")).to_be_visible()

    def test_can_switch_to_the_dutch_original(self, board: Page):
        board.locator("ul > li").first.click()

        board.get_by_role("button", name="Show Dutch original").click()

        expect(board.locator("text=as the model read it")).to_be_visible()

    def test_escape_closes_the_drawer(self, board: Page):
        board.locator("ul > li").first.click()
        expect(board.get_by_role("button", name="Show Dutch original")).to_be_visible()

        board.keyboard.press("Escape")

        expect(board.get_by_role("button", name="Show Dutch original")).not_to_be_visible()

    def test_drawer_reports_provenance(self, board: Page):
        """Which model produced this, and what it cost, stays visible."""
        board.locator("ul > li").first.click()

        expect(board.locator("text=How this was produced")).to_be_visible(timeout=10_000)


@pytest.fixture
def unmarked_job(app_server: str, page: Page):
    """An unmarked vacancy from the real corpus, restored to unmarked afterwards.

    These tests write to the application database, so the finaliser puts the
    marks back even when an assertion fails.
    """
    jobs = page.request.get(f"{app_server}/api/board").json()["jobs"]
    job = next((j for j in jobs if not j["applied_at"] and not j["archived_at"]), None)
    if job is None:
        pytest.skip("no unmarked vacancy to use")
    yield job
    page.request.patch(
        f"{app_server}/api/jobs/{job['id']}/marks", data={"applied": False, "archived": False}
    )


class TestMarks:
    def test_marking_applied_sets_a_timestamp(self, app_server: str, page: Page, unmarked_job):
        response = page.request.patch(
            f"{app_server}/api/jobs/{unmarked_job['id']}/marks", data={"applied": True}
        )

        assert response.ok
        assert response.json()["applied_at"] is not None
        assert response.json()["archived_at"] is None

    def test_mark_shows_up_in_the_board_payload(self, app_server: str, page: Page, unmarked_job):
        page.request.patch(f"{app_server}/api/jobs/{unmarked_job['id']}/marks", data={"archived": True})

        jobs = page.request.get(f"{app_server}/api/board").json()["jobs"]
        row = next(j for j in jobs if j["id"] == unmarked_job["id"])

        assert row["archived_at"] is not None

    def test_unknown_job_is_a_404(self, app_server: str, page: Page):
        response = page.request.patch(f"{app_server}/api/jobs/99999999/marks", data={"applied": True})

        assert response.status == 404

    def test_archiving_from_the_list_hides_the_card(self, board: Page, unmarked_job):
        card = board.locator(f'li[data-job-id="{unmarked_job["id"]}"]')
        expect(card).to_be_visible()
        before = board.locator("ul > li").count()

        card.get_by_role("button", name="Archive").click()

        expect(board.locator("ul > li")).to_have_count(before - 1)

    def test_archived_filter_shows_it_again(self, board: Page, unmarked_job):
        board.locator(f'li[data-job-id="{unmarked_job["id"]}"]').get_by_role(
            "button", name="Archive"
        ).click()

        board.get_by_label("Show", exact=True).select_option("archived")

        card = board.locator(f'li[data-job-id="{unmarked_job["id"]}"]')
        expect(card.get_by_role("button", name="Unarchive")).to_be_visible()

    def test_mark_applied_button_does_not_open_the_drawer(self, board: Page, unmarked_job):
        board.get_by_label("Show", exact=True).select_option("all")
        card = board.locator(f'li[data-job-id="{unmarked_job["id"]}"]')

        card.get_by_role("button", name="Mark applied").click()

        expect(card.get_by_role("button", name="Applied ✓")).to_be_visible()
        expect(board.get_by_role("button", name="Show Dutch original")).not_to_be_visible()
