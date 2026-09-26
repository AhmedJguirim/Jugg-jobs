"""Translation and assessment on the local model.

This layer owns *how* to use the model well: chunking, skipping work that is
already correct, validating what comes back, and repairing it once before giving
up. It never decides *when* to run - that is the workflow's job, and the reason
this module stays free of DBOS.

Every call here occupies the GPU, so every caller must be inside the `llm`
queue (ADR-0007).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import get_settings
from app.integrations.ollama import LlmResult, chat
from app.llm.profile import CandidateProfile
from app.llm.prompts import (
    ASSESSMENT_PROMPT_VERSION,
    TRANSLATION_PROMPT_VERSION,
    assessment_messages,
    json_repair_messages,
    title_translation_messages,
    translation_messages,
)
from app.logging_setup import log
from app.text import chunk_text

VERDICTS = {"strong_match", "possible", "weak", "irrelevant"}
SENIORITIES = {"intern", "junior", "mid", "senior", "lead", "unknown"}
REMOTE_VALUES = {"onsite", "hybrid", "remote", "unknown"}

# Small models sometimes wrap output in fences or a preamble despite being told
# not to; strip that rather than discarding an otherwise good answer.
_FENCE = re.compile(r"^\s*```(?:json|text)?\s*|\s*```\s*$", re.IGNORECASE)
_PREAMBLE = re.compile(
    r"^\s*(here (?:is|'s) (?:the )?(?:translation|english translation|json)|"
    r"translation|english)\s*[:\-]\s*",
    re.IGNORECASE,
)


@dataclass
class TranslationResult:
    text: str
    title: Optional[str] = None
    model: str = ""
    prompt_version: str = TRANSLATION_PROMPT_VERSION
    chunks: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    skipped: bool = False
    skip_reason: Optional[str] = None


@dataclass
class AssessmentResult:
    fit_score: int
    verdict: str
    seniority: str = "unknown"
    dutch_required: bool = False
    blocked: bool = False
    remote: str = "unknown"
    summary: str = ""
    matched_requirements: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    reasoning: str = ""
    model: str = ""
    prompt_version: str = ASSESSMENT_PROMPT_VERSION
    profile_name: str = "default"
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    repaired: bool = False


def _clean_output(value: str) -> str:
    value = _FENCE.sub("", value.strip())
    return _PREAMBLE.sub("", value).strip()


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def translate_text(
    text: str,
    *,
    source_language: str = "nl",
    chunk_chars: Optional[int] = None,
    model: Optional[str] = None,
) -> TranslationResult:
    """Translate cleaned vacancy text to English, chunk by chunk.

    Long descriptions are split because a 4B model handed 10k characters starts
    summarising instead of translating. Chunks are translated independently and
    rejoined, which also means a failure costs one chunk, not the document.
    """
    settings = get_settings()
    chunk_chars = chunk_chars or settings.llm_chunk_chars

    if not text.strip():
        return TranslationResult(text="", skipped=True, skip_reason="empty text")
    if source_language == "en":
        return TranslationResult(
            text=text, skipped=True, skip_reason="already English", chunks=0
        )

    chunks = chunk_text(text, max_chars=chunk_chars)
    translated: list[str] = []
    prompt_tokens = output_tokens = 0
    duration = 0.0
    used_model = ""

    for index, chunk in enumerate(chunks, start=1):
        result: LlmResult = chat(
            translation_messages(chunk),
            model=model,
            temperature=0.2,
            think=False,
            # Allow room for English to run longer than Dutch without truncating.
            num_predict=int(len(chunk) / 2) + 512,
        )
        translated.append(_clean_output(result.content))
        prompt_tokens += result.prompt_tokens
        output_tokens += result.output_tokens
        duration += result.duration_s
        used_model = result.model
        log.info(
            "translated chunk %s/%s (%s chars -> %s tokens, %.1fs)",
            index,
            len(chunks),
            len(chunk),
            result.output_tokens,
            result.duration_s,
        )

    return TranslationResult(
        text="\n\n".join(part for part in translated if part).strip(),
        model=used_model,
        chunks=len(chunks),
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        duration_s=duration,
    )


def translate_title(title: str, *, source_language: str = "nl", model: Optional[str] = None) -> str:
    """Translate a job title. Cheap enough to do in one call."""
    if not title.strip() or source_language == "en":
        return title
    result = chat(
        title_translation_messages(title),
        model=model,
        temperature=0.1,
        think=False,
        num_predict=64,
    )
    # A small model occasionally editorialises; fall back to the original rather
    # than storing a sentence where a title belongs.
    cleaned = _clean_output(result.content).splitlines()[0].strip().strip('"') if result.content else ""
    if not cleaned or len(cleaned) > max(120, len(title) * 3):
        return title
    return cleaned


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------


def _coerce_list(value: Any, *, limit: int = 8) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    items = [str(item).strip() for item in value if str(item).strip()]
    return items[:limit]


def _coerce_choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return text if text in allowed else default


def _score_to_verdict(score: int) -> str:
    if score >= 80:
        return "strong_match"
    if score >= 55:
        return "possible"
    if score >= 25:
        return "weak"
    return "irrelevant"


def _parse_assessment(payload: str) -> dict:
    """Parse the model's JSON, tolerating surrounding text."""
    text = _clean_output(payload)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost object in the response.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("no JSON object in model output")


def assess_job(
    *,
    title: str,
    description: str,
    profile: CandidateProfile,
    company: Optional[str] = None,
    location: Optional[str] = None,
    salary: Optional[str] = None,
    contract: Optional[str] = None,
    education: Optional[str] = None,
    model: Optional[str] = None,
) -> AssessmentResult:
    """Score one vacancy against a profile and return a validated result.

    Output is constrained with Ollama's JSON mode; if it still will not parse,
    the model is given one repair attempt before we give up. Every field is
    coerced into range rather than trusted, so a hallucinated verdict cannot
    reach the database.
    """
    messages = assessment_messages(
        title=title,
        company=company,
        location=location,
        salary=salary,
        contract=contract,
        education=education,
        description=description,
        profile_text=profile.as_prompt_text(),
    )

    result = chat(
        messages,
        model=model,
        temperature=0.1,
        think=False,
        json_format=True,
        # Generous: a verbose assessment truncated at 700 tokens produced
        # unparseable JSON on one real vacancy, and the repair inherited the same
        # cap and truncated in turn.
        num_predict=1400,
    )

    repaired = False
    try:
        data = _parse_assessment(result.content)
    except (ValueError, json.JSONDecodeError):
        log.warning("assessment JSON unparseable, attempting one repair")
        repair = chat(
            json_repair_messages(result.content),
            model=model,
            temperature=0.0,
            think=False,
            json_format=True,
            num_predict=1400,
        )
        data = _parse_assessment(repair.content)
        repaired = True
        result.prompt_tokens += repair.prompt_tokens
        result.output_tokens += repair.output_tokens
        result.duration_s += repair.duration_s

    try:
        score = int(float(data.get("fit_score", 0)))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    verdict = _coerce_choice(data.get("verdict"), VERDICTS, _score_to_verdict(score))

    return AssessmentResult(
        fit_score=score,
        verdict=verdict,
        seniority=_coerce_choice(data.get("seniority"), SENIORITIES, "unknown"),
        dutch_required=bool(data.get("dutch_required", False)),
        blocked=bool(data.get("blocked", False)),
        remote=_coerce_choice(data.get("remote"), REMOTE_VALUES, "unknown"),
        summary=str(data.get("summary") or "").strip()[:2000],
        matched_requirements=_coerce_list(data.get("matched_requirements")),
        missing_requirements=_coerce_list(data.get("missing_requirements")),
        red_flags=_coerce_list(data.get("red_flags")),
        reasoning=str(data.get("reasoning") or "").strip()[:2000],
        model=result.model,
        profile_name=profile.name,
        prompt_tokens=result.prompt_tokens,
        output_tokens=result.output_tokens,
        duration_s=result.duration_s,
        repaired=repaired,
    )
