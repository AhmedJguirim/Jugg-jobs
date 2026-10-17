"""Parser for Ashby-hosted job pages (`jobs.ashbyhq.com/<org>/<job id>`).

The input is the rendered page as copied from the browser, not the server's
HTML shell (Ashby is a single-page app, so the shell holds no job). The markup
uses hashed CSS-module classes (`_title_dea4p_33`) that change between deploys,
so this only relies on Ashby's stable, human-named classes - `ashby-job-posting-*`
- and on the headings' text.

What a page gives us, and where it goes:

| Page                              | `JobCreate`                              |
|-----------------------------------|------------------------------------------|
| h1 `ashby-job-posting-heading`    | `title`, `job_titles`                    |
| description container             | `description` (HTML, styling stripped)   |
| logo `alt` / logo link            | `company_name`, `company_website`        |
| "Address" ("City, Region")        | `location` (city, province)              |
| "Compensation" ("EUR 55K - 72K")  | `salary_min`, `salary_max`, `salary_type`|
| URL                               | `external_id`, `detail_url`, `apply_url` |
| everything else in the left pane  | `raw["sections"]` (employment type, location type, department, ...) |

Nothing is guessed: a field the page does not carry stays empty, and the full
left pane is always kept in `raw` so a later step can read what has no column.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from app.parsers import ListingNotFound, ListingParseError, ParsedListing, ParseResult
from app.parsers.common import description_html, parse_compensation
from app.schemas.job import JobCreate, LocationBase
from app.text.dom import Node, parse_html

PLATFORM = "ashbyhq"
BASE_URL = "https://jobs.ashbyhq.com"

_JOB_ID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_NOT_FOUND_TITLES = {"job not found"}


def parse(url: str, html: str) -> ParseResult:
    org, job_id = _split_url(url)
    root = parse_html(html)

    # Checked first: a removed posting renders a normal Ashby page (header,
    # footer, "View all open positions") that must not be mistaken for a job.
    not_found = _not_found_reason(root)
    if not_found:
        return ListingNotFound(platform=PLATFORM, reason=not_found)

    heading = root.find("h1", "ashby-job-posting-heading")
    if heading is None or not heading.text():
        raise ListingParseError("no job heading (ashby-job-posting-heading): not an Ashby job page")

    sections = _left_pane_sections(root)
    company_name, company_website, logo_url = _company(root)
    description = _description(root)

    salary_min = salary_max = salary_type = None
    compensation = sections.get("Compensation")
    if compensation:
        salary_min, salary_max, salary_type = parse_compensation(compensation)

    detail_url = f"{BASE_URL}/{org}/{job_id}"
    title = heading.text()
    job = JobCreate(
        source=PLATFORM,
        external_id=job_id,
        title=title,
        job_titles=[title],
        description=description,
        # A page that renders the posting is a live posting.
        status="active",
        origin="manual",
        company_name=company_name or None,
        company_website=company_website,
        location=_location(sections),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_type=salary_type,
        apply_option="external",
        apply_url=f"{detail_url}/application",
        detail_url=detail_url,
        raw={
            "platform": PLATFORM,
            "org": org,
            "logo_url": logo_url,
            "sections": sections,
        },
    )
    return ParsedListing(platform=PLATFORM, job=job)


# ---- url ----


def _split_url(url: str) -> tuple[str, str]:
    """`https://jobs.ashbyhq.com/<org>/<job id>[/application]` -> (org, job id)."""
    segments = [s for s in urlsplit(url.strip()).path.split("/") if s]
    if len(segments) < 2 or not _JOB_ID.match(segments[1]):
        raise ListingParseError(f"url has no Ashby job id: {url!r}")
    return segments[0], segments[1].lower()


# ---- page sections ----


def _not_found_reason(root: Node) -> Optional[str]:
    for h1 in root.find_all("h1"):
        if h1.text().lower() in _NOT_FOUND_TITLES:
            return "Ashby shows 'Job not found'"
    if root.find_class_prefix("_notFoundContent"):
        return "Ashby shows its not-found content"
    return None


def _left_pane_sections(root: Node) -> dict[str, str]:
    """Label -> value for the facts beside the description (Location, Department...)."""
    pane = root.find(cls="ashby-job-posting-left-pane")
    sections: dict[str, str] = {}
    if pane is None:
        return sections
    for block in pane.find_all("div"):
        heading = next((c for c in block.children if isinstance(c, Node) and c.tag == "h2"), None)
        if heading is None:
            continue
        values = [p.text() for p in block.find_all("p") if p.text()]
        sections[heading.text()] = "; ".join(values)
    return sections


def _company(root: Node) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(name, website, logo url) from the page header."""
    header = root.find(cls="ashby-job-posting-header")
    if header is None:
        return None, None, None
    logo = header.find("img")
    if logo is None:
        return None, None, None
    name = (logo.attrs.get("alt") or "").strip() or None
    website = None
    link = logo.parent
    while link is not None and link.tag != "a":
        link = link.parent
    if link is not None:
        href = (link.attrs.get("href") or "").strip()
        host = (urlsplit(href).hostname or "").lower()
        if href.startswith(("http://", "https://")) and "ashbyhq.com" not in host:
            website = href
    return name, website, logo.attrs.get("src")


def _description(root: Node) -> Optional[str]:
    return description_html(root.find(cls="ashby-job-posting-description"))


def _location(sections: dict[str, str]) -> Optional[LocationBase]:
    """A place, only when the page gives a postal "Address" ("City, Region").

    "Location" is free text ("EU Remote", "Warsaw", "EU Remote; London") and is
    not turned into a place: it would put "EU Remote" in the cities table. It
    stays available in `raw["sections"]`.
    """
    address = sections.get("Address")
    if not address:
        return None
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts:
        return None
    city = parts[0]
    province = parts[1] if len(parts) > 1 and parts[1].lower() != city.lower() else None
    return LocationBase(city=city, province=province)
