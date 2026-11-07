"""Cancel workflows stranded PENDING by an older version of the code.

    python scripts/clear_stuck.py           # list them, then cancel them
    python scripts/clear_stuck.py --dry-run # only list them

**Stop the app first.** This opens the app's own DBOS state, and two processes
on it at once could both resume the same in-flight work.

Why this is needed: the `llm` queue allows one job at a time, and DBOS only
recovers workflows belonging to the *current* application version. Bump the
version while a job is running - which is exactly what you must do when a
workflow's steps change - and that job stays PENDING forever while still
counting against the concurrency limit. The queue then looks busy and never
dequeues anything again. Symptom: listings waiting, zero GPU activity, no
errors in `logs/dev.log`.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbos import DBOS, DBOSConfig  # noqa: E402

from app.workflows import llm_queue  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="list stranded workflows without cancelling them")
    args = parser.parse_args()

    config: DBOSConfig = {
        # The app's name, so this opens the app's DBOS state (see main.py).
        "name": "dbos-app-starter",
        "system_database_url": os.environ.get("DBOS_SYSTEM_DATABASE_URL"),
        "application_version": llm_queue.APPLICATION_VERSION,
    }
    DBOS(config=config)
    DBOS.launch()
    try:
        current = llm_queue.APPLICATION_VERSION
        stranded = [
            workflow
            for workflow in DBOS.list_workflows(status="PENDING", load_input=False, load_output=False)
            if workflow.app_version and workflow.app_version != current
        ]
        if not stranded:
            print("no stranded workflows; the queue is not wedged")
            return 0

        for workflow in stranded:
            action = "would cancel" if args.dry_run else "cancelling"
            print(f"{action} {workflow.name} ({workflow.workflow_id[:8]}) left PENDING by version {workflow.app_version}")
            if not args.dry_run:
                DBOS.cancel_workflow(workflow.workflow_id, cancel_children=True)
        if not args.dry_run:
            print(f"cleared {len(stranded)} stranded workflow(s)")
        return 0
    finally:
        DBOS.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
