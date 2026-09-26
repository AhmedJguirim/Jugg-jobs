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
STACK_MATCHES = {"core", "proficient", "familiar", "unrelated", "non_technical"}
SENIORITY_FITS = {"under_qualified", "good", "over_qualified", "unknown"}

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
    # Which tier of the candidate's skills the vacancy's primary stack sits in;
    # this is what anchors fit_score to a band (ADR-0008).
    stack_match: str = "unrelated"
    seniority_fit: str = "unknown"
    dutch_required: bool = False
    sponsorship_required: bool = False
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
# Stack tier detection
# ---------------------------------------------------------------------------

# Bands each tier maps to. The model scores *within* the band it is given; the
# band itself is decided here.
STACK_BANDS = {
    "core": (75, 95),
    "proficient": (55, 75),
    "familiar": (30, 55),
    "unrelated": (10, 30),
    "non_technical": (0, 10),
}


def _mentions(text: str, skill: str) -> bool:
    r"""Whole-word, case-insensitive match.

    The \b anchors matter: without them "Java" fires inside "JavaScript" and
    "Go" inside "Going", silently mis-tiering a vacancy.
    """
    pattern = r"\b" + re.escape(skill) + r"\b"
    return re.search(pattern, text, re.IGNORECASE) is not None


# Words that mark a vacancy as a software role at all. Without one of these and
# without any skill hit, the vacancy is not a technical job.
_TECH_MARKERS = (
    "developer", "engineer", "software", "backend", "back-end", "frontend",
    "front-end", "fullstack", "full-stack", "programmer", "programming", "api",
    "database", "devops", "architect", "webdeveloper", "ontwikkelaar",
    "programmeur", "softwareontwikkelaar",
)

# A skill named in the title says far more about the role than one buried in a
# list of nice-to-haves, so title hits dominate the tier decision.
_TITLE_WEIGHT = 10
_BODY_SKILL_WEIGHT = 2
_REPEAT_WEIGHT = 0.5
_MAX_REPEATS = 4

# Supporting technologies: databases, version control, containers, API clients.
# These say little about a vacancy's stack because nearly every technical advert
# names one, so they count as weak evidence. Naming *Laravel* once identifies
# the stack; naming *PostgreSQL* once does not - a "GIS Specialist" role
# mentioning PostgreSQL among its spatial-database requirements was landing in
# the proficient band (55) purely on that.
_SUPPORTING_TECH = {
    "mysql", "postgresql", "mongodb", "redis", "git", "docker", "postman",
    "sql", "rest apis", "background jobs and queues", "linux", "ci/cd",
}
_SUPPORTING_FACTOR = 0.25

# Evidence a tier needs before it can claim the vacancy. One primary technology
# (2.0) clears it; one supporting technology (0.5) does not.
_MIN_EVIDENCE = 2.0


def _count(text: str, skill: str) -> int:
    pattern = r"\b" + re.escape(skill) + r"\b"
    return len(re.findall(pattern, text, re.IGNORECASE))


def classify_stack(
    title: str, description: str, profile: CandidateProfile
) -> tuple[str, list[str]]:
    """Decide which of the candidate's skill tiers a vacancy's stack falls in.

    Done in code rather than by the model. A 4B repeatedly labelled a Python
    vacancy "unrelated" even when Python sat in the candidate's familiar tier
    and the prompt said so with a worked example - it conflated "not their
    strongest stack" with "not in their skill set". Tier membership is an exact
    lookup, so it is computed exactly and the model is left with the part that
    genuinely needs judgement: placing the score inside the band.

    Scoring, rather than first-match, because first-match is wrong on real
    vacancies: a Python role that mentions PHP once in a list of nice-to-haves
    would otherwise be classed as core PHP work. A skill in the *title* weighs
    far more than one buried in the body, and repeated mentions count for a
    little more than a single aside.

    A tier must also clear `_MIN_EVIDENCE` to win at all, and supporting
    technologies (databases, Git, Docker) count for a quarter, so a lone
    incidental mention does not pull an unrelated vacancy into a skill band.

    Returns the winning tier and the skills that matched it.
    """
    title = title or ""
    body = description or ""
    combined = f"{title}\n{body}"

    scores: dict[str, float] = {}
    hits: dict[str, list[str]] = {}

    for tier, skills in (
        ("core", profile.core_skills),
        ("proficient", profile.proficient_skills),
        ("familiar", profile.peripheral_skills),
    ):
        score = 0.0
        matched: list[str] = []
        for skill in skills:
            in_title = _count(title, skill)
            in_body = _count(body, skill)
            if not in_title and not in_body:
                continue
            matched.append(skill)
            weight = _SUPPORTING_FACTOR if skill.lower() in _SUPPORTING_TECH else 1.0
            score += in_title * _TITLE_WEIGHT * weight
            if in_body:
                score += _BODY_SKILL_WEIGHT * weight
                score += min(in_body - 1, _MAX_REPEATS) * _REPEAT_WEIGHT * weight
        # Below the evidence floor the mention is treated as incidental.
        if matched and score >= _MIN_EVIDENCE:
            scores[tier] = score
            hits[tier] = matched

    if scores:
        # Ties break towards the stronger tier, which is the order they are in.
        order = {"core": 0, "proficient": 1, "familiar": 2}
        best = min(scores, key=lambda tier: (-scores[tier], order[tier]))
        return best, hits[best]

    lowered = combined.lower()
    if any(marker in lowered for marker in _TECH_MARKERS):
        return "unrelated", []
    return "non_technical", []


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
    # The tier is a lookup, not a judgement call, so it is computed here and
    # handed to the model as a fact. The model's job is to place the score
    # inside that band - see classify_stack.
    stack_match, matched_skills = classify_stack(title, description, profile)
    low, high = STACK_BANDS[stack_match]

    messages = assessment_messages(
        title=title,
        company=company,
        location=location,
        salary=salary,
        contract=contract,
        education=education,
        description=description,
        profile_text=profile.as_prompt_text(),
        stack_match=stack_match,
        matched_skills=matched_skills,
        band=(low, high),
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
    # Clamp into the band the tier dictates: a small model will occasionally
    # ignore the range it was given, and the tier is the more trustworthy signal.
    score = max(low, min(high, max(0, min(100, score))))

    verdict = _coerce_choice(data.get("verdict"), VERDICTS, _score_to_verdict(score))

    return AssessmentResult(
        fit_score=score,
        verdict=verdict,
        seniority=_coerce_choice(data.get("seniority"), SENIORITIES, "unknown"),
        stack_match=stack_match,
        seniority_fit=_coerce_choice(data.get("seniority_fit"), SENIORITY_FITS, "unknown"),
        dutch_required=bool(data.get("dutch_required", False)),
        sponsorship_required=bool(data.get("sponsorship_required", False)),
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
