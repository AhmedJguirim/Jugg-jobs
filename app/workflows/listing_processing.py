"""Steps that read a raw listing, shared by the grading workflow.

    load_listing_step -> parse_step

Each step is checkpointed. Parsing is pure CPU on text already in the database,
so there is no GPU or network involved. A page that cannot be used - a removed
posting, an unsupported site, markup the parser does not recognise - is a normal
result (`status` in the return value, see `app/services/listing_processing.py`),
not an exception: raising would make DBOS retry a deterministic outcome.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from app.db.session import session_scope
from app.services import listing_processing as processing


@DBOS.step()
def load_listing_step(raw_listing_id: int) -> dict[str, Any]:
    with session_scope() as session:
        return processing.load_listing(session, raw_listing_id)


@DBOS.step()
def parse_step(listing: dict[str, Any]) -> dict[str, Any]:
    return processing.parse(listing)
