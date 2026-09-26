"""Translate and grade ingested vacancies on the local LLM.

Operational script. It does NOT call the model directly: work is enqueued on the
DBOS `llm` queue, which allows one job at a time, so the GPU is never asked to
run two models at once even if you start this twice by mistake.

    python scripts/enrich_jobs.py                     # everything pending
    python scripts/enrich_jobs.py --limit 5           # just a few
    python scripts/enrich_jobs.py --job-id 42         # one vacancy
    python scripts/enrich_jobs.py --force             # redo even if current
    python scripts/enrich_jobs.py --progress          # report, no work
    python scripts/enrich_jobs.py --shortlist         # show the best matches
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbos import DBOS, DBOSConfig  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.integrations.ollama import health  # noqa: E402
from app.repositories import enrichment as enrichment_repo  # noqa: E402
from app.workflows import enrichment as flow  # noqa: E402


def _print_progress() -> None:
    with session_scope() as session:
        stats = enrichment_repo.enrichment_progress(session)
    print(
        f"vacancies with a description : {stats['jobs_with_description']}\n"
        f"translated                   : {stats['translated']} "
        f"({stats['translation_pending']} pending)\n"
        f"graded                       : {stats['assessed']} "
        f"({stats['assessment_pending']} pending)\n"
        f"GPU time spent               : {stats['gpu_seconds_spent']}s"
    )


def _print_shortlist(profile: str, min_score: int, include_blocked: bool, limit: int) -> None:
    with session_scope() as session:
        rows = enrichment_repo.shortlist(
            session,
            profile_name=profile,
            min_score=min_score,
            include_blocked=include_blocked,
            limit=limit,
        )
        if not rows:
            print("nothing scored above the threshold yet")
            return
        for job, assessment, translation in rows:
            title = (translation.title if translation else None) or job.title
            flags = f"  [{'; '.join(assessment.red_flags)}]" if assessment.red_flags else ""
            blocked = " BLOCKED" if assessment.blocked else ""
            print(f"\n{assessment.fit_score:3d}  {assessment.verdict:<13}{blocked}  {title}")
            print(f"     {job.company.name if job.company else '?'} - {job.city or '?'}")
            if assessment.summary:
                print(f"     {assessment.summary}")
            if flags:
                print(f"    {flags}")
            if job.detail_url:
                print(f"     {job.detail_url}")


def _clear_stuck() -> int:
    """Cancel workflows stranded PENDING by a previous application version.

    Why this is needed: the `llm` queue allows one job at a time, and DBOS only
    recovers workflows belonging to the *current* application version. Bump the
    version while a job is running - which is exactly what you must do when a
    workflow's steps change - and that job stays PENDING forever while still
    counting against the concurrency limit. The queue then looks busy and never
    dequeues anything again.

    Observed: one enrich_job_workflow left PENDING at version 0.1.0 held the
    only slot while 91 jobs waited behind it.
    """
    config: DBOSConfig = {
        "name": "job-hunt-enrich",
        "system_database_url": os.environ.get("DBOS_SYSTEM_DATABASE_URL"),
        "application_version": "0.2.0",
    }
    DBOS(config=config)
    DBOS.launch()
    try:
        current = "0.2.0"
        stranded = [
            workflow
            for workflow in DBOS.list_workflows(
                status="PENDING", load_input=False, load_output=False
            )
            if workflow.app_version and workflow.app_version != current
        ]
        if not stranded:
            print("no stranded workflows; the queue is not wedged")
            return 0

        for workflow in stranded:
            print(
                f"cancelling {workflow.name} ({workflow.workflow_id[:8]}) "
                f"left PENDING by version {workflow.app_version}"
            )
            DBOS.cancel_workflow(workflow.workflow_id, cancel_children=True)
        print(f"cleared {len(stranded)} stranded workflow(s)")
        return 0
    finally:
        DBOS.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--job-id", type=int, default=None, help="enrich one vacancy")
    parser.add_argument("--profile", default=None, help="scoring profile name")
    parser.add_argument("--target-language", default="en")
    parser.add_argument("--force", action="store_true", help="redo work already current")
    parser.add_argument("--progress", action="store_true", help="report and exit")
    parser.add_argument("--shortlist", action="store_true", help="print best matches and exit")
    parser.add_argument("--min-score", type=int, default=55)
    parser.add_argument("--include-blocked", action="store_true")
    parser.add_argument(
        "--clear-stuck",
        action="store_true",
        help="cancel workflows left PENDING by an older app version, which can "
             "wedge the single-slot llm queue",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    profile = args.profile or settings.llm_default_profile

    if args.clear_stuck:
        return _clear_stuck()

    if args.progress:
        _print_progress()
        return 0
    if args.shortlist:
        _print_shortlist(profile, args.min_score, args.include_blocked, args.limit)
        return 0

    # Fail fast on a missing model rather than after the first queued job.
    ok, message = health()
    print(message)
    if not ok:
        return 1

    config: DBOSConfig = {
        "name": "job-hunt-enrich",
        "system_database_url": os.environ.get("DBOS_SYSTEM_DATABASE_URL"),
        # Keep in step with main.py: see the note there on why this is bumped
        # when a workflow's step sequence changes.
        "application_version": "0.2.0",
    }
    DBOS(config=config)
    DBOS.launch()
    # After launch: register_queue writes to the system database (see its docstring).
    flow.register_queues()

    try:
        if args.job_id:
            handle = DBOS.enqueue_workflow(
                flow.LLM_QUEUE,
                flow.enrich_job_workflow,
                args.job_id,
                args.target_language,
                profile,
                args.force,
            )
            print(handle.get_result())
        else:
            handle = DBOS.start_workflow(
                flow.enrich_pending_workflow,
                args.limit,
                args.target_language,
                profile,
                args.force,
            )
            summary = handle.get_result()
            print(
                f"queued {summary['queued']}, enriched {summary['enriched']}, "
                f"already current {summary['up_to_date']}, failed {summary['failed']}, "
                f"GPU {summary['gpu_seconds']}s"
            )
        print()
        _print_progress()
    finally:
        DBOS.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
