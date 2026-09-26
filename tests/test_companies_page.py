"""End-to-end tests for the employers browser.

Driven against the real database, so these also check the two kinds of employer
the table holds: seed rows that carry job-board search links, and rows
discovered from vacancies that carry a type and actual jobs.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


@pytest.fixture
def page_loaded(app_server: str, page: Page) -> Page:
    page.goto(f"{app_server}/companies")
    expect(page.get_by_role("heading", name="Employers")).to_be_visible(timeout=15_000)
    expect(page.locator("ul > li").first).to_be_visible(timeout=15_000)
    return page


class TestApi:
    def test_listing_is_paginated(self, app_server: str, page: Page):
        body = page.request.get(f"{app_server}/api/companies?limit=5").json()

        assert len(body["items"]) <= 5
        assert body["total"] > 5
        assert body["has_more"] is True

    def test_offset_returns_different_rows(self, app_server: str, page: Page):
        first = page.request.get(f"{app_server}/api/companies?limit=5&offset=0").json()
        second = page.request.get(f"{app_server}/api/companies?limit=5&offset=5").json()

        assert {c["id"] for c in first["items"]}.isdisjoint({c["id"] for c in second["items"]})

    def test_search_narrows_the_total(self, app_server: str, page: Page):
        everything = page.request.get(f"{app_server}/api/companies?limit=1").json()["total"]
        filtered = page.request.get(f"{app_server}/api/companies?limit=1&q=zzzznotreal").json()

        assert filtered["total"] < everything
        assert filtered["total"] == 0

    def test_has_jobs_filter(self, app_server: str, page: Page):
        body = page.request.get(f"{app_server}/api/companies?has_jobs=true&limit=50").json()

        assert body["total"] > 0
        assert all(c["vacancies"] > 0 for c in body["items"])

    def test_sorting_by_vacancies_puts_the_busiest_first(self, app_server: str, page: Page):
        items = page.request.get(
            f"{app_server}/api/companies?order_by=vacancies&limit=10"
        ).json()["items"]

        counts = [c["vacancies"] for c in items]
        assert counts == sorted(counts, reverse=True)

    def test_unknown_sort_is_rejected(self, app_server: str, page: Page):
        response = page.request.get(f"{app_server}/api/companies?order_by=drop_table")

        assert response.status == 400

    def test_facets_expose_the_filter_values(self, app_server: str, page: Page):
        body = page.request.get(f"{app_server}/api/companies/facets").json()

        assert body["total"] > 0
        assert body["with_jobs"] <= body["total"]
        assert isinstance(body["provinces"], list)

    def test_detail_includes_vacancies_and_links(self, app_server: str, page: Page):
        listed = page.request.get(f"{app_server}/api/companies?has_jobs=true&limit=1").json()
        company_id = listed["items"][0]["id"]

        detail = page.request.get(f"{app_server}/api/companies/{company_id}").json()

        assert "vacancy_list" in detail
        assert "links" in detail
        assert len(detail["vacancy_list"]) > 0

    def test_seed_employers_carry_job_board_links(self, app_server: str, page: Page):
        """The 2901 seed rows exist precisely for these search URLs."""
        body = page.request.get(f"{app_server}/api/companies?has_links=true&limit=1").json()

        assert body["total"] > 1000
        assert body["items"][0]["links"]["google"]

    def test_unknown_company_is_a_404(self, app_server: str, page: Page):
        assert page.request.get(f"{app_server}/api/companies/99999999").status == 404


class TestPage:
    def test_title_and_heading(self, page_loaded: Page):
        expect(page_loaded).to_have_title("Employers - job-hunt")

    def test_stat_tiles_render(self, page_loaded: Page):
        expect(page_loaded.locator("text=with vacancies")).to_be_visible()
        expect(page_loaded.locator("text=with search links")).to_be_visible()

    def test_employers_are_listed(self, page_loaded: Page):
        assert page_loaded.locator("ul > li").count() > 0

    def test_no_raw_alpine_markup_leaks(self, page_loaded: Page):
        body = page_loaded.locator("body").inner_text()

        assert "x-text" not in body
        assert "${" not in body

    def test_links_to_the_job_board(self, page_loaded: Page):
        expect(page_loaded.get_by_role("link", name="Job board")).to_be_visible()


class TestFilters:
    def test_search_filters_the_list(self, page_loaded: Page):
        cards = page_loaded.locator("ul > li")

        page_loaded.get_by_placeholder("company name…").fill("zzzznotreal")

        expect(cards).to_have_count(0)
        expect(page_loaded.locator("text=No employer matches these filters")).to_be_visible()

    def test_reset_restores_the_list(self, page_loaded: Page):
        cards = page_loaded.locator("ul > li")
        page_loaded.get_by_placeholder("company name…").fill("zzzznotreal")
        expect(cards).to_have_count(0)

        page_loaded.get_by_role("button", name="Reset them").click()

        expect(cards).not_to_have_count(0)

    def test_only_with_vacancies_narrows_the_total(self, page_loaded: Page):
        """3017 employers overall, but only ~116 have an ingested vacancy."""
        headings = page_loaded.locator("ul > li h2")
        first_name = headings.first.inner_text().strip()

        page_loaded.locator('input[type="checkbox"]').first.check()

        # The listing re-fetches from the server, so wait for it to change
        # before reading the cards.
        expect(headings.first).not_to_have_text(first_name)
        cards = page_loaded.locator("ul > li")
        assert all(
            "vacancies" in cards.nth(i).inner_text().lower()
            for i in range(min(5, cards.count()))
        )


class TestPagination:
    def test_next_advances_the_page(self, page_loaded: Page):
        # Compare the employer name only: whole-card text includes labels that
        # CSS upper-cases, which makes exact comparison brittle.
        heading = page_loaded.locator("ul > li h2").first
        first_name = heading.inner_text().strip()

        page_loaded.get_by_role("button", name="Next →").click()

        expect(page_loaded.locator("ul > li h2").first).not_to_have_text(first_name)

    def test_previous_is_disabled_on_the_first_page(self, page_loaded: Page):
        expect(page_loaded.get_by_role("button", name="← Previous")).to_be_disabled()

    def test_previous_returns_to_the_first_page(self, page_loaded: Page):
        first_name = page_loaded.locator("ul > li h2").first.inner_text().strip()
        page_loaded.get_by_role("button", name="Next →").click()
        expect(page_loaded.locator("ul > li h2").first).not_to_have_text(first_name)

        page_loaded.get_by_role("button", name="← Previous").click()

        expect(page_loaded.locator("ul > li h2").first).to_have_text(first_name)


class TestDetailDrawer:
    def test_clicking_an_employer_opens_the_drawer(self, page_loaded: Page):
        page_loaded.locator("ul > li").first.click()

        expect(page_loaded.locator("text=Search this employer's jobs")).to_be_visible()

    def test_drawer_shows_job_board_links_for_a_seed_employer(self, page_loaded: Page):
        """A seed employer has no vacancies but five search links."""
        page_loaded.locator("ul > li").first.click()

        expect(page_loaded.get_by_role("link", name="Google ↗")).to_be_visible(timeout=10_000)

    def test_drawer_lists_vacancies_when_there_are_any(self, page_loaded: Page):
        page_loaded.locator('input[type="checkbox"]').first.check()
        expect(page_loaded.locator("ul > li").first).to_be_visible()

        page_loaded.locator("ul > li").first.click()

        expect(
            page_loaded.get_by_role("heading", name=re.compile("Ingested vacancies"))
        ).to_be_visible()

    def test_escape_closes_the_drawer(self, page_loaded: Page):
        page_loaded.locator("ul > li").first.click()
        expect(page_loaded.locator("text=Search this employer's jobs")).to_be_visible()

        page_loaded.keyboard.press("Escape")

        expect(page_loaded.locator("text=Search this employer's jobs")).not_to_be_visible()


class TestScraping:
    """The per-employer scrape button and the endpoint behind it.

    Enrichment is switched off in these tests: they check the wiring, not the
    model, and a real grading pass would tie the suite to the GPU.
    """

    def test_scrape_returns_a_workflow_and_the_derived_query(
        self, app_server: str, page: Page
    ):
        listed = page.request.get(f"{app_server}/api/companies?has_links=true&limit=1").json()
        company = listed["items"][0]

        response = page.request.post(
            f"{app_server}/api/companies/{company['id']}/scrape",
            data={"enrich": False, "limit": 25, "max_pages": 1},
        )

        assert response.ok
        body = response.json()
        assert body["workflow_id"]
        # The query comes from the employer's own search link.
        assert body["query"]

    def test_scraping_an_unknown_company_is_a_404(self, app_server: str, page: Page):
        response = page.request.post(
            f"{app_server}/api/companies/99999999/scrape", data={"enrich": False}
        )

        assert response.status == 404

    def test_scrape_records_the_timestamp(self, app_server: str, page: Page):
        listed = page.request.get(f"{app_server}/api/companies?has_links=true&limit=1").json()
        company_id = listed["items"][0]["id"]

        started = page.request.post(
            f"{app_server}/api/companies/{company_id}/scrape",
            data={"enrich": False, "limit": 25, "max_pages": 1},
        ).json()

        # Poll until the workflow finishes, then the stamp must be set.
        for _ in range(60):
            status = page.request.get(
                f"{app_server}/api/companies/scrape/{started['workflow_id']}"
            ).json()
            if status["status"] == "SUCCESS":
                break
            page.wait_for_timeout(1000)
        else:
            pytest.fail("scrape did not finish in time")

        detail = page.request.get(f"{app_server}/api/companies/{company_id}").json()
        assert detail["last_scraped_at"] is not None
        assert detail["last_scrape_vacancies"] is not None

    def test_listing_exposes_the_search_query_and_scrape_state(
        self, app_server: str, page: Page
    ):
        item = page.request.get(f"{app_server}/api/companies?limit=1").json()["items"][0]

        assert "search_query" in item
        assert "last_scraped_at" in item
        assert "last_scrape_vacancies" in item

    def test_drawer_offers_the_scrape_button(self, page_loaded: Page):
        page_loaded.locator("ul > li").first.click()

        expect(page_loaded.get_by_role("button", name="Scrape vacancies")).to_be_visible()

    def test_drawer_shows_the_query_that_will_be_searched(self, page_loaded: Page):
        page_loaded.locator("ul > li").first.click()

        expect(page_loaded.locator("text=on Nationale Vacaturebank")).to_be_visible()

    def test_never_scraped_employers_say_so(self, page_loaded: Page):
        """"Never looked" must be visibly different from "looked, found none"."""
        expect(page_loaded.locator("text=never scraped").first).to_be_visible()


class TestRowScrapeButton:
    """The scrape button on each listing row.

    The row itself opens the drawer, so the button must stop the click from
    propagating - otherwise every scrape would also pop the drawer open.
    """

    def test_every_row_has_a_scrape_button(self, page_loaded: Page):
        rows = page_loaded.locator("ul > li")
        buttons = page_loaded.locator('ul > li button:has-text("Scrape")')

        assert buttons.count() == rows.count()

    def test_clicking_it_does_not_open_the_drawer(self, page_loaded: Page):
        page_loaded.locator('ul > li button:has-text("Scrape")').first.click()

        expect(page_loaded.locator("text=Search this employer's jobs")).not_to_be_visible()

    def test_clicking_it_starts_a_scrape(self, page_loaded: Page):
        """Assert on the status line, not the button label.

        The first employer returns nothing in about a second, so "Scraping…"
        can be gone before the assertion runs; the status it leaves behind is
        the durable signal.
        """
        row = page_loaded.locator("ul > li").first

        row.locator('button:has-text("Scrape")').click()

        expect(row.locator("p").last).not_to_be_empty(timeout=30_000)

    def test_scrape_state_is_per_row(self, page_loaded: Page):
        """One row's progress must not label any other row."""
        rows = page_loaded.locator("ul > li")

        rows.first.locator('button:has-text("Scrape")').click()
        expect(rows.first.locator("p").last).not_to_be_empty(timeout=30_000)

        assert rows.nth(1).locator('button:has-text("Scrape")').inner_text().strip() == "Scrape"

    def test_the_row_reports_the_result_when_it_finishes(self, page_loaded: Page):
        button = page_loaded.locator('ul > li button:has-text("Scrape")').first
        status = page_loaded.locator("ul > li").first.locator("p").last

        button.click()

        # "N found · M new" once done, or an error label; either way it reports.
        expect(status).to_contain_text("found", timeout=180_000)

    def test_the_drawer_button_still_works(self, page_loaded: Page):
        page_loaded.locator("ul > li h2").first.click()

        expect(page_loaded.get_by_role("button", name="Scrape vacancies")).to_be_visible()


class TestCompletionSound:
    """The chime that marks a finished scrape.

    Synthesised with the Web Audio API, so there is no audio file to assert on.
    These tests instead record every oscillator the page starts, which is the
    closest thing to "what did the user hear".
    """

    @pytest.fixture
    def listening(self, app_server: str, page: Page) -> Page:
        page.add_init_script(
            """
            window.__notes = [];
            const Real = window.AudioContext;
            if (Real) {
              window.AudioContext = class extends Real {
                createOscillator() {
                  const osc = super.createOscillator();
                  const set = osc.frequency.setValueAtTime.bind(osc.frequency);
                  osc.frequency.setValueAtTime = (value, at) => {
                    window.__notes.push(Math.round(value));
                    return set(value, at);
                  };
                  return osc;
                }
              };
            }
            """
        )
        page.goto(f"{app_server}/companies")
        expect(page.locator("ul > li").first).to_be_visible(timeout=15_000)
        return page

    def state(self, page: Page):
        return "Alpine.$data(document.querySelector('[x-data]'))"

    def test_sound_is_on_by_default(self, listening: Page):
        assert listening.evaluate(f"{self.state(listening)}.soundEnabled") is True

    def test_success_plays_a_rising_triad(self, listening: Page):
        listening.evaluate(f"{self.state(listening)}.playSuccess()")

        notes = listening.evaluate("window.__notes")
        assert notes == sorted(notes), "a success should rise, not fall"
        assert len(notes) == 3

    def test_failure_sounds_different_from_success(self, listening: Page):
        listening.evaluate(f"{self.state(listening)}.playFailure()")
        failure = listening.evaluate("window.__notes")

        listening.evaluate("window.__notes = []")
        listening.evaluate(f"{self.state(listening)}.playSuccess()")
        success = listening.evaluate("window.__notes")

        assert failure != success
        assert failure == sorted(failure, reverse=True), "a failure should fall"
        assert max(failure) < max(success), "a failure should be lower pitched"

    def test_muting_silences_it(self, listening: Page):
        listening.evaluate(f"{self.state(listening)}.soundEnabled = false")
        listening.evaluate("window.__notes = []")

        listening.evaluate(f"{self.state(listening)}.playSuccess()")

        assert listening.evaluate("window.__notes") == []

    def test_the_toggle_is_in_the_header(self, listening: Page):
        expect(listening.get_by_role("button", name="Mute completion sound")).to_be_visible()

    def test_the_preference_survives_a_reload(self, listening: Page):
        listening.get_by_role("button", name="Mute completion sound").click()

        listening.reload()
        expect(listening.locator("ul > li").first).to_be_visible(timeout=15_000)

        assert listening.evaluate(f"{self.state(listening)}.soundEnabled") is False
        # Leave the browser profile as we found it for the next test.
        listening.evaluate("localStorage.removeItem('scrapeSound')")

    def test_blocked_storage_does_not_break_the_page(self, listening: Page):
        """Private windows can make localStorage throw; sound must still work."""
        listening.evaluate(
            "Object.defineProperty(window, 'localStorage', "
            "{ get() { throw new Error('blocked'); } })"
        )

        listening.evaluate(f"{self.state(listening)}.loadSoundPreference()")

        assert listening.evaluate(f"{self.state(listening)}.soundEnabled") is True


class TestPageCap:
    """A scrape walks at most MAX_PAGES_PER_SCRAPE result pages.

    The board searches full text, so an employer whose name contains a common
    word matches much of the site: "7 Solutions" returns 287 vacancies across
    12 pages at 25 per page, and 58 pages at 5 per page.
    """

    def test_the_cap_is_twenty(self):
        from app.workflows.enrichment import MAX_PAGES_PER_SCRAPE

        assert MAX_PAGES_PER_SCRAPE == 20

    def test_a_broad_search_is_truncated(self, app_server: str, page: Page):
        listed = page.request.get(f"{app_server}/api/companies?q=7 Solutions&limit=1").json()
        if not listed["items"]:
            pytest.skip("the 7 Solutions employer is not in this database")

        started = page.request.post(
            f"{app_server}/api/companies/{listed['items'][0]['id']}/scrape",
            data={"enrich": False, "limit": 5},
        ).json()

        for _ in range(120):
            status = page.request.get(
                f"{app_server}/api/companies/scrape/{started['workflow_id']}"
            ).json()
            if status["status"] in ("SUCCESS", "ERROR"):
                break
            page.wait_for_timeout(1000)
        else:
            pytest.fail("scrape did not finish in time")

        result = status["result"]
        assert result["pages"] == 20
        assert result["pages_available"] > 20
        assert result["truncated"] is True

    def test_a_normal_search_is_not_marked_truncated(self, app_server: str, page: Page):
        listed = page.request.get(f"{app_server}/api/companies?has_links=true&limit=1").json()

        started = page.request.post(
            f"{app_server}/api/companies/{listed['items'][0]['id']}/scrape",
            data={"enrich": False, "limit": 50},
        ).json()

        for _ in range(60):
            status = page.request.get(
                f"{app_server}/api/companies/scrape/{started['workflow_id']}"
            ).json()
            if status["status"] in ("SUCCESS", "ERROR"):
                break
            page.wait_for_timeout(1000)

        assert status["result"]["truncated"] is False


class TestCancelling:
    def test_cancelling_an_unknown_workflow_is_a_404(self, app_server: str, page: Page):
        response = page.request.post(
            f"{app_server}/api/companies/scrape/not-a-real-workflow/cancel"
        )

        assert response.status == 404

    def test_a_cancelled_scrape_reports_cancelled(self, app_server: str, page: Page):
        listed = page.request.get(f"{app_server}/api/companies?q=7 Solutions&limit=1").json()
        if not listed["items"]:
            pytest.skip("the 7 Solutions employer is not in this database")

        started = page.request.post(
            f"{app_server}/api/companies/{listed['items'][0]['id']}/scrape",
            data={"enrich": False, "limit": 5},
        ).json()

        page.request.post(
            f"{app_server}/api/companies/scrape/{started['workflow_id']}/cancel"
        )

        for _ in range(30):
            status = page.request.get(
                f"{app_server}/api/companies/scrape/{started['workflow_id']}"
            ).json()
            if status["status"] == "CANCELLED":
                break
            page.wait_for_timeout(500)

        assert status["status"] == "CANCELLED"

    def test_the_cancel_button_is_in_the_drawer_only(self, page_loaded: Page):
        """Cancelling is a deliberate act, so it lives in the detail view."""
        assert page_loaded.locator('ul > li button:has-text("Cancel")').count() == 0

        page_loaded.locator("ul > li h2").first.click()
        expect(page_loaded.get_by_role("button", name="Scrape vacancies")).to_be_visible()
        # Hidden until a scrape is actually running.
        expect(page_loaded.get_by_role("button", name="Cancel")).not_to_be_visible()
