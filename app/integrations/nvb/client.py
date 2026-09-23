"""HTTP client for the Nationale Vacaturebank jobs API.

Pure integration code: it performs the request and returns the decoded payload.
It holds no DBOS concepts, so it can be called from a workflow step, a script,
or a test without pulling in the durable-execution runtime.

The browser-like headers are required - the API rejects or degrades requests
without a plausible User-Agent and Referer.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.logging_setup import log

BASE_URL = "https://api.nationalevacaturebank.nl/api/jobs/v3/sites/nationalevacaturebank.nl/jobs"

DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nationalevacaturebank.nl/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

DEFAULT_TIMEOUT = 30.0


def fetch_jobs_page(
    query: str,
    *,
    page: int = 1,
    limit: int = 10,
    sort: str = "date",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fetch one page of vacancies and return the raw decoded JSON.

    Returns the raw dict rather than a parsed model so callers that checkpoint
    the result (DBOS steps) store plain JSON. Parse with
    `app.integrations.nvb.schema.JobSearchResponse` when you want typed access.

    Raises `httpx.HTTPStatusError` on a non-2xx response.
    """
    params = {"page": page, "limit": limit, "sort": sort, "query": query}
    log.info("GET %s params=%s", BASE_URL, params)

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(BASE_URL, params=params, headers=DEFAULT_HEADERS)

    log.info(
        "response status=%s elapsed=%.2fs bytes=%s",
        response.status_code,
        response.elapsed.total_seconds(),
        len(response.content),
    )
    response.raise_for_status()
    return response.json()
