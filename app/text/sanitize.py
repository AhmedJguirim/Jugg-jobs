"""Turn a job-board HTML description into clean text for a small LLM.

Why this is its own module: a 4B model spends its limited attention on whatever
you hand it. Markup, double-encoded entities and repeated navigation crumbs all
cost tokens and degrade the output, so the text is cleaned *before* it reaches
the model rather than being papered over in the prompt.

What the real data needed (measured over the ingested corpus, 68 vacancies):

- **Only six tags appear**: `p, ul, ol, li, b, i`. No script/style/table, so a
  focused parser beats a dependency.
- **61 of 68 descriptions are double-encoded**: they contain `&amp;eacute;`, not
  `&eacute;`. One `html.unescape` pass leaves `d&aacute;t` in the text, which
  the model then tries to translate. Hence `unescape_entities` loops.
- **Boilerplate repeats**: the same short lines (job title, city, "40 hours per
  week") appear several times from the page's navigation and header blocks.
  Repeats are dropped so the model sees each fact once.

Nothing here calls a model, so it is fast, deterministic and cheap to test.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser

# Tags that start a new line of text.
_BLOCK_TAGS = {"p", "div", "ul", "ol", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
# Tags whose content is never useful to a language model.
_DROP_CONTENT_TAGS = {"script", "style", "head", "noscript", "svg"}

# A repeated line is only treated as boilerplate below this length; longer
# repeated lines are far more likely to be real content than navigation crumbs.
# The boilerplate actually seen in the corpus ("Rotterdam", "40 hours per week",
# a repeated job title) is all well under this, while a short sentence is not.
_BOILERPLATE_MAX_LEN = 60

# Lines this short carry no translatable meaning on their own.
_MIN_LINE_LEN = 2

# Stopwords that separate Dutch from English cheaply. Deliberately small and
# high-frequency: this decides whether to spend ~16s of GPU time, so it only has
# to be right about the language, not identify it among dozens.
_DUTCH_MARKERS = re.compile(
    r"\b(de|het|een|en|van|je|jij|jouw|wij|onze|ons|voor|met|bij|zijn|wordt|worden"
    r"|ook|niet|wat|waar|naar|uit|over|als|maar|door|deze|die|dat|op|te|om|aan)\b",
    re.IGNORECASE,
)
_ENGLISH_MARKERS = re.compile(
    r"\b(the|and|you|your|we|our|us|for|with|are|is|will|not|what|where|this|that"
    r"|from|about|as|but|by|to|of|in|on|at|have|has)\b",
    re.IGNORECASE,
)

# How decisive the stopword counts must be before we trust them.
_LANGUAGE_MARGIN = 1.3


@dataclass(frozen=True)
class CleanedText:
    """Sanitised text plus what the pipeline needs to decide what to do with it."""

    text: str
    language: str  # "nl", "en" or "unknown"
    content_hash: str
    original_chars: int
    clean_chars: int
    truncated: bool = False

    @property
    def is_dutch(self) -> bool:
        return self.language == "nl"

    @property
    def reduction(self) -> float:
        """Share of the original characters removed (0.0-1.0)."""
        if not self.original_chars:
            return 0.0
        return 1 - (self.clean_chars / self.original_chars)


class _TextExtractor(HTMLParser):
    """Collect text, turning block-level tags into line breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")
        if tag == "li":
            # A leading marker keeps list structure legible once tags are gone.
            self._parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def unescape_entities(value: str, *, max_passes: int = 3) -> str:
    """Decode HTML entities, repeating while the text keeps changing.

    Most of this corpus is double-encoded (`&amp;eacute;`), so a single pass is
    not enough. Looping until stable also survives triple encoding, and the pass
    cap stops a pathological input from spinning.
    """
    for _ in range(max_passes):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    return value


def detect_language(text: str) -> str:
    """Classify text as "nl", "en" or "unknown" by stopword frequency.

    Used to skip translating what is already English - the cheapest possible
    saving, since it avoids the model entirely.
    """
    dutch = len(_DUTCH_MARKERS.findall(text))
    english = len(_ENGLISH_MARKERS.findall(text))

    if dutch == 0 and english == 0:
        return "unknown"
    if dutch > english * _LANGUAGE_MARGIN:
        return "nl"
    if english > dutch * _LANGUAGE_MARGIN:
        return "en"
    return "unknown"


def content_hash(text: str) -> str:
    """Stable fingerprint of cleaned text.

    Enrichment is keyed on this: if a re-ingest leaves the description
    unchanged, the stored translation is still valid and the GPU stays idle.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _drop_repeated_boilerplate(lines: list[str]) -> list[str]:
    """Remove short lines that appear more than once, keeping the first.

    Job pages repeat the title, city and hours in navigation and header blocks.
    The length guard protects genuine repeated prose.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        key = line.casefold()
        if len(line) <= _BOILERPLATE_MAX_LEN and key in seen:
            continue
        seen.add(key)
        kept.append(line)
    return kept


def strip_html(value: str) -> str:
    """HTML to plain text, preserving paragraph and list structure."""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = parser.text()

    # convert_charrefs handled one pass; the corpus needs another.
    text = unescape_entities(text)

    # Non-breaking and other exotic spaces confuse tokenisers; normalise them.
    text = text.replace("\xa0", " ").replace("‑", "-").replace("​", "")
    return text


def clean_lines(text: str) -> list[str]:
    """Split into trimmed, de-duplicated, meaningful lines."""
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        # A bullet marker with nothing after it is noise.
        if line in {"-", "-  ", ""}:
            continue
        if len(line.replace("- ", "")) < _MIN_LINE_LEN:
            continue
        lines.append(line)
    return _drop_repeated_boilerplate(lines)


def sanitize_description(
    value: str | None,
    *,
    max_chars: int | None = None,
) -> CleanedText:
    """Clean one job description and describe what came out.

    `max_chars` truncates on a line boundary as a last-resort guard; the
    pipeline prefers to chunk long text rather than lose the end of it.
    """
    original = value or ""
    if not original.strip():
        return CleanedText(
            text="",
            language="unknown",
            content_hash=content_hash(""),
            original_chars=len(original),
            clean_chars=0,
        )

    lines = clean_lines(strip_html(original))
    text = "\n".join(lines).strip()

    truncated = False
    if max_chars is not None and len(text) > max_chars:
        cut = text[:max_chars]
        # Prefer to end on a line break so a sentence is not severed.
        boundary = cut.rfind("\n")
        text = cut[:boundary] if boundary > max_chars // 2 else cut
        text = text.rstrip()
        truncated = True

    return CleanedText(
        text=text,
        language=detect_language(text),
        content_hash=content_hash(text),
        original_chars=len(original),
        clean_chars=len(text),
        truncated=truncated,
    )


def chunk_text(text: str, *, max_chars: int = 3000) -> list[str]:
    """Split text into chunks that a small model can translate faithfully.

    Splitting on blank lines first, then on single lines, keeps paragraphs and
    list items intact. Long documents are chunked rather than truncated because
    a 4B model handed 10k characters tends to summarise instead of translate.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")
    if not text.strip():
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for block in _split_blocks(text, max_chars):
        block_len = len(block) + 1
        if size + block_len > max_chars and current:
            chunks.append("\n".join(current).strip())
            current, size = [], 0
        current.append(block)
        size += block_len

    if current:
        chunks.append("\n".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def _split_blocks(text: str, max_chars: int) -> list[str]:
    """Break text into units no larger than max_chars, splitting progressively."""
    blocks: list[str] = []
    for paragraph in text.split("\n"):
        if len(paragraph) <= max_chars:
            blocks.append(paragraph)
            continue
        # A single paragraph over the limit: split on sentence ends, then hard.
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        buffer = ""
        for sentence in sentences:
            while len(sentence) > max_chars:
                blocks.append(sentence[:max_chars])
                sentence = sentence[max_chars:]
            if len(buffer) + len(sentence) + 1 > max_chars:
                if buffer:
                    blocks.append(buffer.strip())
                buffer = sentence
            else:
                buffer = f"{buffer} {sentence}".strip()
        if buffer:
            blocks.append(buffer.strip())
    return blocks
