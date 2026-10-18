"""Parser for Lever-hosted job pages (`jobs.lever.co/<company>/<job id>`).

Input is the page's job content as copied from the browser. Lever's classes are
plain and stable (`posting-headline`, `posting-category`, `data-qa=...`):

| Page                                   | `JobCreate`                              |
|----------------------------------------|------------------------------------------|
| h2 in `posting-headline`               | `title`, `job_titles`                    |
| `posting-category` tags                | `raw["sections"]`: Location, Department, Employment Type, Location Type |
| the description + titled sections      | `description` (HTML, formatting only)    |
| the "apply" button                     | `apply_url`                              |
| pay sentence in the description        | `salary_min`, `salary_max`, `salary_type` |
| URL                                    | `external_id`, `detail_url`, company slug |
| a place, if "City, Region, Country"    | `location` (Lever usually gives only a country, which is not a place row) |

**The copied page has no company name.** Only the URL slug (`insiderone`) says whose
job it is, so the name is recovered from the page text: a capitalised phrase that
spells the slug ("Insider One"). When none does, the slug is capitalised
("Jobgether"). Nothing else is guessed.

**What is left out of the description:** the AI-use disclaimer and the repeated
"apply" button, which are page furniture and not part of the posting. Pay is read
only from a sentence about pay (see `common.read_pay`), so "a $500M Series E" in an
"About us" paragraph is not a salary.

Registered in `PLATFORM_HOSTS` / `_registry` (`app/parsers/__init__.py`), so a
listing on one of `HOSTS` is graded and turned into a job like an Ashby one.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote, urlsplit

from app.parsers import ListingNotFound, ListingParseError, ParsedListing, ParseResult
from app.parsers.common import company_from_slug, description_html, place_from_text, read_pay
from app.schemas.job import JobCreate
from app.text.dom import Node, parse_html

PLATFORM = "lever"

# Hosts a Lever job page is served from; keep in step with `PLATFORM_HOSTS`.
HOSTS = ("jobs.lever.co", "jobs.eu.lever.co")

_JOB_ID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_NOT_FOUND_PHRASES = (
    "couldn't find anything here",
    "could not find anything here",
    "no longer open",
    "job you're looking for",
    "job you are looking for",
    "page you were looking for",
)
# Sections of the page that are not part of the posting.
_FURNITURE = {"ai-disclaimer", "btn-apply-bottom"}
# class on a `posting-category` -> the label the grader sees (the same ones the
# Ashby parser uses, so the grader reads every platform alike).
_CATEGORIES = (
    ("location", "Location"),
    ("department", "Department"),
    ("commitment", "Employment Type"),
    ("workplaceTypes", "Location Type"),
)


def parse(url: str, html: str) -> ParseResult:
    base, company_slug, job_id = _split_url(url)
    root = parse_html(html)

    title_node = _headline(root)
    # Checked first, like the other parsers: a removed posting is a result, not an error.
    if title_node is None or not title_node.text():
        reason = _not_found_reason(root)
        if reason:
            return ListingNotFound(platform=PLATFORM, reason=reason)
        raise ListingParseError("no job title (h2 in posting-headline): not a Lever job page")

    sections = _categories(root)
    blocks = _description_blocks(root)
    description = "\n".join(h for h in (description_html(b) for b in blocks) if h) or None
    text = " ".join(b.text() for b in blocks)
    pay_text = "\n".join(line for b in blocks for line in b.lines())

    salary_min, salary_max, salary_type, compensation = read_pay(pay_text)

    title = title_node.text()
    detail_url = f"{base}/{company_slug}/{job_id}"
    job = JobCreate(
        source=PLATFORM,
        external_id=job_id,
        title=title,
        job_titles=[title],
        description=description,
        # A page that renders the posting is a live posting.
        status="active",
        origin="manual",
        company_name=_company_name(company_slug, text),
        location=place_from_text(sections.get("Location", "")),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_type=salary_type,
        apply_option="external",
        apply_url=_apply_url(root) or f"{detail_url}/apply",
        detail_url=detail_url,
        raw={
            "platform": PLATFORM,
            "board": company_slug,
            "sections": sections,
            "compensation": compensation,
        },
    )
    return ParsedListing(platform=PLATFORM, job=job)


# ---- url ----


def _split_url(url: str) -> tuple[str, str, str]:
    """`https://jobs.lever.co/<company>/<job id>[/apply]` -> (base, company, id)."""
    parts = urlsplit(url.strip())
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or not _JOB_ID.match(segments[1]):
        raise ListingParseError(f"url has no Lever job id: {url!r}")
    return f"{parts.scheme or 'https'}://{parts.netloc}", unquote(segments[0]), segments[1].lower()


# ---- page ----


def _headline(root: Node) -> Optional[Node]:
    header = root.find(cls="posting-headline")
    return header.find("h2") if header is not None else None


def _not_found_reason(root: Node) -> Optional[str]:
    text = root.text().lower()
    for phrase in _NOT_FOUND_PHRASES:
        if phrase in text:
            return f"Lever says the job is gone ({phrase!r})"
    return None


def _categories(root: Node) -> dict[str, str]:
    """The tags under the title: location, team, commitment, workplace type."""
    found: dict[str, str] = {}
    for node in root.find_all(cls="posting-category"):
        # Lever writes "Full-time /": the slash is a separator, not part of the value.
        value = re.sub(r"\s*/\s*$", "", node.text()).strip()
        if not value:
            continue
        for css, label in _CATEGORIES:
            if css in node.classes and label not in found:
                found[label] = value
    return found


def _description_blocks(root: Node) -> list[Node]:
    """The posting's own sections: the opening description, then each titled
    section (accountabilities, requirements, benefits...)."""
    opening = next((n for n in root.iter() if n.attrs.get("data-qa") == "job-description"), None)
    if opening is None or opening.parent is None:
        return []
    return [
        child
        for child in opening.parent.children
        if isinstance(child, Node)
        and "section" in child.classes
        and child.attrs.get("data-qa") not in _FURNITURE
    ]


def _apply_url(root: Node) -> Optional[str]:
    button = root.find(cls="postings-btn")
    href = (button.attrs.get("href") or "").strip() if button is not None else ""
    return href if href.startswith(("http://", "https://")) else None


def _company_name(slug: str, text: str) -> str:
    """The company's own spelling of its URL slug, found in the page text (see
    `common.company_from_slug`): "insiderone" -> "Insider One"; else the slug."""
    return company_from_slug(slug, text)[0]
