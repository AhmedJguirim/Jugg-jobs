"""Durable workflow: fetch the pages of collected links and put them on the waiting list (ADR-0024).

    for each link:  fetch_link_step  (request the page, store it as a raw listing)

The "Process all" button on /workspace/urls starts it. It fetches and stores,
nothing more: grading is on demand (ADR-0016), so the pages end up on the
waiting list for the Grade buttons or a grading batch.

One link is one checkpointed step, so a restart resumes at the next link instead
of asking the servers for pages already stored. The workflow runs on its own
queue with a concurrency of one, which is what keeps the 1-3 s pause between
requests meaningful: two runs at once would double the pace on every host.

A link that could not be fetched (blocked, timed out, a page without the posting)
is counted and left without HTML; it is simply picked up by the next run.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from app.logging_setup import log
from app.services import listing_fetch as fetching

FETCH_QUEUE = "listing-fetch"
PROGRESS_EVENT = "progress"

# Most links one click takes. A link costs a couple of seconds of pacing, so this
# is about half an hour; the rest is picked up by the next click.
MAX_PER_RUN = 500

# How many unfinished links the progress event remembers, so the page can say why.
KEEP_PROBLEMS = 15

_queue_registered = False


def register_queue() -> None:
    """Register the queue. Call AFTER `DBOS.launch()` (see
    `llm_queue.register_queues` for why). Idempotent.

    Both limits are one: `worker_concurrency` for this process, `global_concurrency`
    for any other process on the same system database.
    """
    global _queue_registered
    if _queue_registered:
        return
    DBOS.register_queue(FETCH_QUEUE, worker_concurrency=1, global_concurrency=1, on_conflict="always_update")
    _queue_registered = True


@DBOS.step()
def fetch_link_step(url: str, pause: bool) -> dict[str, Any]:
    """Request one link's page and store it. `pause`: a request was just made."""
    return fetching.process_link(url, pause=pause)


@DBOS.workflow()
def fetch_listings_workflow(urls: list[str]) -> dict[str, Any]:
    """Fetch each link in turn; progress is published as the `progress` event."""
    progress: dict[str, Any] = {
        "total": len(urls), "done": 0,
        fetching.ADDED: 0, fetching.GONE: 0, fetching.RETRY: 0, fetching.EXISTS: 0,
        "current": None, "problems": [],
    }
    DBOS.set_event(PROGRESS_EVENT, progress)
    log.info("fetching run %s: %s link(s)", DBOS.workflow_id, len(urls))

    pause = False
    for url in urls:
        progress["current"] = url
        DBOS.set_event(PROGRESS_EVENT, progress)
        try:
            result = fetch_link_step(url, pause)
        except Exception as error:  # one link must not sink the run
            log.warning("fetching run %s: %s failed: %s", DBOS.workflow_id, url, error)
            result = {
                "url": url, "status": "failed", "action": fetching.RETRY,
                "note": f"{type(error).__name__}: {error}", "requested": True,
            }
        pause = bool(result["requested"])

        progress["done"] += 1
        progress[result["action"]] += 1
        if result["action"] == fetching.RETRY:
            progress["problems"] = [
                *progress["problems"][-(KEEP_PROBLEMS - 1):],
                {"url": url, "status": result["status"], "note": result["note"]},
            ]
        DBOS.set_event(PROGRESS_EVENT, progress)

    progress["current"] = None
    DBOS.set_event(PROGRESS_EVENT, progress)
    summary = {k: v for k, v in progress.items() if k not in ("current", "problems")}
    log.info("fetching run %s done: %s", DBOS.workflow_id, summary)
    return summary
