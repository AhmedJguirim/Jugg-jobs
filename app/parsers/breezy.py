"""Parser for Breezy HR job pages (`<company>.breezy.hr/p/<id>-<title-slug>`).

What the saved pages contain decides what this can read. All nine pages saved so
far are **only the description block** (`<div class="description">`): no header, so
no title, company, location or job type in the HTML. What the page does give:

| Where                                   | `JobCreate`                              |
|-----------------------------------------|------------------------------------------|
| the `description` block                 | `description` (HTML, formatting only; every word kept) |
| URL subdomain (`localize.breezy.hr`)    | `company_name`                           |
| URL title slug (`4ba5...-senior-platform-engineer`) | `title`, `job_titles`        |
| URL id (`4ba5bc0d9700`)                 | `external_id`                            |
| pay sentence in the text                | `salary_min`, `salary_max`, `salary_type` |

The company and the title are only slugs in the URL (`inallmedia-llc`,
`senior-full-stack-engineer-tech-lead`). Where the description spells the same
words ("Senior Full-Stack Engineer - Tech Lead", "20four7VA") that spelling is
used; otherwise the slug is capitalised, with common acronyms kept upper-case
(`ai` -> "AI"). `raw["title_source"]` and `raw["company_source"]` say which, so a
guessed name is never mistaken for a read one.

Nothing structured is guessed about location or job type: they are not in these
pages, and the few that appear as free text ("Location: Kyiv, Ukraine - Remote")
stay in the description, which the grader reads. A copy that includes the page's
header would carry more; this parser does not read it because no sample has one.

Registered in `PLATFORM_HOSTS` (as "breezy.hr", which matches every company
subdomain) and `_registry` in `app/parsers/__init__.py`, so a listing on
`<company>.breezy.hr` is graded and turned into a job like the other platforms'.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from app.parsers import ListingNotFound, ListingParseError, ParsedListing, ParseResult
from app.parsers.common import company_from_slug, description_html, read_pay, title_from_slug
from app.schemas.job import JobCreate
from app.text.dom import Node, parse_html

PLATFORM = "breezy"

# Hosts are `<company>.breezy.hr`; `PLATFORM_HOSTS` matches subdomains of "breezy.hr".
HOST_SUFFIX = "breezy.hr"

# `/p/<id>-<title-slug>`: the id is 12 to 16 hex characters.
_PATH = re.compile(r"^/p/([0-9a-f]{10,16})(?:-([^/]+))?/?$", re.IGNORECASE)
_NOT_FOUND_PHRASES = (
    "no longer available",
    "no longer accepting applications",
    "position has been filled",
    "position is closed",
    "no longer open",
    "page you were looking for",
)


def parse(url: str, html: str) -> ParseResult:
    base, company_slug, job_id, title_slug = _split_url(url)
    root = parse_html(html)

    description_node = root.find(cls="description")
    if description_node is None or not description_node.text():
        # A removed position redirects to the company's page; with no description
        # block there is nothing to read, and the wording says which it was.
        reason = _not_found_reason(root)
        if reason:
            return ListingNotFound(platform=PLATFORM, reason=reason)
        raise ListingParseError('no description block (div.description): not a Breezy job page')

    text = " ".join(description_node.lines())
    title, title_source = _title(title_slug, text, description_node)
    company, company_source = company_from_slug(company_slug, text)
    salary_min, salary_max, salary_type, compensation = read_pay("\n".join(description_node.lines()))

    detail_url = f"{base}/p/{job_id}" + (f"-{title_slug}" if title_slug else "")
    job = JobCreate(
        source=PLATFORM,
        external_id=job_id.lower(),
        title=title,
        job_titles=[title],
        description=description_html(description_node),
        # A page that renders the posting is a live posting.
        status="active",
        origin="manual",
        company_name=company,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_type=salary_type,
        apply_option="external",
        apply_url=detail_url,
        detail_url=detail_url,
        raw={
            "platform": PLATFORM,
            "board": company_slug,
            "title_source": title_source,
            "company_source": company_source,
            "sections": {},
            "compensation": compensation,
        },
    )
    return ParsedListing(platform=PLATFORM, job=job)


# ---- url ----


def _split_url(url: str) -> tuple[str, str, str, Optional[str]]:
    """`https://<company>.breezy.hr/p/<id>-<title-slug>` -> (base, company, id, slug)."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if not host.endswith("." + HOST_SUFFIX):
        raise ListingParseError(f"not a company.breezy.hr url: {url!r}")
    match = _PATH.match(parts.path)
    if match is None:
        raise ListingParseError(f"url has no Breezy job id (/p/<id>-<title>): {url!r}")
    company = host[: -len(HOST_SUFFIX) - 1]
    # The host is case-insensitive; the canonical link uses it lower-cased.
    return f"{(parts.scheme or 'https').lower()}://{host}", company, match.group(1), match.group(2)


# ---- page ----


def _not_found_reason(root: Node) -> Optional[str]:
    text = root.text().lower()
    for phrase in _NOT_FOUND_PHRASES:
        if phrase in text:
            return f"Breezy says the position is gone ({phrase!r})"
    return None


def _title(slug: Optional[str], text: str, description: Node) -> tuple[str, str]:
    """(title, source). The URL slug is the only title these pages have; when the
    description spells the same words, that spelling is used. A URL with no slug
    falls back to the description's first heading, and failing that is an error."""
    if slug:
        return title_from_slug(slug, text)
    heading = next((h for h in description.iter() if h.tag in ("h1", "h2", "h3") and h.text()), None)
    if heading is None:
        raise ListingParseError("no title: the url has no title slug and the description has no heading")
    return heading.text(), "text"
