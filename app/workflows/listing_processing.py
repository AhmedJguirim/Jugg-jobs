"""Durable workflow: turn one raw job listing into a `jobs_test` row.

    load_listing_step -> parse_step -> store_step

Each step is checkpointed. Parsing is pure CPU on text already in the database,
so there is no GPU or network involved and no queue: the workflow is started
directly. Storing is the only step with side effects and it is an upsert on
(source, external_id), so a replay after a crash cannot create a duplicate.

The workflow does not raise for a page it cannot use - a removed posting, an
unsupported site, markup it does not recognise. Those are normal results
(`status` in the return value, see `app/services/listing_processing.py`);
raising would make DBOS retry a deterministic outcome.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from app.db.session import session_scope
from app.logging_setup import log
from app.services import listing_processing as processing


@DBOS.step()
def load_listing_step(raw_listing_id: int) -> dict[str, Any]:
    with session_scope() as session:
        return processing.load_listing(session, raw_listing_id)


@DBOS.step()
def parse_step(listing: dict[str, Any]) -> dict[str, Any]:
    return processing.parse(listing)


@DBOS.step()
def store_step(parsed: dict[str, Any]) -> dict[str, Any]:
    with session_scope() as session:
        return processing.store(session, parsed)


@DBOS.workflow()
def process_raw_listing_workflow(raw_listing_id: int) -> dict[str, Any]:
    listing = load_listing_step(raw_listing_id)
    parsed = parse_step(listing)
    result = store_step(parsed)
    log.info(
        "raw listing %s: %s%s",
        raw_listing_id,
        result["status"],
        f" ({result['reason']})" if result.get("reason") else "",
    )
    return result
