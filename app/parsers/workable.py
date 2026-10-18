"""Parser for Workable-hosted job pages
(`apply.workable.com/<company>/j/<id>` and `apply.workable.com/j/<id>`).

Input is the page's content as copied from the browser. Workable's CSS classes are
hashed (`styles--2Kv6p`), but its `data-ui` attributes are stable and descriptive,
so this reads only those:

| Page                                           | `JobCreate`                              |
|------------------------------------------------|------------------------------------------|
| `h1[data-ui=job-title]`                        | `title`, `job_titles`                    |
| `data-ui` job-description / requirements / benefits sections | `description` (HTML, formatting only; each section keeps its heading) |
| `job-workplace`, `job-department`, `job-type`, `job-location` | `raw["sections"]`: Location Type, Department, Employment Type, Location |
| `company-logo` image `alt`                     | `company_name`                           |
| `apply-button` link                            | `apply_url`                              |
| pay sentence in the text                       | `salary_min`, `salary_max`, `salary_type` |
| URL                                            | `external_id`, `detail_url`              |

A job can list several locations (cities in several countries); they are joined with
"; " in `raw["sections"]["Location"]` and only a *single* postal location is turned
into a `locations` row.

**Not found is judged first**, because two things look like a job page and are not:
Workable redirects a removed job to the company's careers page (`?not_found=true`),
which says "This job is no longer available", and a copy of that page is a ~400 KB
list of other jobs. Either signal gives `ListingNotFound`. A careers page without
that message is not a removal, it is the wrong page, and is reported as unreadable.

Not hooked to the real flow yet: it is not in `PLATFORM_HOSTS` / `_registry` in
`app/parsers/__init__.py`, so no listing is parsed with it until it is.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlsplit

from app.parsers import ListingNotFound, ListingParseError, ParsedListing, ParseResult
from app.parsers.common import description_html, place_from_text, read_pay
from app.schemas.job import JobCreate
from app.text.dom import Node, parse_html

PLATFORM = "workable"

# Hosts a Workable job page is served from; keep in step with `PLATFORM_HOSTS` when hooked.
HOSTS = ("apply.workable.com",)

# `/<company>/j/<id>` or `/j/<id>`; ids are ten hex characters.
_JOB_ID = re.compile(r"^[0-9A-Fa-f]{10}$")
_GONE_PHRASES = ("this job is no longer available", "job is no longer available", "no longer accepting applications")
# Sections of the posting, in page order, and the label kept for each.
_SECTIONS = ("job-description", "job-requirements", "job-benefits")
# data-ui of a tag under the title -> the label the grader sees (the same labels
# the Ashby and Lever parsers use).
_FACTS = (
    ("job-workplace", "Location Type"),
    ("job-department", "Department"),
    ("job-type", "Employment Type"),
)


def parse(url: str, html: str) -> ParseResult:
    root = parse_html(html)
    title_node = _job_title(root)

    # Judged before the URL is read: a removed job's URL may carry no job id at all
    # (`.../supportyourapp/?not_found=true`).
    if title_node is None:
        reason = _not_found_reason(url, root)
        if reason:
            return ListingNotFound(platform=PLATFORM, reason=reason)
    elif _url_says_not_found(url):
        return ListingNotFound(platform=PLATFORM, reason="Workable's URL says not_found=true")

    base, url_slug, job_id = _split_url(url)
    if title_node is None or not title_node.text():
        if _is_careers_page(root):
            raise ListingParseError("this is the company's careers page, not a job page")
        raise ListingParseError("no job title (h1[data-ui=job-title]): not a Workable job page")

    logo = root.find_by_attr("data-ui", "company-logo")
    company = _company(logo)
    slug = url_slug or _slug_from_logo(logo)

    sections = {}
    for ui, label in _FACTS:
        node = root.find_by_attr("data-ui", ui)
        if node is not None and node.text():
            sections[label] = node.text()
    locations = _locations(root)
    if locations:
        sections["Location"] = "; ".join(locations)

    blocks = [n for ui in _SECTIONS for n in root.find_all_by_attr("data-ui", ui)]
    description = "\n".join(h for h in (description_html(b) for b in blocks) if h) or None
    salary_min, salary_max, salary_type, compensation = read_pay("\n".join(line for b in blocks for line in b.lines()))

    detail_url = f"{base}/{slug}/j/{job_id}" if slug else f"{base}/j/{job_id}"
    title = title_node.text()
    job = JobCreate(
        source=PLATFORM,
        external_id=job_id,
        title=title,
        job_titles=[title],
        description=description,
        # A page that renders the posting is a live posting.
        status="active",
        origin="manual",
        company_name=company,
        # Only a single postal location is a place; "Remote" or several are not.
        location=place_from_text(locations[0]) if len(locations) == 1 else None,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_type=salary_type,
        apply_option="external",
        apply_url=_apply_url(root, base) or f"{detail_url}/apply/",
        detail_url=detail_url,
        raw={
            "platform": PLATFORM,
            "board": slug,
            "job_code": _text(root.find_by_attr("data-ui", "job-code")) or None,
            "sections": sections,
            "compensation": compensation,
        },
    )
    return ParsedListing(platform=PLATFORM, job=job)


# ---- url ----


def _url_says_not_found(url: str) -> bool:
    return "not_found=true" in url.lower()


def _split_url(url: str) -> tuple[str, Optional[str], str]:
    """`https://apply.workable.com/[<company>/]j/<id>[/apply]` -> (base, company, id)."""
    parts = urlsplit(url.strip())
    segments = [s for s in parts.path.split("/") if s]
    if "j" not in segments:
        raise ListingParseError(f"url has no Workable job id: {url!r}")
    at = segments.index("j")
    if at + 1 >= len(segments) or not _JOB_ID.match(segments[at + 1]) or at > 1:
        raise ListingParseError(f"url has no Workable job id: {url!r}")
    return f"{parts.scheme or 'https'}://{parts.netloc}", (segments[0] if at == 1 else None), segments[at + 1]


# ---- page ----


def _text(node: Optional[Node]) -> str:
    return node.text() if node is not None else ""


def _job_title(root: Node) -> Optional[Node]:
    """The job's own title: an h1. The careers page also has `data-ui=job-title`
    on every card of its jobs list, so the tag matters."""
    return next((n for n in root.find_all_by_attr("data-ui", "job-title") if n.tag == "h1"), None)


def _not_found_reason(url: str, root: Node) -> Optional[str]:
    if _url_says_not_found(url):
        return "Workable's URL says not_found=true"
    text = root.text().lower()
    for phrase in _GONE_PHRASES:
        if phrase in text:
            return f"Workable says the job is gone ({phrase!r})"
    return None


def _is_careers_page(root: Node) -> bool:
    """The company's page listing all its jobs (a jobs list, a "Careers at X" heading)."""
    return root.find_by_attr("data-ui", "jobs") is not None or "careers at" in root.text().lower()


def _company(logo: Optional[Node]) -> Optional[str]:
    image = logo.find("img") if logo is not None else None
    return (image.attrs.get("alt") or "").strip() or None if image is not None else None


def _slug_from_logo(logo: Optional[Node]) -> Optional[str]:
    """`/pearltalent/` (the logo's link) -> `pearltalent`, for a job URL with no company."""
    href = (logo.attrs.get("href") or "") if logo is not None else ""
    segments = [s for s in urlsplit(href).path.split("/") if s]
    return segments[0] if segments else None


def _locations(root: Node) -> list[str]:
    """One string per location the job lists ("Mexico City, Mexico"), in order."""
    block = root.find_by_attr("data-ui", "job-location")
    if block is None:
        return []
    found = [n.text().strip().rstrip(",") for n in block.find_all_by_attr("data-ui", "job-location-tooltip")]
    return [loc for loc in dict.fromkeys(found) if loc]


def _apply_url(root: Node, base: str) -> Optional[str]:
    button = root.find_by_attr("data-ui", "apply-button")
    href = (button.attrs.get("href") or "").strip() if button is not None else ""
    return urljoin(base + "/", href) if href else None
