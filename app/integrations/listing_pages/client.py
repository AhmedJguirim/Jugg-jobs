"""HTTP client for job-posting pages.

Posting pages on the platforms we parse need no login and no cookies: one plain
GET answers (a browser's `cf_clearance` / `__cf_bm` are not sent). Only the
headers a browser sends for a link followed from a search result are.

Pure integration code: it performs the request and returns what came back, and
holds no database or DBOS concepts.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.logging_setup import log

DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
    ),
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# For a platform's own JSON endpoints (the page's script calls them too).
JSON_HEADERS: dict[str, str] = {
    **DEFAULT_HEADERS,
    "Accept": "application/json",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class Page:
    status_code: int
    text: str
    size: int  # bytes on the wire (decoded), before any cutting down
    # Where the request ended up after redirects: a removed Greenhouse job
    # redirects to its board with `?error=true`, and answers 200.
    final_url: str = ""


def fetch_page(url: str, *, timeout: float = DEFAULT_TIMEOUT, json: bool = False) -> Page:
    """GET one page. A 4xx/5xx answer is a result, not an error: the caller
    decides what a 404 means. Raises `httpx.TransportError` when no answer came."""
    log.info("GET %s", url)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, headers=JSON_HEADERS if json else DEFAULT_HEADERS)
    log.info(
        "response status=%s elapsed=%.2fs bytes=%s",
        response.status_code,
        response.elapsed.total_seconds(),
        len(response.content),
    )
    return Page(
        status_code=response.status_code,
        text=response.text,
        size=len(response.content),
        final_url=str(response.url),
    )
