"""Turn a collected link into the HTML a hand copy would have been.

The parsers were written against what the user copies from the browser: the
element that holds the posting, not the page around it. Each platform here says
how to get from a link to that, because the platforms differ:

- Lever, Greenhouse and Breezy serve the posting as plain HTML inside a much larger
  page (Lever 720-740 KB around a 5-15 KB block): the block is cut out.
- Ashby and Workable are single-page apps; their responses hold the posting only
  as JSON, so the block is rebuilt from it (`ashby.py`, `workable.py`).

A platform with no entry cannot be fetched yet (`supported`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from app.integrations.listing_pages import ashby, workable
from app.integrations.listing_pages.client import Page
from app.text.dom import Node, parse_html

# What a service passes in to make a request: `get(url)` or `get(url, json=True)`.
Getter = Callable[..., Page]


@dataclass(frozen=True)
class Source:
    # link -> the page that decides the outcome (usually one request)
    request: Callable[[str, Getter], Page]
    # (body, address it came from, getter) -> the block, or None
    posting: Callable[[str, str, Getter], Optional[str]]
    # body -> the block to keep for a posting the server says is gone. None keeps the body.
    gone: Optional[Callable[[str], str]] = None
    # A 200 answer -> True if it is really the platform's "removed" answer (a redirect
    # to a board page, a "closed" page): some platforms never use a 404 for it.
    is_gone: Optional[Callable[[Page], bool]] = None


def _posting_url(*form_suffixes: str) -> Callable[[str], str]:
    """The posting's own page: no query, and not its application form."""

    def build(url: str) -> str:
        parts = urlsplit(url.strip())
        path = parts.path.rstrip("/")
        for suffix in form_suffixes:
            path = path.removesuffix(suffix)
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    return build


def _simple(form_suffixes: tuple[str, ...], cut: Callable[[str], Optional[str]], **extra) -> Source:
    """One GET of the posting's page, then `cut` the block out of the body."""
    address = _posting_url(*form_suffixes)
    return Source(
        request=lambda url, get: get(address(url)),
        posting=lambda text, final_url, get: cut(text),
        **extra,
    )


def _find_block(html: str, finder: Callable[[Node], Optional[Node]]) -> Optional[str]:
    node = finder(parse_html(html))
    return node.outer_html() if node is not None else None


def _lever(html: str) -> Optional[str]:
    """`<div class="content">` that holds the posting headline (the page has
    other `content` divs)."""

    def finder(root: Node) -> Optional[Node]:
        for node in root.find_all("div", cls="content"):
            if node.find(cls="posting-headline") is not None:
                return node
        return None

    return _find_block(html, finder)


def _greenhouse(html: str) -> Optional[str]:
    """The plain `div` that holds the job header, tags, description and the
    "career at X?" box - the parent of `div.job__description`."""

    def finder(root: Node) -> Optional[Node]:
        description = root.find("div", cls="job__description")
        return description.parent if description is not None else None

    return _find_block(html, finder)


def _greenhouse_gone(page: Page) -> bool:
    """A removed job redirects to the board with `?error=true` and answers 200."""
    return "error=true" in page.final_url.lower()


def _greenhouse_gone_block(html: str) -> str:
    # The board page the redirect lands on says nothing about the job; the parser
    # needs a sentence it knows (and the link's own `error=true` is not in the
    # url it is given).
    return '<div data-synthetic="redirect"><p>This job is no longer open</p></div>'


def _lever_gone_block(html: str) -> str:
    # Lever's 404 is a whole page (~300 KB); the parser only needs its sentence.
    return "<div data-synthetic='404'><p>Sorry, we couldn't find anything here</p></div>"


def _breezy(html: str) -> Optional[str]:
    """`<div class="description">` - the only part of the page the Breezy parser
    reads (title and company come from the link). The parser takes the first one too."""
    return _find_block(html, lambda root: root.find("div", cls="description"))


def _breezy_gone(page: Page) -> bool:
    """A removed position either redirects to the company's page, away from
    `/p/<id>-...`, or stays on the link and shows a "Position Closed" heading."""
    if not urlsplit(page.final_url).path.startswith("/p/"):
        return True
    root = parse_html(page.text)
    closed = any("position closed" in h1.text().lower() for h1 in root.find_all("h1"))
    return closed and root.find("div", cls="description") is None


def _breezy_gone_block(html: str) -> str:
    return '<div data-synthetic="redirect"><p>This position is no longer available</p></div>'


_PLATFORMS: dict[str, Source] = {
    "lever": _simple(("/apply",), _lever, gone=_lever_gone_block),
    "greenhouse": _simple((), _greenhouse, gone=_greenhouse_gone_block, is_gone=_greenhouse_gone),
    "breezy": _simple((), _breezy, gone=_breezy_gone_block, is_gone=_breezy_gone),
    "ashbyhq": _simple(("/application",), ashby.posting_block, gone=ashby.gone_block),
    "workable": Source(
        request=workable.request,
        posting=workable.posting_block,
        gone=workable.gone_block,
    ),
}


def supported(platform: Optional[str]) -> bool:
    return platform in _PLATFORMS


def request(platform: str, url: str, get: Getter) -> Page:
    """Make the request(s) for a collected link; the page that decides the outcome."""
    return _PLATFORMS[platform].request(url, get)


def is_gone(platform: str, page: Page) -> bool:
    """True for a 200 answer that is really the platform's "removed" answer."""
    check = _PLATFORMS[platform].is_gone
    return bool(check and check(page))


def extract_posting(platform: str, page: Page, get: Getter) -> Optional[str]:
    """The posting's block as HTML, or None if the page does not have one."""
    return _PLATFORMS[platform].posting(page.text, page.final_url, get)


def gone_page(platform: str, page: Page) -> str:
    """What to keep for a posting the server says is gone."""
    gone = _PLATFORMS[platform].gone
    return gone(page.text) if gone is not None else page.text
