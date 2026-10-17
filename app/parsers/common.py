"""Pieces every platform parser needs.

- `description_html`: a posting's description as HTML that is safe to show.
- `parse_compensation`: a pay range out of a sentence.
"""

from __future__ import annotations

import re
from typing import Optional

from app.text.dom import Node

_EMPTY_PARAGRAPH = re.compile(r"<p></p>")
# Attributes worth keeping in a stored description; inline `style` and ids are noise.
KEEP_ATTRS = frozenset({"href"})
# The description is shown as HTML, so only formatting tags survive.
ALLOWED_TAGS = frozenset(
    {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u", "a", "h1", "h2", "h3",
     "h4", "h5", "h6", "blockquote", "code", "pre", "hr", "span", "div"}
)


def description_html(node: Optional[Node]) -> Optional[str]:
    """The node's inner HTML, reduced to formatting tags and http(s)/mailto links."""
    if node is None:
        return None
    html = node.inner_html(keep_attrs=KEEP_ATTRS, allowed_tags=ALLOWED_TAGS)
    html = _EMPTY_PARAGRAPH.sub("", html).strip()
    return html or None


_AMOUNT = re.compile(r"(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*([kK])?")


def parse_compensation(text: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """`"EUR 55K - 72K"` -> (55000, 72000, "yearly"). Unreadable -> all None.

    Takes the first range in the text; a posting may list several tiers, and
    the caller keeps the full sentence(s). `salary_type` uses the vocabulary
    the NVB data already has: yearly / monthly / hourly / unspecified.
    """
    lowered = text.lower()
    if re.search(r"hour|/\s*hr\b|\bp\.?h\.?\b", lowered):
        period = "hourly"
    elif re.search(r"month|/\s*mo\b", lowered):
        period = "monthly"
    elif re.search(r"year|annual|/\s*yr\b|p\.a\.|per annum", lowered):
        period = "yearly"
    else:
        period = None

    amounts: list[int] = []
    for number, k in _AMOUNT.findall(text):
        if k:
            value = float(number.replace(",", ".")) * 1000
        elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", number):
            value = float(re.sub(r"[.,]", "", number))  # 55,000 / 55.000
        else:
            value = float(number.replace(",", "."))
        # Small bare numbers are noise ("401k" aside, "5 days") unless hourly.
        if value >= 1000 or (period == "hourly" and value > 0):
            amounts.append(int(round(value)))
        if len(amounts) == 2:
            break

    if not amounts:
        return None, None, None
    low, high = amounts[0], amounts[-1]
    if high < low:
        low, high = high, low
    if period is None:
        period = "yearly" if high >= 10000 else "unspecified"
    return low, high, period
