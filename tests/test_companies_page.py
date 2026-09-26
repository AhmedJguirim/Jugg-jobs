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
