"""Unit tests for the translation and assessment service.

The model is replaced with a stub, so these run fast, need no GPU, and can
reproduce the specific ways a small model misbehaves (code fences, preambles,
malformed JSON, out-of-range scores).
"""

from __future__ import annotations

import json

import pytest

from app.llm import service
from app.llm.profile import CandidateProfile
from app.llm.service import assess_job, translate_text, translate_title


class FakeLlm:
    """Records calls and returns queued replies."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        content = self.replies.pop(0) if self.replies else "default reply"
        return service.LlmResult(
            content=content,
            model="qwen3.5:4b",
            prompt_tokens=10,
            output_tokens=20,
            duration_s=0.5,
            raw={"eval_duration": 500_000_000},
        )


@pytest.fixture
def profile() -> CandidateProfile:
    return CandidateProfile(
        name="test",
        headline="Backend engineer",
        target_roles=["Backend Developer"],
        strong_skills=["Python"],
        languages={"fluent": ["English"], "none": ["Dutch"]},
        dealbreakers=["requires fluent Dutch"],
    )


def valid_assessment(**overrides) -> str:
    payload = {
        "fit_score": 82,
        "verdict": "strong_match",
        "seniority": "senior",
        "dutch_required": False,
        "blocked": False,
        "remote": "hybrid",
        "summary": "A backend role using Python.",
        "matched_requirements": ["Python experience"],
        "missing_requirements": [],
        "red_flags": [],
        "reasoning": "Matches the target role.",
    }
    payload.update(overrides)
    return json.dumps(payload)


# ---- translation ----


class TestTranslateText:
    def test_translates_and_returns_text(self, monkeypatch):
        monkeypatch.setattr(service, "chat", FakeLlm("Translated text"))

        result = translate_text("Nederlandse tekst")

        assert result.text == "Translated text"
        assert result.skipped is False

    def test_english_input_skips_the_model_entirely(self, monkeypatch):
        fake = FakeLlm("should not be called")
        monkeypatch.setattr(service, "chat", fake)

        result = translate_text("Already English", source_language="en")

        assert result.skipped is True
        assert result.skip_reason == "already English"
        assert fake.calls == []  # the GPU was never touched

    def test_empty_input_skips(self, monkeypatch):
        monkeypatch.setattr(service, "chat", FakeLlm())

        assert translate_text("   ").skipped is True

    def test_long_text_is_chunked_into_several_calls(self, monkeypatch):
        fake = FakeLlm("a", "b", "c", "d", "e", "f", "g", "h")
        monkeypatch.setattr(service, "chat", fake)
        text = "\n".join(f"Regel {i} met wat inhoud erin" for i in range(60))

        result = translate_text(text, chunk_chars=300)

        assert result.chunks > 1
        assert len(fake.calls) == result.chunks

    def test_token_counts_accumulate_across_chunks(self, monkeypatch):
        monkeypatch.setattr(service, "chat", FakeLlm("a", "b", "c", "d", "e", "f", "g", "h"))
        text = "\n".join(f"Regel {i} met wat inhoud erin" for i in range(60))

        result = translate_text(text, chunk_chars=300)

        assert result.output_tokens == 20 * result.chunks

    def test_thinking_is_disabled(self, monkeypatch):
        """Thinking cost 4547 tokens vs 163 for the same quality; it must stay off."""
        fake = FakeLlm("Translated")
        monkeypatch.setattr(service, "chat", fake)

        translate_text("Nederlandse tekst")

        assert fake.calls[0]["think"] is False

    def test_temperature_is_low_for_faithfulness(self, monkeypatch):
        fake = FakeLlm("Translated")
        monkeypatch.setattr(service, "chat", fake)

        translate_text("Nederlandse tekst")

        assert fake.calls[0]["temperature"] <= 0.3

    def test_code_fences_are_stripped(self, monkeypatch):
        monkeypatch.setattr(service, "chat", FakeLlm("```\nTranslated text\n```"))

        assert translate_text("tekst").text == "Translated text"

    def test_preamble_is_stripped(self, monkeypatch):
        monkeypatch.setattr(service, "chat", FakeLlm("Here is the translation: Translated"))

        assert translate_text("tekst").text == "Translated"


class TestTranslateTitle:
    def test_translates_a_title(self, monkeypatch):
        monkeypatch.setattr(service, "chat", FakeLlm("Registered Nurse"))

        assert translate_title("Verpleegkundige") == "Registered Nurse"

    def test_english_title_is_returned_unchanged(self, monkeypatch):
        fake = FakeLlm()
        monkeypatch.setattr(service, "chat", fake)

        assert translate_title("Backend Developer", source_language="en") == "Backend Developer"
        assert fake.calls == []

    def test_quotes_are_stripped(self, monkeypatch):
        monkeypatch.setattr(service, "chat", FakeLlm('"Registered Nurse"'))

        assert translate_title("Verpleegkundige") == "Registered Nurse"

    def test_only_the_first_line_is_used(self, monkeypatch):
        monkeypatch.setattr(service, "chat", FakeLlm("Registered Nurse\nsome commentary"))

        assert translate_title("Verpleegkundige") == "Registered Nurse"

    def test_rambling_output_falls_back_to_the_original(self, monkeypatch):
        """A 4B sometimes explains instead of answering; keep the original title."""
        monkeypatch.setattr(service, "chat", FakeLlm("x" * 500))

        assert translate_title("Verpleegkundige") == "Verpleegkundige"

    def test_empty_output_falls_back_to_the_original(self, monkeypatch):
        monkeypatch.setattr(service, "chat", FakeLlm(""))

        assert translate_title("Verpleegkundige") == "Verpleegkundige"


# ---- assessment ----


class TestAssessJob:
    def test_parses_a_valid_assessment(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment()))

        result = assess_job(title="Backend Developer", description="Python role", profile=profile)

        assert result.fit_score == 82
        assert result.verdict == "strong_match"
        assert result.seniority == "senior"

    def test_uses_json_mode(self, monkeypatch, profile):
        """Constrained decoding is far more reliable than asking nicely."""
        fake = FakeLlm(valid_assessment())
        monkeypatch.setattr(service, "chat", fake)

        assess_job(title="t", description="d", profile=profile)

        assert fake.calls[0]["json_format"] is True

    def test_records_the_profile_name(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment()))

        assert assess_job(title="t", description="d", profile=profile).profile_name == "test"

    def test_parses_json_wrapped_in_prose(self, monkeypatch, profile):
        monkeypatch.setattr(
            service, "chat", FakeLlm(f"Sure! Here it is:\n{valid_assessment()}\nHope that helps.")
        )

        assert assess_job(title="t", description="d", profile=profile).fit_score == 82

    def test_parses_json_in_code_fences(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(f"```json\n{valid_assessment()}\n```"))

        assert assess_job(title="t", description="d", profile=profile).fit_score == 82

    def test_repairs_malformed_json_with_one_retry(self, monkeypatch, profile):
        fake = FakeLlm("{not valid json at all", valid_assessment())
        monkeypatch.setattr(service, "chat", fake)

        result = assess_job(title="t", description="d", profile=profile)

        assert result.repaired is True
        assert result.fit_score == 82
        assert len(fake.calls) == 2

    def test_unrepairable_json_raises(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm("nonsense", "still nonsense"))

        with pytest.raises((ValueError, json.JSONDecodeError)):
            assess_job(title="t", description="d", profile=profile)

    def test_score_above_range_is_clamped(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(fit_score=250)))

        assert assess_job(title="t", description="d", profile=profile).fit_score == 100

    def test_negative_score_is_clamped(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(fit_score=-10)))

        assert assess_job(title="t", description="d", profile=profile).fit_score == 0

    def test_non_numeric_score_becomes_zero(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(fit_score="very good")))

        assert assess_job(title="t", description="d", profile=profile).fit_score == 0

    def test_invalid_verdict_is_derived_from_the_score(self, monkeypatch, profile):
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(verdict="amazing", fit_score=90))
        )

        assert assess_job(title="t", description="d", profile=profile).verdict == "strong_match"

    def test_invalid_seniority_becomes_unknown(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(seniority="wizard")))

        assert assess_job(title="t", description="d", profile=profile).seniority == "unknown"

    def test_string_instead_of_list_is_coerced(self, monkeypatch, profile):
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(red_flags="requires Dutch"))
        )

        assert assess_job(title="t", description="d", profile=profile).red_flags == ["requires Dutch"]

    def test_missing_fields_get_safe_defaults(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(json.dumps({"fit_score": 50})))

        result = assess_job(title="t", description="d", profile=profile)

        assert result.verdict == "weak"
        assert result.red_flags == []
        assert result.summary == ""

    def test_blocked_is_captured_separately_from_the_score(self, monkeypatch, profile):
        """A great role that requires Dutch: high score AND blocked."""
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm(valid_assessment(fit_score=85, blocked=True, dutch_required=True)),
        )

        result = assess_job(title="t", description="d", profile=profile)

        assert result.fit_score == 85
        assert result.verdict == "strong_match"
        assert result.blocked is True
        assert result.dutch_required is True

    def test_profile_text_reaches_the_prompt(self, monkeypatch, profile):
        fake = FakeLlm(valid_assessment())
        monkeypatch.setattr(service, "chat", fake)

        assess_job(title="t", description="d", profile=profile)

        user_message = fake.calls[0]["messages"][1]["content"]
        assert "Backend Developer" in user_message
        assert "Dutch" in user_message

    def test_vacancy_facts_reach_the_prompt(self, monkeypatch, profile):
        fake = FakeLlm(valid_assessment())
        monkeypatch.setattr(service, "chat", fake)

        assess_job(
            title="Python Developer",
            description="body",
            profile=profile,
            company="Acme",
            location="Utrecht",
            salary="4000-6000",
        )

        user_message = fake.calls[0]["messages"][1]["content"]
        assert "Acme" in user_message
        assert "Utrecht" in user_message
        assert "4000-6000" in user_message

    def test_long_lists_are_capped(self, monkeypatch, profile):
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm(valid_assessment(red_flags=[f"flag {i}" for i in range(40)])),
        )

        assert len(assess_job(title="t", description="d", profile=profile).red_flags) <= 8
