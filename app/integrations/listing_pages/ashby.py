"""Ashby: rebuild the rendered posting from the data the server embeds.

Ashby is a single-page app. The server sends a ~40 KB shell with no posting in
it; the page's script draws the posting from `window.__appData`, a JSON object
in that same response (`posting`: title, department, location, workplace and
employment type, compensation, `descriptionHtml`; `organization`: name, website,
logo). There is nothing to cut out, so this writes the few elements the Ashby
parser reads - header with logo link, `h1`, the left pane's labelled facts, the
description - from that JSON, in the same class names and labels the rendered
page uses. The root carries `data-synthetic="appData"` so a stored block can be
told from a hand copy.

A posting that was taken down answers 404 with `posting: null`; `gone_block` is
the page the parser reads as "Job not found".
"""

from __future__ import annotations

import json
import re
from html import escape
from typing import Any, Optional

_MARKER = "window.__appData = "

# Ashby's enum values -> the words its page shows.
_WORKPLACE = {"OnSite": "On-site", "Remote": "Remote", "Hybrid": "Hybrid"}


def app_data(html: str) -> Optional[dict[str, Any]]:
    """The embedded `__appData` object, or None if the page has none."""
    start = html.find(_MARKER)
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html[start + len(_MARKER):])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _words(value: str) -> str:
    """`FullTime` -> `Full time`."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    return spaced[:1] + spaced[1:].lower()


def _facts(posting: dict[str, Any]) -> list[tuple[str, str]]:
    """The left pane, in the order the page shows it. An empty fact is left out."""
    location = posting.get("locationName") or ""
    secondary = [s.strip() for s in posting.get("secondaryLocationNames") or [] if s and s.strip()]
    workplace = posting.get("workplaceType") or ""
    employment = posting.get("employmentType") or ""
    facts = [
        ("Location", "; ".join(p for p in [location.strip(), *secondary] if p)),
        ("Address", (posting.get("locationAddress") or "").strip()),
        ("Employment Type", _words(employment) if employment else ""),
        ("Location Type", _WORKPLACE.get(workplace, _words(workplace)) if workplace else ""),
        ("Department", (posting.get("departmentName") or "").strip()),
        ("Compensation", (posting.get("compensationTierSummary") or "").strip()),
    ]
    return [(label, value) for label, value in facts if value]


def posting_block(html: str) -> Optional[str]:
    """The rebuilt posting, or None if the page carries no posting."""
    data = app_data(html)
    posting = (data or {}).get("posting")
    if not isinstance(posting, dict) or not posting.get("title"):
        return None
    organization = data.get("organization") or {}
    theme = organization.get("theme") or {}

    header = ""
    if organization.get("name"):
        logo = (
            f'<img alt="{escape(organization["name"], quote=True)}" '
            f'src="{escape(theme.get("logoWordmarkImageUrl") or "", quote=True)}">'
        )
        website = organization.get("publicWebsite") or ""
        header = (
            '<div class="ashby-job-posting-header">'
            + (f'<a href="{escape(website, quote=True)}">{logo}</a>' if website else logo)
            + "</div>"
        )

    pane = "".join(
        f"<div><h2>{escape(label)}</h2><p>{escape(value)}</p></div>" for label, value in _facts(posting)
    )
    return (
        '<div id="root" data-synthetic="appData">'
        + header
        + f'<h1 class="ashby-job-posting-heading">{escape(posting["title"])}</h1>'
        + f'<div class="ashby-job-posting-left-pane">{pane}</div>'
        + f'<div class="ashby-job-posting-description">{posting.get("descriptionHtml") or ""}</div>'
        + "</div>"
    )


def gone_block(html: str) -> str:
    """What the parser reads as a removed posting. Ashby's own 404 shell has no
    "Job not found" text in it (the script writes it), so it is supplied here."""
    return '<div id="root" data-synthetic="appData"><h1>Job not found</h1></div>'
