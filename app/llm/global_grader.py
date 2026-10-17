"""Grade a vacancy from any country against a candidate profile.

Counterpart of `service.assess_job` for sources that are not the Dutch job
board (ADR-0015). Same split of work, same reason (ADR-0008):

- **The model reports, with quotes.** Where the role may be done, which
  languages it needs, whether it demands a nationality or a clearance, whether it
  sponsors visas - each with the sentence from the advert that says so.
- **Code decides.** A restriction counts only when its quote appears verbatim in
  the text the model was given (and says what the claim needs it to say). Whether
  it then bars *this* candidate is a lookup against the profile: the languages
  they speak, the country they live in, the nationality they hold. The 4B model
  once "quoted" an office address as proof a permit was required; checking the
  quote in code is what stops that kind of inference reaching the database.
- **The stack tier is code too** (`service.classify_stack`), and clamps the score
  into its band exactly as for Dutch vacancies.

Nothing in `service.py` or `prompts.py` is changed or bypassed for NVB vacancies;
this module only imports from them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from app.integrations.ollama import chat
from app.llm import geo
from app.llm.global_prompts import GLOBAL_ASSESSMENT_PROMPT_VERSION, global_assessment_messages
from app.llm.profile import CandidateProfile
from app.llm.prompts import json_repair_messages
from app.llm.service import (
    ELIGIBILITY_BLOCKED,
    ELIGIBILITY_RELOCATION,
    ELIGIBILITY_REMOTE_OK,
    REMOTE_VALUES,
    SENIORITIES,
    SENIORITY_FITS,
    STACK_BANDS,
    VERDICTS,
    AssessmentResult,
    AssessmentUnparseable,
    _coerce_choice,
    _coerce_list,
    _evidence_is_real,
    _parse_assessment,
    _profile_has_eu_authorization,
    _score_to_verdict,
    classify_stack,
)
from app.logging_setup import log

# Languages a restriction can name, with the words an advert might use for them.
_LANGUAGE_WORDS = {
    "chinese": ("chinese", "mandarin", "cantonese", "putonghua"),
    "dutch": ("dutch", "nederlands", "flemish"),
    "german": ("german", "deutsch"),
    "french": ("french", "francais", "français"),
    "spanish": ("spanish", "espanol", "español", "castilian"),
    "portuguese": ("portuguese", "portugues", "português"),
    "russian": ("russian",),
    "arabic": ("arabic",),
    "japanese": ("japanese",),
    "korean": ("korean",),
    "hindi": ("hindi",),
    "turkish": ("turkish",),
    "polish": ("polish",),
    "italian": ("italian",),
}
_CITIZENSHIP_TERMS = ("citizen", "national of", "nationality", "passport")
# Levels that need real command of the language (anything but nice-to-have).
_STRONG_LEVELS = {"native", "fluent", "working"}
_MIN_QUOTE = 8


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def canon_language(name: str) -> str:
    lowered = _norm(name)
    for canon, words in _LANGUAGE_WORDS.items():
        if any(word in lowered for word in words):
            return canon
    return lowered


def _language_words(canon: str) -> tuple[str, ...]:
    return _LANGUAGE_WORDS.get(canon, (canon,))


def candidate_languages(profile: CandidateProfile) -> dict[str, str]:
    """canonical language -> "strong" | "basic", from the profile.

    native, fluent and proficient count as strong enough to be asked for at work;
    `none` is absence and is simply not listed.
    """
    spoken: dict[str, str] = {}
    for level, strength in (("native", "strong"), ("fluent", "strong"), ("proficient", "strong"), ("basic", "basic")):
        values = (profile.languages or {}).get(level) or []
        if isinstance(values, str):
            values = [values]
        for name in values:
            spoken.setdefault(canon_language(name), strength)
    return spoken


def candidate_country(profile: CandidateProfile) -> str:
    return geo.country_of(str((profile.location or {}).get("based_in") or ""))


def candidate_citizenships(profile: CandidateProfile) -> set[str]:
    """Countries the candidate holds citizenship of.

    The profile may list `citizenship`. When it does not, the only thing known is
    where they live, so that country stands in for it - which is why a role that
    demands any *other* nationality counts as barred.
    """
    declared = profile.raw.get("citizenship") if profile.raw else None
    if isinstance(declared, str):
        declared = [declared]
    if declared:
        return {c for c in (geo.nationality_country(x) for x in declared) if c}
    home = candidate_country(profile)
    return {home} if home else set()


def profile_text(profile: CandidateProfile) -> str:
    """The profile as labelled lines, without the Dutch-specific wording of
    `CandidateProfile.as_prompt_text` ("Meets Dutch education levels")."""
    spoken, absent = profile._language_line()
    location = profile.location or {}
    education = profile.education or {}
    lines = [f"Headline: {profile.headline or 'unspecified'}"]
    if profile.summary:
        lines.append(f"Summary: {profile.summary}")
    if profile.years_experience is not None:
        lines.append(f"Years of professional experience: {profile.years_experience}")
    lines += [
        f"Target seniority: {', '.join(profile.seniority) or 'any'}",
        f"Target roles: {', '.join(profile.target_roles) or 'any'}",
        "",
        "SKILL TIERS:",
        f"  CORE - used in production, daily: {', '.join(profile.core_skills) or 'unspecified'}",
        f"  PROFICIENT - used and comfortable: {', '.join(profile.proficient_skills) or 'none'}",
        f"  FAMILIAR - touched only: {', '.join(profile.peripheral_skills) or 'none'}",
    ]
    if education:
        lines.append(
            f"Highest education: {education.get('highest_level', 'unspecified')}"
            + (f" in {education['field']}" if education.get("field") else "")
        )
    if profile.differentiators:
        lines.append(f"Particular strengths: {'; '.join(profile.differentiators)}")
    if profile.domains:
        lines.append(f"Domains worked in: {', '.join(profile.domains)}")
    citizenship = profile.raw.get("citizenship") if profile.raw else None
    if isinstance(citizenship, list):
        citizenship = ", ".join(citizenship)
    lines += [
        "",
        "WHERE THE CANDIDATE CAN WORK (reported separately, never lowers fit_score):",
        f"  Languages spoken: {spoken}",
        f"  Languages NOT spoken: {absent}",
        f"  Currently based in: {location.get('based_in', 'unspecified')}",
        f"  Citizenship: {citizenship or 'not stated'}",
        f"  Work authorisation: {location.get('work_authorization', 'unspecified')}",
        f"  Relocation: {location.get('relocation', 'unspecified')}",
        f"  Work arrangement: {location.get('remote_preference', 'no preference')}",
        "",
        f"Not interested in: {', '.join(profile.not_interested_in) or 'nothing excluded'}",
        f"Dealbreakers: {', '.join(profile.dealbreakers) or 'none'}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deciding from the model's report
# ---------------------------------------------------------------------------


def _quoted(evidence: Any, haystack: str, *, terms: tuple[str, ...] = ()) -> bool:
    """A quote is real when it is in the text verbatim and, if `terms` are given,
    contains at least one of them (so a quote about something else does not
    support the claim)."""
    quote = _norm(str(evidence or "")).strip("\"' ")
    if len(quote) < _MIN_QUOTE or quote not in haystack:
        return False
    return not terms or any(term in quote for term in terms)


@dataclass
class Decisions:
    """What the code concluded from the model's report, kept for auditing."""

    blockers: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)  # claims dropped for lacking a real quote
    languages: list[dict] = field(default_factory=list)
    location_restriction: Optional[dict] = None
    remote_open: bool = False


def decide(
    data: dict, *, haystack: str, profile: CandidateProfile
) -> tuple[Decisions, dict[str, Any]]:
    """Turn the model's report into verified facts and an eligibility tier.

    Returns the decisions and a dict of the assessment fields they determine.
    `haystack` is the normalised text the model read (title, labelled facts,
    description); every quote is checked against it.
    """
    d = Decisions()
    spoken = candidate_languages(profile)
    country = candidate_country(profile)
    needs_permit = not _profile_has_eu_authorization(profile)
    remote = _coerce_choice(data.get("remote"), REMOTE_VALUES, "unknown")

    # --- languages ---
    dutch_required = False
    entries = data.get("languages_required")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("language") or "").strip()
        canon = canon_language(name)
        level = _coerce_choice(entry.get("level"), {"native", "fluent", "working", "basic", "preferred"}, "preferred")
        if not name or (canon == "english" and "english" in spoken):
            continue
        real = _quoted(entry.get("evidence"), haystack, terms=_language_words(canon))
        record = {"language": name, "level": level, "verified": real}
        d.languages.append(record)
        if not real:
            d.ignored.append(f"language {name}: no matching quote in the advert")
            continue
        have = spoken.get(canon)
        if level == "preferred":
            if have is None:
                d.flags.append(f"{name.title()} preferred")
            continue
        enough = have == "strong" or (have == "basic" and level == "basic")
        if not enough:
            d.blockers.append(f"requires {level} {name.title()}")
            dutch_required = dutch_required or canon == "dutch"

    # --- citizenship ---
    if bool(data.get("citizenship_required")):
        if _quoted(data.get("citizenship_evidence"), haystack, terms=_CITIZENSHIP_TERMS):
            wanted = [str(n) for n in (data.get("citizenship_nationalities") or []) if str(n).strip()]
            wanted_countries = {c for c in (geo.nationality_country(n) for n in wanted) if c}
            held = candidate_citizenships(profile)
            label = ", ".join(wanted) or "a specific country"
            if not wanted_countries & held:
                d.blockers.append(f"requires citizenship of {label}")
        else:
            d.ignored.append("citizenship: no matching quote in the advert")

    # --- security clearance ---
    if bool(data.get("security_clearance_required")):
        if _quoted(data.get("clearance_evidence"), haystack, terms=("clearance",)):
            d.blockers.append("requires a security clearance")
        else:
            d.ignored.append("security clearance: no matching quote in the advert")

    # --- where the person must be ---
    scope = _coerce_choice(data.get("remote_scope"), {"worldwide", "countries", "region", "unknown"}, "unknown")
    places = [str(p) for p in (data.get("remote_places") or []) if str(p).strip()]
    restricted_out = False
    if scope in ("countries", "region") and places:
        mentions_place = any(_norm(p) in _norm(str(data.get("location_evidence") or "")) for p in places)
        if _quoted(data.get("location_evidence"), haystack) and mentions_place:
            includes = geo.places_include(places, country)
            d.location_restriction = {"places": places, "includes_candidate": includes, "country": country}
            if not includes:
                restricted_out = True
                d.blockers.append(f"remote only within {', '.join(places)}")
        else:
            d.ignored.append("remote restriction: no matching quote in the advert")

    # --- permits and sponsorship ---
    sponsorship = _coerce_choice(data.get("sponsorship"), {"offered", "not_offered", "unknown"}, "unknown")
    permit_claimed = bool(data.get("requires_existing_permit"))
    permit_real = permit_claimed and _evidence_is_real(str(data.get("permit_evidence") or ""), haystack)
    if permit_claimed and not permit_real:
        d.ignored.append("permit requirement: no matching quote in the advert")
    no_sponsor_real = sponsorship == "not_offered" and _quoted(
        data.get("permit_evidence"), haystack, terms=("sponsor", "visa")
    )

    remote_open = remote == "remote" and not restricted_out
    if needs_permit and not remote_open:
        if permit_real:
            d.blockers.append("needs an existing right to work")
        elif no_sponsor_real:
            d.blockers.append("does not sponsor visas")
    d.remote_open = remote_open

    # --- the tier ---
    if d.blockers:
        eligibility = ELIGIBILITY_BLOCKED
    elif remote_open:
        eligibility = ELIGIBILITY_REMOTE_OK
    else:
        eligibility = ELIGIBILITY_RELOCATION

    if restricted_out:
        location_policy = "remote_restricted"
    elif remote == "remote" and scope == "worldwide" and _quoted(data.get("location_evidence"), haystack):
        location_policy = "remote_worldwide"
    elif remote == "remote":
        location_policy = "remote_unspecified"
    elif remote in ("onsite", "hybrid"):
        location_policy = remote
    else:
        location_policy = "unknown"

    for other in _coerce_list(data.get("other_restrictions"), limit=4):
        d.flags.append(other)

    fields = {
        "eligibility": eligibility,
        "blocked": eligibility == ELIGIBILITY_BLOCKED,
        "dutch_required": dutch_required,
        "requires_existing_permit": permit_real,
        "offers_sponsorship": sponsorship == "offered",
        "sponsorship_required": needs_permit and not remote_open,
        "location_policy": location_policy,
        "remote": remote,
    }
    return d, fields


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


@dataclass
class GradeOutcome:
    result: AssessmentResult
    decisions: Decisions


def _readable_text(title: str, facts: dict[str, str], description: str) -> str:
    """Exactly what the model reads, so quotes can be checked against it."""
    fact_lines = "\n".join(f"{label}: {value}" for label, value in facts.items() if value)
    return _norm("\n".join([title or "", fact_lines, description or ""]))


def assess_listing(
    *,
    title: str,
    description: str,
    profile: CandidateProfile,
    company: Optional[str] = None,
    facts: Optional[dict[str, str]] = None,
    model: Optional[str] = None,
) -> GradeOutcome:
    """Grade one vacancy. Raises `AssessmentUnparseable` when the model's reply
    cannot be read even after a repair and a salvage attempt."""
    facts = facts or {}
    stack_match, matched_skills = classify_stack(title, description, profile)
    low, high = STACK_BANDS[stack_match]

    messages = global_assessment_messages(
        title=title,
        company=company,
        facts=facts,
        description=description,
        profile_text=profile_text(profile),
        stack_match=stack_match,
        matched_skills=matched_skills,
        band=(low, high),
    )
    result = chat(messages, model=model, temperature=0.1, think=False, json_format=True, num_predict=1800)

    repaired = False
    try:
        data = _parse_assessment(result.content)
    except (ValueError, json.JSONDecodeError):
        log.warning("global assessment JSON unparseable, attempting one repair")
        repair = chat(
            json_repair_messages(result.content),
            model=model, temperature=0.0, think=False, json_format=True, num_predict=1800,
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
    score = max(low, min(high, max(0, min(100, score))))

    decisions, fields = decide(data, haystack=_readable_text(title, facts, description), profile=profile)

    red_flags = list(dict.fromkeys(
        decisions.blockers + _coerce_list(data.get("red_flags"), limit=8) + decisions.flags
    ))[:12]

    assessment = AssessmentResult(
        fit_score=score,
        verdict=_coerce_choice(data.get("verdict"), VERDICTS, _score_to_verdict(score)),
        seniority=_coerce_choice(data.get("seniority"), SENIORITIES, "unknown"),
        stack_match=stack_match,
        seniority_fit=_coerce_choice(data.get("seniority_fit"), SENIORITY_FITS, "unknown"),
        summary=str(data.get("summary") or "").strip()[:2000],
        matched_requirements=_coerce_list(data.get("matched_requirements")),
        missing_requirements=_coerce_list(data.get("missing_requirements")),
        red_flags=red_flags,
        reasoning=str(data.get("reasoning") or "").strip()[:2000],
        model=result.model,
        prompt_version=GLOBAL_ASSESSMENT_PROMPT_VERSION,
        profile_name=profile.name,
        prompt_tokens=result.prompt_tokens,
        output_tokens=result.output_tokens,
        duration_s=result.duration_s,
        repaired=repaired,
        **fields,
    )
    return GradeOutcome(result=assessment, decisions=decisions)
