"""How a DBOS workflow status reads on the workspace's progress lines."""

from __future__ import annotations

import pytest

from app.services import listing_progress as progress


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
