"""Turn a pasted job-listing page into a `JobCreate`.

One parser per job platform, chosen from the listing's URL. A parser is pure -
HTML and URL in, result out - so it never touches the database; storing the
result is the caller's job (`app/services/listing_processing.py`).

A parser returns one of two things, or raises:

- `ParsedListing` - the job, as the same `JobCreate` the NVB ingest produces.
- `ListingNotFound` - the platform rendered its "this job does not exist" page.
  Common (postings are taken down), so it is a result, not an error, and it is
  checked before anything else is read from the page.
- `ListingParseError` - the HTML is not what that platform's job page looks like
  (an empty paste, a different page, a layout change).

To add a platform: write `app/parsers/<name>.py` exposing `PLATFORM` and
`parse(url, html)`, and register it in `PARSERS` below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union
from urllib.parse import urlsplit

from app.schemas.job import JobCreate


class ListingParseError(Exception):
    """The HTML is not a job page this parser understands."""


@dataclass(frozen=True)
class ListingNotFound:
    platform: str
    reason: str


@dataclass(frozen=True)
class ParsedListing:
    platform: str
    job: JobCreate


ParseResult = Union[ParsedListing, ListingNotFound]

# Host (without "www.") -> platform name. A subdomain matches its parent:
# `jobs.ashbyhq.com` is listed, `foo.jobs.ashbyhq.com` would match too.
PLATFORM_HOSTS = {
    "jobs.ashbyhq.com": "ashbyhq",
    "job-boards.greenhouse.io": "greenhouse",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.eu.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever",
    "apply.workable.com": "workable",
}


def detect_platform(url: str) -> Optional[str]:
    """Which platform a listing URL belongs to, or None if unsupported."""
    host = (urlsplit(url.strip()).hostname or "").lower()
    host = host.removeprefix("www.")
    for known, platform in PLATFORM_HOSTS.items():
        if host == known or host.endswith("." + known):
            return platform
    return None


def _registry() -> dict[str, Callable[[str, str], ParseResult]]:
    # Imported here so the modules can import the types above without a cycle.
    from app.parsers import ashbyhq, greenhouse, lever, workable

    return {
        ashbyhq.PLATFORM: ashbyhq.parse,
        greenhouse.PLATFORM: greenhouse.parse,
        lever.PLATFORM: lever.parse,
        workable.PLATFORM: workable.parse,
    }


def parse_listing(platform: str, url: str, html: str) -> ParseResult:
    parser = _registry().get(platform)
    if parser is None:
        raise ListingParseError(f"no parser for platform {platform!r}")
    return parser(url, html)
