"""Pull result links out of a pasted Google results block.

The input is the `<div id="search">` element copied from a Google results
page. An organic result is an `<a href>` that wraps the result's `<h3>` title;
everything else in the block (the "Read more" deep links with a `#:~:text=`
fragment, the `ping` tracking URLs, ads, icons) is ignored. Stdlib only.

If the pasted text has no `id="search"` element the whole text is scanned, so
copying a slightly different slice of the page still works.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urldefrag

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _ResultLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_search = False
        self._search_depth = 0  # open elements inside the search div
        self.saw_search_div = False
        self._href: str | None = None
        self._has_h3 = False
        self.scoped: list[str] = []  # found inside #search
        self.anywhere: list[str] = []  # found anywhere (fallback)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if not self.in_search and tag == "div" and attributes.get("id") == "search":
            self.in_search = True
            self.saw_search_div = True
            self._search_depth = 1
        elif self.in_search and tag not in _VOID:
            self._search_depth += 1

        if tag == "a":
            self._href = attributes.get("href")
            self._has_h3 = False
        elif tag == "h3" and self._href is not None:
            self._has_h3 = True

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            if self._has_h3:
                url = urldefrag(self._href.strip())[0]
                if url.lower().startswith(("http://", "https://")):
                    self.anywhere.append(url)
                    if self.in_search:
                        self.scoped.append(url)
            self._href = None
            self._has_h3 = False
        if self.in_search and tag not in _VOID:
            self._search_depth -= 1
            if self._search_depth <= 0:
                self.in_search = False


def extract_result_urls(html_text: str) -> list[str]:
    """Result URLs in page order, fragment stripped, duplicates removed."""
    parser = _ResultLinks()
    parser.feed(html_text)
    parser.close()
    found = parser.scoped if parser.saw_search_div else parser.anywhere
    return list(dict.fromkeys(found))
