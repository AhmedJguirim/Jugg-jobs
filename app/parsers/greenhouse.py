"""Parser for Greenhouse-hosted job pages
(`job-boards.greenhouse.io/<board>/jobs/<id>`, also `boards.greenhouse.io/...`).

Input is the page's job content as copied from the browser. Greenhouse's classes
here are plain and human-named (`job__title`, `job__location`,
`job__description`), unlike Ashby's hashed ones. The copied fragment carries no
header, so the company is read from the "Interested in building your career at
<Company>?" line of the job-alert box that closes the page.

| Page                               | `JobCreate`                              |
|------------------------------------|------------------------------------------|
| `job__title` h1                    | `title`, `job_titles`                    |
| `job__description`                 | `description` (HTML, formatting only)    |
| "career at X?" in the alert box    | `company_name`                           |
| `job__location`                    | `raw["sections"]["Location"]`; `location` when it is a postal "City, Region, Country" |
| pay sentence in the description    | `salary_min`, `salary_max`, `salary_type` |
| URL                                | `external_id`, `detail_url`, `apply_url` |

Pay is read only from a sentence that is about pay ("pay range", "base salary",
"compensation range"...) and is not about equity, bonus or a stipend: one sampled
posting lists a "New hire equity: $24,000 - $36,000" and no salary at all, and
must not come out with a $24k salary. Nothing is guessed; a missing fact stays
empty.

Registered in `PLATFORM_HOSTS` / `_registry` (`app/parsers/__init__.py`), so a
listing on one of `HOSTS` is graded and turned into a job like an Ashby one.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from app.parsers import ListingNotFound, ListingParseError, ParsedListing, ParseResult
from app.parsers.common import description_html, place_from_text, read_pay
from app.schemas.job import JobCreate
from app.text.dom import Node, parse_html

PLATFORM = "greenhouse"

# Hosts a Greenhouse job page can be served from; keep in step with `PLATFORM_HOSTS`.
HOSTS = ("job-boards.greenhouse.io", "boards.greenhouse.io", "job-boards.eu.greenhouse.io")

# `/<board>/jobs/<id>`; ids are digits, sometimes with a suffix ("6111473004-98").
_JOB_ID = re.compile(r"^\d+(?:-[\w-]+)?$")
_COMPANY = re.compile(r"career at (.+?)\?", re.IGNORECASE)
_NOT_FOUND_PHRASES = (
    "no longer open",
    "job you are looking for",
    "job you're looking for",
    "this job is no longer",
    "page you were looking for",
)


def parse(url: str, html: str) -> ParseResult:
    base, board, job_id = _split_url(url)
    root = parse_html(html)

    title_node = root.find("h1", "section-header") or next(iter(root.find_all("h1")), None)
    # Checked first, like Ashby's: a removed posting is a result, not an error.
    reason = _not_found_reason(url, root, has_title=bool(title_node and title_node.text()))
    if reason:
        return ListingNotFound(platform=PLATFORM, reason=reason)
    if title_node is None or not title_node.text():
        raise ListingParseError("no job title (h1.section-header): not a Greenhouse job page")

    description_node = root.find(cls="job__description")
    location_text = _text_of(root.find(cls="job__location"))
    company = _company(root)
    salary_min, salary_max, salary_type, compensation = read_pay("\n".join(description_node.lines()) if description_node is not None else "")

    title = title_node.text()
    detail_url = f"{base}/{board}/jobs/{job_id}"
    sections = {"Location": location_text} if location_text else {}
    job = JobCreate(
        source=PLATFORM,
        external_id=job_id,
        title=title,
        job_titles=[title],
        description=description_html(description_node),
        # A page that renders the posting is a live posting.
        status="active",
        origin="manual",
        company_name=company,
        location=place_from_text(location_text),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_type=salary_type,
        apply_option="external",
        apply_url=detail_url,
        detail_url=detail_url,
        raw={
            "platform": PLATFORM,
            "board": board,
            "tags": [t.text() for t in root.find_all(cls="tag-text") if t.text()],
            "sections": sections,
            "compensation": compensation,
        },
    )
    return ParsedListing(platform=PLATFORM, job=job)


# ---- url ----


def _split_url(url: str) -> tuple[str, str, str]:
    """`https://job-boards.greenhouse.io/<board>/jobs/<id>` -> (base, board, id)."""
    parts = urlsplit(url.strip())
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 3 or segments[1] != "jobs" or not _JOB_ID.match(segments[2]):
        raise ListingParseError(f"url has no Greenhouse job id: {url!r}")
    return f"{parts.scheme or 'https'}://{parts.netloc}", segments[0], segments[2]


# ---- page ----


def _text_of(node: Optional[Node]) -> str:
    return node.text() if node is not None else ""


def _not_found_reason(url: str, root: Node, *, has_title: bool) -> Optional[str]:
    """Greenhouse sends a removed job to its board with an error, or shows a
    "no longer open" message. Only judged when there is no job title: a real job
    whose text happens to say "no longer open" is still a job."""
    if has_title:
        return None
    if "error=true" in url.lower():
        return "Greenhouse redirected to its board with an error"
    text = root.text().lower()
    for phrase in _NOT_FOUND_PHRASES:
        if phrase in text:
            return f"Greenhouse says the job is gone ({phrase!r})"
    return None


def _company(root: Node) -> Optional[str]:
    alert = root.find(cls="job-alert")
    match = _COMPANY.search(alert.text()) if alert is not None else None
    return match.group(1).strip() if match else None
