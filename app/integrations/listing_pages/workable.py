"""Workable: rebuild the rendered posting from the data behind the page.

Workable's job page is a single-page app: the response is an ~8 KB shell with a
loading animation. The script behind it calls public JSON endpoints, and so does
this:

- `/api/v2/accounts/<company>/jobs/<id>` - title, locations, department, type,
  workplace, code, and the description / requirements / benefits HTML.
  A removed job answers 404.
- `/api/v1/accounts/<company>` - the company's name and logo, which the job
  record does not carry. Asked once per company per process.

A link without a company (`apply.workable.com/j/<id>`) redirects to the one with
it; the company is read from where the redirect lands.

The Workable parser reads `data-ui` attributes, so those are what is written,
with the labels and headings the page shows ("Full time", "Description", each
section's body in a `div`). The
root carries `data-synthetic="api"`.
"""

from __future__ import annotations

import json
import re
from html import escape
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from app.integrations.listing_pages.client import Page

BASE = "https://apply.workable.com"
_ID = re.compile(r"^[0-9A-Fa-f]{10}$")

# Workable's enum values -> the words its page shows.
_TYPES = {"full": "Full time", "part": "Part time", "contract": "Contract", "temp": "Temporary", "other": "Other"}
_WORKPLACE = {"remote": "Remote", "hybrid": "Hybrid", "on_site": "On-site", "onsite": "On-site"}

Getter = Callable[..., Page]

# company slug -> account record ({} when it could not be read); per process.
_accounts: dict[str, dict[str, Any]] = {}


def split_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """`/<company>/j/<id>[/apply]` or `/j/<id>` -> (company or None, id or None)."""
    segments = [s for s in urlsplit(url.strip()).path.split("/") if s]
    if "j" not in segments:
        return None, None
    at = segments.index("j")
    if at > 1 or at + 1 >= len(segments) or not _ID.match(segments[at + 1]):
        return None, None
    return (segments[0] if at == 1 else None), segments[at + 1]


def api_url(company: str, job_id: str) -> str:
    return f"{BASE}/api/v2/accounts/{company}/jobs/{job_id}"


def request(url: str, get: Getter) -> Page:
    """Ask for the job's record. With no company in the link, one request to
    the page first finds it. A link that is no Workable job gets a 404-like
    page so it ends as not found rather than as an error."""
    company, job_id = split_url(url)
    if job_id is None:
        return Page(status_code=404, text="", size=0, final_url=url)
    if company is None:
        landed = get(f"{BASE}/j/{job_id}")
        company, _ = split_url(landed.final_url or "")
        if company is None:
            return landed if landed.status_code != 200 else Page(404, "", landed.size, landed.final_url)
    return get(api_url(company, job_id), json=True)


def _account(company: str, get: Getter) -> dict[str, Any]:
    if company not in _accounts:
        record: dict[str, Any] = {}
        page = get(f"{BASE}/api/v1/accounts/{company}", json=True)
        if page.status_code == 200:
            try:
                parsed = json.loads(page.text)
                record = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                record = {}
        _accounts[company] = record
    return _accounts[company]


def _place(location: dict[str, Any]) -> str:
    return ", ".join(p for p in (location.get("city"), location.get("region"), location.get("country")) if p)


def posting_block(text: str, final_url: str, get: Getter) -> Optional[str]:
    """The rebuilt posting, or None if the answer holds no job."""
    try:
        job = json.loads(text)
    except ValueError:
        return None
    if not isinstance(job, dict) or not job.get("title"):
        return None
    company, _ = _company_and_id(final_url)

    logo = ""
    if company:
        account = _account(company, get)
        if account.get("name"):
            logo = (
                f'<a data-ui="company-logo" href="/{escape(company, quote=True)}/">'
                f'<img alt="{escape(account["name"], quote=True)}" src="{escape(account.get("logo") or "", quote=True)}"></a>'
            )

    def fact(ui: str, value: str) -> str:
        return f'<span data-ui="{ui}">{escape(value)}</span>' if value else ""

    departments = job.get("department") or []
    places = [
        _place(loc)
        for loc in job.get("locations") or ([job["location"]] if job.get("location") else [])
        if isinstance(loc, dict) and not loc.get("hidden")
    ]
    kind, workplace = job.get("type") or "", job.get("workplace") or ""
    sections = "".join(
        f'<section data-ui="{ui}"><h2>{heading}</h2><div>{job.get(key)}</div></section>'
        for ui, key, heading in (
            ("job-description", "description", "Description"),
            ("job-requirements", "requirements", "Requirements"),
            ("job-benefits", "benefits", "Benefits"),
        )
        if job.get(key)
    )
    return (
        '<div data-synthetic="api">'
        + logo
        + f'<h1 data-ui="job-title">{escape(job["title"])}</h1>'
        + fact("job-workplace", _WORKPLACE.get(workplace, workplace.replace("_", " ").capitalize()))
        + fact("job-department", ", ".join(d for d in departments if d) if isinstance(departments, list) else str(departments))
        + fact("job-type", _TYPES.get(kind, kind.capitalize()))
        + fact("job-code", job.get("code") or "")
        + ('<div data-ui="job-location">' + "".join(fact("job-location-tooltip", p) for p in places if p) + "</div>" if places else "")
        + sections
        + "</div>"
    )


def _company_and_id(final_url: str) -> tuple[Optional[str], Optional[str]]:
    """From the API address the answer came from: `/accounts/<company>/jobs/<id>`."""
    segments = [s for s in urlsplit(final_url).path.split("/") if s]
    if "accounts" in segments:
        at = segments.index("accounts")
        if at + 1 < len(segments):
            return segments[at + 1], segments[at + 3] if at + 3 < len(segments) else None
    return None, None


def gone_block(text: str) -> str:
    """What the parser reads as a removed job ("This job is no longer available")."""
    return '<div data-synthetic="api"><p>This job is no longer available</p></div>'
