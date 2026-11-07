"""Fetch the HTML of collected job-posting links into `job_listings_test`.

    python scripts/fetch_listings.py                # the next 10 links not fetched yet
    python scripts/fetch_listings.py --limit 50
    python scripts/fetch_listings.py --retry        # also try again the ones that were blocked or failed
    python scripts/fetch_listings.py --include-pasted   # also links you already pasted by hand, to compare
    python scripts/fetch_listings.py --platform workable   # only one platform's links
    python scripts/fetch_listings.py --show 5       # print the stored HTML of row id 5 and stop

Takes links from `manual_job_listing_urls` that have no hand-pasted raw listing
and no row in `job_listings_test`. One request at a time, a random 1-3 second
pause between requests. Every platform with a parser is fetched (lever, ashbyhq, greenhouse, workable, breezy);
a link on any other site is recorded as `unsupported` without a request. Nothing here grades anything or
touches `raw_job_listings`. See docs/adr/0023-fetching-listing-html.md.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import session_scope  # noqa: E402
from app.db.models.job_listing_test import JobListingTest  # noqa: E402
from app.services import listing_fetch  # noqa: E402


def _line(outcome: dict) -> str:
    size = f"{len(outcome['html_body']):>7,} chars of {outcome['page_bytes']:,} bytes" if outcome["page_bytes"] else ""
    note = f"  ({outcome['note']})" if outcome["note"] and outcome["status"] != "fetched" else ""
    return f"{outcome['status']:<11} {size:<34} {outcome['url']}{note}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=10, help="how many links to fetch (default 10)")
    parser.add_argument("--retry", action="store_true", help="also retry blocked / failed / empty ones")
    parser.add_argument("--include-pasted", action="store_true", help="also fetch links that already have a hand-pasted raw listing")
    parser.add_argument("--platform", help="only links of this platform (lever, ashbyhq, greenhouse, workable, ...)")
    parser.add_argument("--show", type=int, metavar="ID", help="print the stored HTML of a job_listings_test row")
    args = parser.parse_args()

    if args.show is not None:
        with session_scope() as session:
            row = session.get(JobListingTest, args.show)
            if row is None:
                print(f"no job_listings_test row {args.show}")
                return 1
            print(row.html_body)
        return 0

    results = listing_fetch.fetch_pending(
        args.limit, retry=args.retry, include_pasted=args.include_pasted,
        platform=args.platform, on_result=lambda o: print(_line(o), flush=True))
    if not results:
        print("nothing to fetch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
