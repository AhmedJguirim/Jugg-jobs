"""Pieces every platform parser needs.

- `description_html`: a posting's description as HTML that is safe to show.
- `parse_compensation`: a pay range out of a sentence.
- `read_pay`: the pay range of a whole description, from the sentence that states it.
- `place_from_text`: a `LocationBase` from a postal "City, Region, Country" string.
"""

from __future__ import annotations

import re
from typing import Optional

from app.schemas.job import LocationBase
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



def _pay_sentences(text: str) -> list[str]:
    """Sentences of a description that state pay, in order."""
    found = []
    for sentence in _SENTENCE_BREAK.split(text or ""):
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


def read_pay(text: str) -> tuple[Optional[int], Optional[int], Optional[str], Optional[dict]]:
    """(min, max, type, details) from the first sentence of `text` that states pay.

    Only a sentence about pay counts, never one about equity, a bonus or a
    stipend, so a "$500M Series E" or "New hire equity: $24,000" is not a salary.
    `details` is what to keep in `raw`: the sentence and its currency (the salary
    columns have none). All `None` when no pay is stated.
    """
    for sentence in _pay_sentences(text):
        low, high, period = parse_compensation(sentence)
        if low is not None:
            currency, source = _currency(sentence)
            details = {"text": sentence[:_MAX_SENTENCE], "currency": currency, "currency_source": source}
            return low, high, period, details
    return None, None, None, None


# Two letters after a comma are a US state ("Austin, TX") only if they are one;
# otherwise they are a country code ("London, UK", "Amsterdam, NL"). The few codes
# that are both ("CA" Canada/California, "DE" Germany/Delaware) are read as states.
_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
    "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)


def place_from_text(text: str) -> Optional[LocationBase]:
    """A place, only when the text is a postal "City, Region, Country".

    "Remote Poland", "Canada (Remote)", "Multiple locations", a lone country or a
    lone city are not places the cities table should hold; callers keep that text
    in `raw["sections"]`.
    """
    if not text or re.search(r"remote|hybrid|anywhere|multiple|various|worldwide", text, re.IGNORECASE):
        return None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) >= 3:
        return LocationBase(city=parts[0], province=parts[1], country_name=parts[-1])
    if len(parts) == 2:
        # "Austin, TX" is a state; "Berlin, Germany" and "London, UK" are countries.
        if parts[1] in _US_STATES:
            return LocationBase(city=parts[0], province=parts[1])
        return LocationBase(city=parts[0], country_name=parts[1])
    return None
