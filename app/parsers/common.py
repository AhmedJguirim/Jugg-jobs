"""Pieces every platform parser needs.

- `description_html`: a posting's description as HTML that is safe to show.
- `parse_compensation`: a pay range out of a sentence.
- `read_pay`: the pay range of a whole description, from the sentence that states it.
- `place_from_text`: a `LocationBase` from a postal "City, Region, Country" string.
- `company_from_slug` / `title_from_slug`: a name or title the URL only gives as a
  slug, recovered with its real spelling from the page text where it appears.
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
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")



# A line that is just an amount: a currency marker, or a thousands-formatted number.
_AMOUNT_LINE = re.compile(r"[$\u20ac\u00a3]|z\u0142|\b(?:USD|CAD|EUR|GBP|AUD|NZD|CHF|PLN|SEK|NOK|DKK|INR|SGD|JPY|BRL|MXN)\b|\d{1,3}(?:[.,]\d{3})")


def _pay_sentences(text: str) -> list[str]:
    """Sentences (or lines) of a description that state pay, in order.

    A pay *label* with no figure of its own ("Base Pay Grade") borrows the next
    line when that line is an amount ("259,000 zl - 379,000 zl"): pages lay such
    ranges out as a heading and a value in separate blocks.
    """
    pieces = [p for p in _SENTENCE_BREAK.split(text or "") if p.strip()]
    found = []
    for index, sentence in enumerate(pieces):
        lowered = sentence.lower()
        if not any(w in lowered for w in _PAY_WORDS) or any(w in lowered for w in _NOT_PAY_WORDS):
            continue
        candidate = sentence
        if not re.search(r"\d", sentence) and index + 1 < len(pieces):
            following = pieces[index + 1]
            if _AMOUNT_LINE.search(following) and not any(w in following.lower() for w in _NOT_PAY_WORDS):
                candidate = f"{sentence} {following}"
        if re.search(r"\d", candidate):
            found.append(candidate.strip())
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

    `text` should have one statement per line (`Node.lines()`), so a bullet list
    without full stops is not read as one long sentence.

    Only a sentence about pay counts, never one about equity, a bonus or a
    stipend, so a "$500M Series E" or "New hire equity: $24,000" is not a salary.
    `details` is what to keep in `raw`: the sentence and its currency (the salary
    columns have none). All `None` when no pay is stated.
    """
    for sentence in _pay_sentences(text):
        low, high, period = parse_compensation(sentence)
        if low is not None:
            currency, source = _currency(sentence)
            # A bare small number ("5+ years", "28 days") is not pay: unless a
            # currency is named, the amount must be a real-looking figure.
            if currency is None and high < 1000:
                continue
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


# ---- names and titles that a URL gives only as a slug ----------------------------

# A run of words starting with a capital or a digit ("Insider One", "20four7VA").
_CAPITALISED_RUN = re.compile(r"[A-Z0-9][\w&'-]*(?: [A-Z0-9][\w&'-]*)*")
_ACRONYMS = {
    "ai": "AI", "qa": "QA", "ui": "UI", "ux": "UX", "api": "API", "sql": "SQL", "aws": "AWS",
    "it": "IT", "hr": "HR", "ml": "ML", "sre": "SRE", "cto": "CTO", "vp": "VP", "erp": "ERP",
    "crm": "CRM", "gcp": "GCP", "llm": "LLM", "php": "PHP", "sdet": "SDET", "devops": "DevOps",
    "usa": "USA", "uk": "UK", "us": "US", "llc": "LLC", "ltd": "Ltd", "inc": "Inc",
}
_EDGE_PUNCTUATION = " \t\n.,;:!?()[]{}\"'*"


def _squash(text: str) -> str:
    """Lower-case letters and digits only: how a slug and its spelling compare equal."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def slug_words(slug: str) -> str:
    """`"senior-frontend-developer"` -> `"Senior Frontend Developer"` (known acronyms kept upper-case)."""
    words = [w for w in re.split(r"[-_\s]+", slug) if w]
    return " ".join(_ACRONYMS.get(w.lower(), w.capitalize()) for w in words)


def company_from_slug(slug: str, text: str) -> tuple[str, str]:
    """(name, source): the company's own spelling of its URL slug, found in `text`.

    "insiderone" -> "Insider One" when the text says "Insider One", and "20four7va"
    -> "20four7VA". Otherwise the slug, capitalised ("jobgether" -> "Jobgether").
    `source` is "text" or "slug" so callers can record how sure the name is.
    """
    wanted = _squash(slug)
    for run in _CAPITALISED_RUN.finditer(text or ""):
        words = run.group().split()
        for start in range(len(words)):
            for end in range(start + 1, min(len(words), start + 4) + 1):
                candidate = " ".join(words[start:end])
                if _squash(candidate) == wanted:
                    return candidate, "text"
    return slug_words(slug), "slug"


def title_from_slug(slug: str, text: str) -> tuple[str, str]:
    """(title, source): the job title from its URL slug, spelt as the text spells it.

    The slug has lost punctuation and case ("senior-full-stack-engineer-tech-lead");
    when the same words appear consecutively in `text` ("Senior Full-Stack Engineer
    - Tech Lead") that spelling is used. Otherwise the slug, capitalised.
    """
    target = _squash(slug)
    words = (text or "").split()
    if target:
        for start in range(len(words)):
            joined = ""
            for end in range(start, min(len(words), start + 24)):
                joined += _squash(words[end])
                if joined == target:
                    found = " ".join(words[start : end + 1]).strip(_EDGE_PUNCTUATION)
                    return found, "text"
                if len(joined) > len(target):
                    break
    return slug_words(slug), "slug"
