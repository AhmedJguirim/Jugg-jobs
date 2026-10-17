"""Grade stored raw job listings and create real jobs from them.

    python scripts/process_listing.py 2                  # one raw_job_listings id
    python scripts/process_listing.py 1 2 3              # several, one workflow each
    python scripts/process_listing.py --all              # every stored listing
    python scripts/process_listing.py 2 --profile ahmed --force

Each id runs `process_listing_workflow`: detect the platform from the url, parse
the page, grade it against the profile on the local model, and only then create
the job in `jobs` (with its grade). A removed posting ("Job not found") is
reported and creates nothing; so does a listing the model could not grade.
`--force` grades again even when a current grade exists.

Grading goes through the `llm` queue like all model work. This script uses its
own DBOS system database, so do not run it while the app is grading: the
one-GPU limit is enforced per system database.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbos import DBOS, DBOSConfig  # noqa: E402

from app.db.session import session_scope  # noqa: E402
from app.integrations.ollama import health  # noqa: E402
from app.repositories import raw_job_listing as raw_repo  # noqa: E402
from app.workflows import enrichment as flow  # noqa: E402
from app.workflows.listing_grading import process_listing_workflow  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", type=int, help="raw_job_listings ids")
    parser.add_argument("--all", action="store_true", help="process every stored raw listing")
    parser.add_argument("--profile", default=None, help="profile to grade against (default: LLM_DEFAULT_PROFILE)")
    parser.add_argument("--force", action="store_true", help="grade again even if a current grade exists")
    args = parser.parse_args()

    if args.all:
        with session_scope() as session:
            ids = sorted(row.id for row in raw_repo.list_recent(session, limit=100_000))
    else:
        ids = args.ids
    if not ids:
        parser.error("give at least one id, or --all")

    ok, message = health()
    print(message)
    if not ok:
        return 1

    config: DBOSConfig = {
        "name": "job-hunt-listings",
        "system_database_url": os.environ.get("DBOS_SYSTEM_DATABASE_URL"),
        "application_version": flow.APPLICATION_VERSION,
    }
    DBOS(config=config)
    DBOS.launch()
    # After launch: register_queue writes to the system database.
    flow.register_queues()
    try:
        for listing_id in ids:
            result = DBOS.start_workflow(
                process_listing_workflow, listing_id, args.profile, args.force
            ).get_result()
            detail = result.get("reason") or ""
            if result["status"] in ("stored", "up_to_date"):
                detail = f"job #{result['job_id']} {result['title']}"
                if result["status"] == "stored":
                    detail += (
                        f" | fit {result['fit_score']} {result['verdict']} | {result['eligibility']}"
                        + (f" | {'; '.join(result['red_flags'][:3])}" if result["red_flags"] else "")
                    )
            print(f"#{listing_id:<4} {result['status']:<16} {detail}")
    finally:
        DBOS.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
