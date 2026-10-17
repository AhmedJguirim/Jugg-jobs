"""Parse stored raw job listings into `jobs_test`.

    python scripts/process_listing.py 2          # one raw_job_listings id
    python scripts/process_listing.py 1 2 3      # several, one workflow each
    python scripts/process_listing.py --all      # every listing not yet stored

Each id runs `process_raw_listing_workflow`: detect the platform from the url,
parse the page, upsert the job into `jobs_test` (never into `jobs`). A listing
the platform reports as removed ("Job not found") is reported and stores nothing.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbos import DBOS, DBOSConfig  # noqa: E402

from app.db.session import session_scope  # noqa: E402
from app.repositories import raw_job_listing as raw_repo  # noqa: E402
from app.workflows.listing_processing import process_raw_listing_workflow  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", type=int, help="raw_job_listings ids")
    parser.add_argument("--all", action="store_true", help="process every stored raw listing")
    args = parser.parse_args()

    if args.all:
        with session_scope() as session:
            ids = sorted(row.id for row in raw_repo.list_recent(session, limit=100_000))
    else:
        ids = args.ids
    if not ids:
        parser.error("give at least one id, or --all")

    config: DBOSConfig = {
        "name": "job-hunt-listings",
        "system_database_url": os.environ.get("DBOS_SYSTEM_DATABASE_URL"),
    }
    DBOS(config=config)
    DBOS.launch()
    try:
        for listing_id in ids:
            result = DBOS.start_workflow(process_raw_listing_workflow, listing_id).get_result()
            detail = result.get("title") or result.get("reason") or ""
            created = " (new)" if result.get("created") else " (updated)" if result["status"] == "stored" else ""
            print(f"#{listing_id:<4} {result['status']:<12}{created} {detail}")
    finally:
        DBOS.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
