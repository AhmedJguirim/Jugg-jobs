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

Not hooked to the real flow yet: it is not in `PLATFORM_HOSTS` / `_registry` in
`app/parsers/__init__.py`, so no listing is parsed with it until it is.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from app.parsers import ListingNotFound, ListingParseError, ParsedListing, ParseResult
from app.parsers.common import description_html, parse_compensation
from app.schemas.job import JobCreate, LocationBase
from app.text.dom import Node, parse_html

PLATFORM = "greenhouse"

# Hosts a Greenhouse job page can be served from (for `detect_platform`, once hooked).
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

# A sentence is about pay when it says so, and not about pay when it is about
# something else that has dollar amounts in it.
_PAY_WORDS = (
    "pay range", "salary", "base pay", "base compensation", "compensation range",
    "pay between", "annual base", "hourly rate", "base salary", "pay for this",
    "salary range", "annualized",
)
_NOT_PAY_WORDS = ("equity", "stock", "bonus", "espp", "rsu", "stipend", "refresh", "commission")
_CURRENCY_CODE = re.compile(r"\b(USD|CAD|EUR|GBP|AUD|NZD|CHF|PLN|SEK|NOK|DKK|INR|SGD|JPY|BRL|MXN)\b")
# Symbols stand in for a code only when no code is written. "$" is read as US
# dollars: a bare "$" in a posting is far more often USD than CAD/AUD, which
# say so ("CAD $ 164,490").
_CURRENCY_SYMBOLS = (("zł", "PLN"), ("€", "EUR"), ("£", "GBP"), ("$", "USD"))
_MAX_SENTENCE = 300
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")


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
    sentences = _pay_sentences(description_node)

    salary_min = salary_max = salary_type = None
    compensation = None
    if sentences:
        salary_min, salary_max, salary_type = parse_compensation(sentences[0])
        if salary_min is not None:
            currency, source = _currency(sentences[0])
            compensation = {"text": sentences[0][:_MAX_SENTENCE], "currency": currency, "currency_source": source}

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
        location=_location(location_text),
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


def _pay_sentences(description: Optional[Node]) -> list[str]:
    """Sentences of the description that state pay, in order."""
    if description is None:
        return []
    found = []
    for sentence in _SENTENCE_BREAK.split(description.text()):
        lowered = sentence.lower()
        if any(w in lowered for w in _PAY_WORDS) and not any(w in lowered for w in _NOT_PAY_WORDS):
            if re.search(r"\d", sentence):
                found.append(sentence.strip())
    return found


def _currency(sentence: str) -> tuple[Optional[str], Optional[str]]:
    """(code, how we know): "code" when written, "symbol" when inferred from one."""
    code = _CURRENCY_CODE.search(sentence)
    if code:
        return code.group(1), "code"
    for symbol, inferred in _CURRENCY_SYMBOLS:
        if symbol in sentence:
            return inferred, "symbol"
    return None, None


def _location(text: str) -> Optional[LocationBase]:
    """A place, only when the text is a postal "City, Region, Country".

    "Remote Poland", "Canada (Remote)", "Multiple locations" are not places and
    are left in `raw["sections"]` rather than put in the cities table.
    """
    if not text or re.search(r"remote|hybrid|anywhere|multiple|various|worldwide", text, re.IGNORECASE):
        return None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) >= 3:
        return LocationBase(city=parts[0], province=parts[1], country_name=parts[-1])
    if len(parts) == 2:
        # "Austin, TX" is a state; "Berlin, Germany" is a country.
        if re.fullmatch(r"[A-Z]{2}", parts[1]):
            return LocationBase(city=parts[0], province=parts[1])
        return LocationBase(city=parts[0], country_name=parts[1])
    return None
