"""Local-LLM enrichment: prompts, candidate profiles, translation and grading."""

from app.llm.profile import CandidateProfile, load_profile
from app.llm.service import (
    AssessmentResult,
    TranslationResult,
    assess_job,
    translate_text,
    translate_title,
)

__all__ = [
    "AssessmentResult",
    "CandidateProfile",
    "TranslationResult",
    "assess_job",
    "load_profile",
    "translate_text",
    "translate_title",
]
