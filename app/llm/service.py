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
LOCATION_POLICIES = {"remote_worldwide", "remote_eu_only", "hybrid", "onsite", "unknown"}

# Three tiers, decided in code from the evidence the model reports:
#   blocked    (red)    - the advert itself rules the candidate out
#   relocation (orange) - reachable, but needs a move or a visa conversation
#   remote_ok  (green)  - doable from where the candidate already is
ELIGIBILITY_BLOCKED = "blocked"
ELIGIBILITY_RELOCATION = "relocation"
ELIGIBILITY_REMOTE_OK = "remote_ok"
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
    # Evidence from the advert, and the tier derived from it.
    location_policy: str = "unknown"
    requires_existing_permit: bool = False
    offers_sponsorship: bool = False
    eligibility: str = ELIGIBILITY_RELOCATION
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


def _profile_has_eu_authorization(profile: CandidateProfile) -> bool:
    """Whether the candidate may already work in the target country.

    Read from the profile's `location.work_authorization` text: a profile that
    says it holds no permit, or needs sponsorship, is treated as unauthorised.
    Absent or empty, the candidate is assumed authorised - the conservative
    reading for a profile that never mentions the subject.
    """
    text = str((profile.location or {}).get("work_authorization") or "").lower()
    if not text:
        return True
    lacks = ("no eu", "no work permit", "needs visa", "needs sponsorship",
             "requires sponsorship", "no netherlands work permit", "without a permit")
    return not any(phrase in text for phrase in lacks)


# Words that make a sentence actually about the right to work or to live
# somewhere. A quote without one of these is about something else.
_PERMIT_TERMS = (
    "permit", "visa", "sponsor", "right to work", "work authorisation",
    "work authorization", "authorised to work", "authorized to work",
    "eligible to work", "already live", "currently live", "currently residing",
    "must reside", "must be based", "based in the eu", "resident of",
    "eu citizen", "eu/eea", "verblijfsvergunning", "werkvergunning",
    "woonachtig", "visumsponsoring", "tewerkstellingsvergunning",
)


def _evidence_is_real(evidence: str, description: str) -> bool:
    """Check a quoted sentence exists in the advert AND is about permits.

    Two hurdles, because the model cleared the first one dishonestly. Asked to
    quote the sentence requiring a permit, it returned "Laravel developer wanted
    for our Utrecht office." - a real sentence from a real advert that says
    nothing about permits. Verbatim matching alone let an inference through
    wearing a quotation as a disguise.

    So the quote must appear in the text *and* contain language about the right
    to work or to live somewhere. Anything else is the model reasoning from the
    job being on site, which is precisely the inference this flag must not make.
    """
    quote = " ".join((evidence or "").split()).strip().strip('"').lower()
    if len(quote) < 12:  # too short to be a real sentence
        return False
    if not any(term in quote for term in _PERMIT_TERMS):
        return False
    haystack = " ".join((description or "").split()).lower()
    return quote in haystack


def _score_to_verdict(score: int) -> str:
    if score >= 80:
        return "strong_match"
    if score >= 55:
        return "possible"
    if score >= 25:
        return "weak"
    return "irrelevant"


class AssessmentUnparseable(ValueError):
    """The model never produced JSON we could read, even after repair."""


def _close_truncated_json(text: str) -> Optional[dict]:
    """Rescue an object that was cut off mid-write.

    About one assessment in two hundred runs to the token ceiling: the model
    falls into a repetition loop and the reply ends mid-string, so the JSON has
    no closing brace. The fields it had already finished - which include
    fit_score and verdict, written first - are perfectly good, and throwing them
    away costs 20 seconds of GPU to produce something no better.

    So: drop the unterminated tail, then close whatever brackets are still open.
    Returns None when there is nothing recoverable.
    """
    start = text.find("{")
    if start == -1:
        return None
    body = text[start:]

    # Walk the text tracking structure, remembering the last point at which the
    # object was syntactically complete enough to truncate cleanly.
    stack: list[str] = []
    in_string = False
    escaped = False
    cut = None

    for index, char in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if stack:
                stack.pop()
        elif char == "," and not stack[1:]:
            # A comma at the top level of the object: everything before it is a
            # complete set of key/value pairs.
            cut = index

    if in_string or stack:
        if cut is None:
            return None
        candidate = body[:cut] + "}"
    else:
        candidate = body

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_assessment(payload: str) -> dict:
    """Parse the model's JSON, tolerating surrounding text and truncation."""
    text = _clean_output(payload)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost object in the response.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    salvaged = _close_truncated_json(text)
    if salvaged is not None:
        log.info("assessment JSON was truncated; salvaged %s field(s)", len(salvaged))
        return salvaged

    raise AssessmentUnparseable("no JSON object in model output")


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
    truncated = result.output_tokens and result.output_tokens >= 1400
    try:
        data = _parse_assessment(result.content)
    except (ValueError, json.JSONDecodeError):
        log.warning(
            "assessment JSON unparseable%s, attempting one repair",
            " (output hit the token ceiling)" if truncated else "",
        )
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
    remote = _coerce_choice(data.get("remote"), REMOTE_VALUES, "unknown")
    dutch_required = bool(data.get("dutch_required", False))

    # Work authorisation is a rule, not a judgement, so it is applied here.
    # The model claimed one on-site internship needed no sponsorship because
    # "the candidate is eligible for remote work" - which the vacancy never
    # said; its own `remote` field was "unknown". On a Dutch job board, a role
    # that is not explicitly fully remote is in the Netherlands, and a candidate
    # without an EU permit needs sponsorship for it. Anything but a confirmed
    # remote role therefore requires sponsorship.
    needs_permit = not _profile_has_eu_authorization(profile)
    location_policy = _coerce_choice(
        data.get("location_policy"), LOCATION_POLICIES, "unknown"
    )
    # Only an explicit statement counts, and only one we can find in the text:
    # the model claimed this for adverts that never mentioned permits.
    claimed_permit = bool(data.get("requires_existing_permit", False))
    permit_evidence = str(data.get("permit_evidence") or "")
    requires_existing_permit = claimed_permit and _evidence_is_real(
        permit_evidence, description
    )
    if claimed_permit and not requires_existing_permit:
        log.info(
            "ignoring an unevidenced permit requirement; the quoted sentence "
            "is not in the advert: %r",
            permit_evidence[:80],
        )
    offers_sponsorship = bool(data.get("offers_sponsorship", False))

    # Doable from outside the EU, so no permit question arises at all.
    remote_from_anywhere = location_policy == "remote_worldwide" or remote == "remote"

    # Hard blocks first, and they apply whatever the candidate's permit status:
    # a Dutch-language requirement rules out someone who does not read Dutch
    # whether or not they may legally work here. Checking permits first meant
    # the language requirement was silently skipped for an authorised profile.
    hard_blocked = (
        dutch_required
        or requires_existing_permit
        or bool(data.get("blocked", False))
    )

    if hard_blocked:
        eligibility = ELIGIBILITY_BLOCKED
    elif remote_from_anywhere:
        eligibility = ELIGIBILITY_REMOTE_OK
    else:
        # Reachable: the candidate is willing to relocate, and sponsorship is a
        # conversation rather than a closed door. For a candidate who already
        # holds the right to work, this simply means "on site".
        eligibility = ELIGIBILITY_RELOCATION

    sponsorship_required = needs_permit and not remote_from_anywhere
    blocked = eligibility == ELIGIBILITY_BLOCKED

    return AssessmentResult(
        fit_score=score,
        verdict=verdict,
        seniority=_coerce_choice(data.get("seniority"), SENIORITIES, "unknown"),
        stack_match=stack_match,
        seniority_fit=_coerce_choice(data.get("seniority_fit"), SENIORITY_FITS, "unknown"),
        dutch_required=dutch_required,
        sponsorship_required=sponsorship_required,
        blocked=blocked,
        eligibility=eligibility,
        location_policy=location_policy,
        requires_existing_permit=requires_existing_permit,
        offers_sponsorship=offers_sponsorship,
        remote=remote,
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
