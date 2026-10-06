"""What the batch scrape panel says about each employer."""

from __future__ import annotations

import pytest

from app.services import scrape_progress as progress
from app.workflows import enrichment as flow


class TestStateOf:
    @pytest.mark.parametrize(
        "status, state",
        [
            (None, "waiting"),
            ("ENQUEUED", "waiting"),
            ("PENDING", "running"),
            ("SUCCESS", "done"),
            ("CANCELLED", "cancelled"),
            ("ERROR", "failed"),
            ("MAX_RECOVERY_ATTEMPTS_EXCEEDED", "failed"),
        ],
    )
    def test_maps_dbos_statuses(self, status, state):
        assert progress.state_of(status) == state


class TestRunningLabel:
    def test_nothing_published_yet(self):
        assert progress.running_label(None) == "starting"

    def test_first_page_before_the_page_count_is_known(self):
        label = progress.running_label({"phase": "fetching", "page": 1, "pages": None, "found": 0})

        assert label == "searching the job board"

    def test_later_page_shows_position_and_finds(self):
        label = progress.running_label({"phase": "fetching", "page": 3, "pages": 5, "found": 40})

        assert label == "fetching page 3/5 · 40 found"

    def test_grading_shows_the_vacancy_and_its_stage(self):
        label = progress.running_label(
            {"phase": "grading", "done": 4, "total": 23},
            current_state="running",
            current_stage="translating the description",
        )

        assert label == "vacancy 5/23 - translating the description"

    def test_grading_behind_another_employer_says_so(self):
        """Another employer's vacancy can hold the GPU; a long pause needs a reason."""
        label = progress.running_label({"phase": "grading", "done": 0, "total": 8}, current_state="waiting")

        assert label == "vacancy 1/8 - waiting for the GPU"

    def test_grading_before_the_first_stage_is_published(self):
        label = progress.running_label({"phase": "grading", "done": 2, "total": 3}, current_state="running")

        assert label == "vacancy 3/3 - reading the vacancy"

    def test_other_phases_are_shown_as_is(self):
        assert progress.running_label({"phase": "checking the model server"}) == "checking the model server"


class TestFinishedLabel:
    def test_a_normal_finish(self):
        label, problem = progress.finished_label(
            {"status": "done", "vacancies": 23, "created": 5, "enriched": 5, "failed": 0}
        )

        assert label == "23 found · 5 new · 5 graded"
        assert problem is False

    def test_nothing_to_grade(self):
        label, problem = progress.finished_label({"status": "done", "vacancies": 0, "created": 0})

        assert label == "0 found · 0 new"
        assert problem is False

    def test_ollama_down_is_a_problem(self):
        label, problem = progress.finished_label(
            {"status": "done", "vacancies": 7, "created": 7, "enrich_status": "llm_unavailable"}
        )

        assert "Ollama is not running" in label
        assert problem is True

    def test_failed_vacancies_flag_a_problem(self):
        _, problem = progress.finished_label({"status": "done", "vacancies": 3, "enriched": 2, "failed": 1})

        assert problem is True

    def test_no_query(self):
        label, problem = progress.finished_label({"status": "no_query"})

        assert label == "no search query for this employer"
        assert problem is True


class TestBatchSummary:
    def test_counts_each_state(self):
        rows = [{"state": s} for s in ("done", "done", "running", "waiting", "failed")]

        counts = progress.batch_summary(rows)

        assert counts == {"total": 5, "waiting": 1, "running": 1, "done": 2, "cancelled": 0, "failed": 1}


class TestBatchWiring:
    def test_child_ids_are_deterministic(self):
        """The status endpoint finds each company's workflow from the batch id alone."""
        assert flow.batch_child_id("abc", 7) == flow.batch_child_id("abc", 7) == "abc-company-7"

    def test_child_ids_differ_per_company(self):
        assert flow.batch_child_id("abc", 7) != flow.batch_child_id("abc", 8)

    def test_batch_size_is_capped(self):
        assert flow.MAX_COMPANIES_PER_BATCH == 50

    def test_company_scrapes_run_two_at_a_time(self):
        """The GPU is the bottleneck: one being graded, the next being fetched."""
        assert flow.COMPANY_SCRAPE_CONCURRENCY == 2

    def test_batch_workflow_is_registered(self):
        assert callable(flow.scrape_companies_workflow)
