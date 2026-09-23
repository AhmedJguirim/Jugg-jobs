"""Ingest every page of a job-board search into the database.

Operational script: run by a human or a scheduler, not imported by the app.

    python scripts/ingest_jobs.py "Odido"
    python scripts/ingest_jobs.py "Odido" --limit 25 --max-pages 2
    python scripts/ingest_jobs.py "python developer" "data engineer"

Safe to re-run: vacancies are keyed on (source, external_id), so a second run
updates the rows it already has instead of duplicating them.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.job_ingest import DEFAULT_LIMIT, ingest_query  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("queries", nargs="+", help="one or more search queries")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="page size")
    parser.add_argument("--max-pages", type=int, default=None, help="stop after N pages")
    parser.add_argument("--sort", default="date", help="sort order (default: date)")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="seconds to pause between page requests (default: 0.3)",
    )
    args = parser.parse_args(argv)

    failed = False
    for query in args.queries:
        stats = ingest_query(
            query,
            limit=args.limit,
            max_pages=args.max_pages,
            sort=args.sort,
            delay_seconds=args.delay,
        )
        print(stats.summary())
        failed = failed or not stats.ok

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
