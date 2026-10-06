"""Guards on the queue configuration.

The single-GPU guarantee is a configuration property, so it is asserted here.
If someone raises the llm queue's concurrency, these tests fail loudly rather
than the GPU failing quietly.

Verified separately at runtime (six jobs enqueued at once produced peak observed
concurrency 1); this file stops the configuration from drifting afterwards.
"""

from __future__ import annotations

import pytest
from dbos import DBOS

from app.workflows import enrichment as flow


class TestConcurrencyConstants:
    def test_llm_queue_allows_exactly_one_job(self):
        assert flow.LLM_CONCURRENCY == 1, (
            "The llm queue must stay at 1: there is one GPU, and a second "
            "concurrent model would exhaust its VRAM."
        )

    def test_ingest_queue_is_allowed_to_be_parallel(self):
        """Page fetching is network-bound, so it is not the GPU's problem."""
        assert flow.INGEST_WORKER_CONCURRENCY >= 1

    def test_queue_names_are_distinct(self):
        assert len({flow.LLM_QUEUE, flow.INGEST_QUEUE, flow.COMPANY_QUEUE}) == 3


class TestRegisterQueues:
    @pytest.fixture
    def recorded(self, monkeypatch):
        calls: list[dict] = []

        def fake_register(name, **kwargs):
            calls.append({"name": name, **kwargs})
            return object()

        monkeypatch.setattr(DBOS, "register_queue", fake_register)
        monkeypatch.setattr(flow, "_queues_registered", False)
        flow.register_queues()
        return {call["name"]: call for call in calls}

    def test_registers_every_queue(self, recorded):
        assert flow.LLM_QUEUE in recorded
        assert flow.INGEST_QUEUE in recorded
        assert flow.COMPANY_QUEUE in recorded

    def test_company_queue_never_touches_the_gpu_limit(self, recorded):
        """Company scrapes only wait on the llm queue; they must not share it."""
        assert recorded[flow.COMPANY_QUEUE]["worker_concurrency"] == flow.COMPANY_SCRAPE_CONCURRENCY

    def test_llm_queue_limits_this_process(self, recorded):
        assert recorded[flow.LLM_QUEUE]["worker_concurrency"] == 1

    def test_llm_queue_limits_every_process(self, recorded):
        """global_concurrency is the part a single Horizon worker cannot give:
        it is enforced in the database, so a second app process is also bound."""
        assert recorded[flow.LLM_QUEUE]["global_concurrency"] == 1

    def test_ingest_queue_sets_no_global_limit(self, recorded):
        assert "global_concurrency" not in recorded[flow.INGEST_QUEUE]

    def test_registration_is_idempotent(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(DBOS, "register_queue", lambda name, **k: calls.append(name))
        monkeypatch.setattr(flow, "_queues_registered", False)

        flow.register_queues()
        flow.register_queues()

        assert len(calls) == 3  # three queues, registered once each


class TestWorkflowsAreRegistered:
    @pytest.mark.parametrize(
        "name",
        [
            "enrich_job_workflow",
            "enrich_pending_workflow",
            "scrape_and_enrich_workflow",
            "ingest_page_workflow",
        ],
    )
    def test_workflow_exists(self, name: str):
        assert callable(getattr(flow, name))

    @pytest.mark.parametrize(
        "name",
        ["prepare_text", "translate_step", "assess_step", "persist_enrichment", "fetch_page", "store_page"],
    )
    def test_step_exists(self, name: str):
        assert callable(getattr(flow, name))


class TestApplicationVersion:
    """Every process must run workflows under the same DBOS version.

    `enrich_jobs.py --clear-stuck` cancels PENDING workflows of any version but
    its own, so a copy of the version that lagged behind main.py would cancel
    healthy work. Both read the one constant instead.
    """

    @pytest.mark.parametrize("path", ["main.py", "scripts/enrich_jobs.py"])
    def test_no_hardcoded_version(self, path: str):
        import os
        import re

        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            source = handle.read()

        assert not re.search(r'"application_version":\s*"', source), (
            f"{path} hardcodes application_version; use flow.APPLICATION_VERSION"
        )
