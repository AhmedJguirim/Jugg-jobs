"""Grade a parsed listing, then create the real job (ADR-0015).

The order is the point: a listing is graded *before* anything is written, and the
job, its text and its grade are stored together. A listing that cannot be graded
(model down, unreadable reply) therefore leaves no half-made job behind, and one
that is graded is never visible without its grade.

Functions take and return plain dicts so the DBOS workflow can checkpoint each
stage, and so tests can call them without DBOS:

    prepare -> (GPU) grade -> persist
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.global_grader import assess_listing
from app.llm.global_prompts import GLOBAL_ASSESSMENT_PROMPT_VERSION
from app.llm.profile import load_profile
from app.llm.scoring import AssessmentUnparseable
from app.logging_setup import log
from app.repositories import enrichment as enrichment_repo
from app.repositories import job as job_repo
from app.schemas.job import JobCreate
from app.text import sanitize_description

# What the model reads is capped: the context window also holds the prompt and
# the profile, and a prompt that overflows it is truncated from the front, which
# would silently drop the instructions. Cut on a line boundary.
GRADE_MAX_CHARS = 14_000

# Marks the translation row of a vacancy that needed no translating.
SOURCE_TEXT_PROMPT_VERSION = "source-text-v1"


def resolve_profile_name(profile_name: Optional[str]) -> str:
    return profile_name or get_settings().llm_default_profile


def prepare(job: dict[str, Any], profile_name: Optional[str], session: Session) -> dict[str, Any]:
    """Clean the text, collect the facts the model sees, and say whether the
    stored grade (if any) is already current - the GPU's off switch."""
    payload = JobCreate.model_validate(job)
    profile = resolve_profile_name(profile_name)
    cleaned = sanitize_description(payload.description, max_chars=GRADE_MAX_CHARS)

    sections = (payload.raw or {}).get("sections") or {}
    facts = {label: value for label, value in sections.items() if value}
    if payload.salary_min is not None and "Compensation" not in facts:
        # The salary columns have no currency; the parser keeps it in raw, and
        # without it "259000-379000" reads as euros to the model.
        currency = ((payload.raw or {}).get("compensation") or {}).get("currency")
        facts["Salary"] = (
            f"{payload.salary_min}-{payload.salary_max}{' ' + currency if currency else ''} ({payload.salary_type})"
        )

    existing = job_repo.get_by_external_id(session, payload.source, payload.external_id)
    current = False
    if existing is not None:
        stored = enrichment_repo.get_assessment(session, existing.id, profile)
        current = enrichment_repo.assessment_is_current(
            stored,
            source_hash=cleaned.content_hash,
            prompt_version=GLOBAL_ASSESSMENT_PROMPT_VERSION,
        )

    return {
        "profile_name": profile,
        "title": payload.title,
        "company": payload.company_name,
        "facts": facts,
        "text": cleaned.text,
        "language": cleaned.language,
        "source_hash": cleaned.content_hash,
        "truncated": cleaned.truncated,
        "job_id": existing.id if existing is not None else None,
        "current": current,
    }


def grade(prepared: dict[str, Any]) -> dict[str, Any]:
    """GPU step. Returns the assessment as a dict, or `{}` if the reply was
    unreadable (the caller then stores nothing)."""
    profile = load_profile(prepared["profile_name"])
    try:
        outcome = assess_listing(
            title=prepared["title"],
            description=prepared["text"],
            profile=profile,
            company=prepared["company"],
            facts=prepared["facts"],
        )
    except AssessmentUnparseable as error:
        log.warning("listing %r: could not read a grade from the model (%s)", prepared["title"], error)
        return {}

    result = outcome.result
    return {
        "fit_score": result.fit_score,
        "verdict": result.verdict,
        "seniority": result.seniority,
        "seniority_fit": result.seniority_fit,
        "stack_match": result.stack_match,
        "remote": result.remote,
        "dutch_required": result.dutch_required,
        "sponsorship_required": result.sponsorship_required,
        "eligibility": result.eligibility,
        "location_policy": result.location_policy,
        "requires_existing_permit": result.requires_existing_permit,
        "offers_sponsorship": result.offers_sponsorship,
        "blocked": result.blocked,
        "summary": result.summary,
        "reasoning": result.reasoning,
        "matched_requirements": result.matched_requirements,
        "missing_requirements": result.missing_requirements,
        "red_flags": result.red_flags,
        "model": result.model,
        "repaired": result.repaired,
        "prompt_tokens": result.prompt_tokens,
        "output_tokens": result.output_tokens,
        "duration_ms": int(result.duration_s * 1000),
        "prompt_version": GLOBAL_ASSESSMENT_PROMPT_VERSION,
        "profile_name": prepared["profile_name"],
        "source_hash": prepared["source_hash"],
        # What the code made of the model's claims (kept for auditing a grade).
        "decisions": {
            "blockers": outcome.decisions.blockers,
            "flags": outcome.decisions.flags,
            "ignored": outcome.decisions.ignored,
            "languages": outcome.decisions.languages,
            "location_restriction": outcome.decisions.location_restriction,
        },
    }


def persist(
    session: Session,
    job: dict[str, Any],
    prepared: dict[str, Any],
    assessment: dict[str, Any],
) -> dict[str, Any]:
    """Create (or refresh) the job in `jobs` with its text and grade, together."""
    payload = JobCreate.model_validate(job)
    stored, created = job_repo.upsert(session, payload)

    # The text is already in the language we read it in, so the "English
    # translation" row (which the board's drawer shows) is the cleaned text
    # itself, marked as not produced by a model.
    enrichment_repo.upsert_translation(
        session,
        job_id=stored.id,
        target_language="en",
        source_language=prepared["language"] if prepared["language"] != "unknown" else "en",
        title=payload.title,
        description=prepared["text"],
        source_hash=prepared["source_hash"],
        skipped=True,
        skip_reason="published in English; stored as written",
        prompt_version=SOURCE_TEXT_PROMPT_VERSION,
    )
    enrichment_repo.upsert_assessment(
        session,
        job_id=stored.id,
        profile_name=assessment["profile_name"],
        source_hash=assessment["source_hash"],
        prompt_version=assessment["prompt_version"],
        fit_score=assessment["fit_score"],
        verdict=assessment["verdict"],
        seniority=assessment["seniority"],
        seniority_fit=assessment["seniority_fit"],
        stack_match=assessment["stack_match"],
        remote=assessment["remote"],
        dutch_required=assessment["dutch_required"],
        sponsorship_required=assessment["sponsorship_required"],
        eligibility=assessment["eligibility"],
        location_policy=assessment["location_policy"],
        requires_existing_permit=assessment["requires_existing_permit"],
        offers_sponsorship=assessment["offers_sponsorship"],
        blocked=assessment["blocked"],
        summary=assessment["summary"],
        reasoning=assessment["reasoning"],
        matched_requirements=assessment["matched_requirements"],
        missing_requirements=assessment["missing_requirements"],
        red_flags=assessment["red_flags"],
        details=assessment,
        model=assessment["model"],
        repaired=assessment["repaired"],
        prompt_tokens=assessment["prompt_tokens"],
        output_tokens=assessment["output_tokens"],
        duration_ms=assessment["duration_ms"],
    )
    return {
        "status": "stored",
        "job_id": stored.id,
        "created": created,
        "title": payload.title,
        "profile_name": assessment["profile_name"],
        "fit_score": assessment["fit_score"],
        "verdict": assessment["verdict"],
        "eligibility": assessment["eligibility"],
        "blocked": assessment["blocked"],
        "red_flags": assessment["red_flags"],
    }
