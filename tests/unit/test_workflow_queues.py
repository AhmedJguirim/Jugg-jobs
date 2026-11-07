"""Guards on the queue configuration.

The single-GPU guarantee is a configuration property, so it is asserted here.
If someone raises the llm queue's concurrency, these tests fail loudly rather
than the GPU failing quietly.

Verified separately at runtime (six jobs enqueued at once produced peak observed
concurrency 1); this file stops the configuration from drifting afterwards.
"""

from __future__ import annotations

import os
import re

import pytest
from dbos import DBOS

from app.workflows import listing_fetch, listing_grading
from app.workflows import llm_queue as flow


def _recorded(monkeypatch, register) -> dict[str, dict]:
    calls: list[dict] = []
    monkeypatch.setattr(DBOS, "register_queue", lambda name, **kwargs: calls.append({"name": name, **kwargs}))
    register()
    return {call["name"]: call for call in calls}


class TestTheGpuQueue:
    def test_allows_exactly_one_job(self):
        assert flow.LLM_CONCURRENCY == 1, (
            "The llm queue must stay at 1: there is one GPU, and a second "
            "concurrent model would exhaust its VRAM."
        )

    @pytest.fixture
    def recorded(self, monkeypatch):
        monkeypatch.setattr(flow, "_queues_registered", False)
        return _recorded(monkeypatch, flow.register_queues)

    def test_limits_this_process(self, recorded):
        assert recorded[flow.LLM_QUEUE]["worker_concurrency"] == 1

    def test_limits_every_process(self, recorded):
        """global_concurrency is enforced in the database, so a second app
        process is bound too."""
        assert recorded[flow.LLM_QUEUE]["global_concurrency"] == 1

    def test_registration_is_idempotent(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(DBOS, "register_queue", lambda name, **k: calls.append(name))
        monkeypatch.setattr(flow, "_queues_registered", False)

        flow.register_queues()
        flow.register_queues()

        assert calls == [flow.LLM_QUEUE]


class TestTheOtherQueues:
    def test_names_are_distinct(self):
        assert len({flow.LLM_QUEUE, listing_grading.LISTING_QUEUE, listing_fetch.FETCH_QUEUE}) == 3

    def test_grading_batches_never_touch_the_gpu_limit(self, monkeypatch):
        """A batch only waits on the llm queue; it must not share its limit."""
        monkeypatch.setattr(listing_grading, "_queue_registered", False)
        recorded = _recorded(monkeypatch, listing_grading.register_queue)

        assert "global_concurrency" not in recorded[listing_grading.LISTING_QUEUE]

    def test_fetching_is_one_page_at_a_time_everywhere(self, monkeypatch):
        """Two runs at once would double the request rate on every host."""
        monkeypatch.setattr(listing_fetch, "_queue_registered", False)
        recorded = _recorded(monkeypatch, listing_fetch.register_queue)[listing_fetch.FETCH_QUEUE]

        assert (recorded["worker_concurrency"], recorded["global_concurrency"]) == (1, 1)


class TestWorkflowsAreRegistered:
    @pytest.mark.parametrize(
        "module, name",
        [
            (listing_grading, "process_listing_workflow"),
            (listing_grading, "grade_listings_workflow"),
            (listing_grading, "grade_listing_workflow"),
            (listing_fetch, "fetch_listings_workflow"),
        ],
    )
    def test_workflow_exists(self, module, name: str):
        assert callable(getattr(module, name))


class TestApplicationVersion:
    """Every process must run workflows under the same DBOS version, so each
    reads the one constant instead of keeping a copy that can lag behind."""

    @pytest.mark.parametrize("path", ["main.py", "scripts/process_listing.py"])
    def test_no_hardcoded_version(self, path: str):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            source = handle.read()

        assert not re.search(r'"application_version":\s*"', source), (
            f"{path} hardcodes application_version; use llm_queue.APPLICATION_VERSION"
        )
