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
        skills={
            "core": ["PHP", "Laravel"],
            "proficient": ["ReactJS"],
            "familiar": ["Python"],
        },
        languages={"fluent": ["English"], "none": ["Dutch"]},
        dealbreakers=["requires fluent Dutch"],
    )


# Descriptions that land a vacancy in a known tier, so a test can predict the
# band its score will be clamped into.
CORE_JOB = {"title": "Laravel Developer", "description": "Build Laravel and PHP services."}
FAMILIAR_JOB = {"title": "Python Developer", "description": "Write Python all day."}
UNRELATED_JOB = {"title": "Go Engineer", "description": "Write Go and Kafka consumers."}


def valid_assessment(**overrides) -> str:
    payload = {
        "fit_score": 82,
        "verdict": "strong_match",
        "seniority": "senior",
        "stack_match": "core",
        "seniority_fit": "good",
        "location_policy": "unknown",
        "requires_existing_permit": False,
        "permit_evidence": "",
        "offers_sponsorship": False,
        "dutch_required": False,
        "sponsorship_required": False,
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

        result = assess_job(**CORE_JOB, profile=profile)

        assert result.fit_score == 82
        assert result.verdict == "strong_match"
        assert result.seniority == "senior"

    def test_uses_json_mode(self, monkeypatch, profile):
        """Constrained decoding is far more reliable than asking nicely."""
        fake = FakeLlm(valid_assessment())
        monkeypatch.setattr(service, "chat", fake)

        assess_job(**CORE_JOB, profile=profile)

        assert fake.calls[0]["json_format"] is True

    def test_records_the_profile_name(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment()))

        assert assess_job(**CORE_JOB, profile=profile).profile_name == "test"

    def test_parses_json_wrapped_in_prose(self, monkeypatch, profile):
        monkeypatch.setattr(
            service, "chat", FakeLlm(f"Sure! Here it is:\n{valid_assessment()}\nHope that helps.")
        )

        assert assess_job(**CORE_JOB, profile=profile).fit_score == 82

    def test_parses_json_in_code_fences(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(f"```json\n{valid_assessment()}\n```"))

        assert assess_job(**CORE_JOB, profile=profile).fit_score == 82

    def test_repairs_malformed_json_with_one_retry(self, monkeypatch, profile):
        fake = FakeLlm("{not valid json at all", valid_assessment())
        monkeypatch.setattr(service, "chat", fake)

        result = assess_job(**CORE_JOB, profile=profile)

        assert result.repaired is True
        assert result.fit_score == 82
        assert len(fake.calls) == 2

    def test_unrepairable_json_raises(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm("nonsense", "still nonsense"))

        with pytest.raises((ValueError, json.JSONDecodeError)):
            assess_job(**CORE_JOB, profile=profile)

    def test_score_is_clamped_into_the_tier_band(self, monkeypatch, profile):
        """A core-stack vacancy cannot score above its band however the model replies."""
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(fit_score=250)))

        assert assess_job(**CORE_JOB, profile=profile).fit_score == 95

    def test_score_below_the_band_is_raised_into_it(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(fit_score=-10)))

        assert assess_job(**CORE_JOB, profile=profile).fit_score == 75

    def test_non_numeric_score_falls_back_into_the_band(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(fit_score="very good")))

        assert assess_job(**CORE_JOB, profile=profile).fit_score == 75

    def test_invalid_verdict_is_derived_from_the_score(self, monkeypatch, profile):
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(verdict="amazing", fit_score=90))
        )

        assert assess_job(**CORE_JOB, profile=profile).verdict == "strong_match"

    def test_stack_tier_comes_from_code_not_the_model(self, monkeypatch, profile):
        """The model's own stack_match is ignored; the lookup is authoritative."""
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(stack_match="non_technical"))
        )

        assert assess_job(**CORE_JOB, profile=profile).stack_match == "core"

    def test_familiar_stack_scores_in_its_lower_band(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(fit_score=95)))

        result = assess_job(**FAMILIAR_JOB, profile=profile)

        assert result.stack_match == "familiar"
        assert result.fit_score == 55

    def test_unrelated_stack_scores_low_however_enthusiastic_the_model(
        self, monkeypatch, profile
    ):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(fit_score=99)))

        result = assess_job(**UNRELATED_JOB, profile=profile)

        assert result.stack_match == "unrelated"
        assert result.fit_score == 30

    def test_sponsorship_is_derived_in_code_not_taken_from_the_model(
        self, monkeypatch, profile
    ):
        """The model claimed an on-site internship needed no sponsorship because
        "the candidate is eligible for remote work" - which the vacancy never
        said. Anything but a confirmed remote role needs a permit.

        Needing one is a relocation, not a block: the candidate will move.
        """
        profile.location = {"work_authorization": "No EU work permit; needs sponsorship"}
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm(valid_assessment(sponsorship_required=False, remote="unknown", blocked=False)),
        )

        result = assess_job(**CORE_JOB, profile=profile)

        assert result.sponsorship_required is True
        assert result.eligibility == "relocation"
        assert result.blocked is False

    def test_a_fully_remote_role_needs_no_sponsorship(self, monkeypatch, profile):
        profile.location = {"work_authorization": "No EU work permit; needs sponsorship"}
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm(valid_assessment(remote="remote", blocked=False, dutch_required=False)),
        )

        result = assess_job(**CORE_JOB, profile=profile)

        assert result.sponsorship_required is False
        assert result.blocked is False

    def test_an_authorised_candidate_never_needs_sponsorship(self, monkeypatch, profile):
        profile.location = {"work_authorization": "EU citizen, free to work in the NL"}
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(remote="onsite", blocked=False))
        )

        assert assess_job(**CORE_JOB, profile=profile).sponsorship_required is False

    def test_dutch_requirement_always_blocks(self, monkeypatch, profile):
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(dutch_required=True, blocked=False))
        )

        assert assess_job(**CORE_JOB, profile=profile).blocked is True

    def test_seniority_fit_is_captured(self, monkeypatch, profile):
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(seniority_fit="under_qualified"))
        )

        assert assess_job(**CORE_JOB, profile=profile).seniority_fit == "under_qualified"

    def test_invalid_seniority_fit_becomes_unknown(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(seniority_fit="meh")))

        assert assess_job(**CORE_JOB, profile=profile).seniority_fit == "unknown"

    def test_invalid_seniority_becomes_unknown(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(seniority="wizard")))

        assert assess_job(**CORE_JOB, profile=profile).seniority == "unknown"

    def test_string_instead_of_list_is_coerced(self, monkeypatch, profile):
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(red_flags="requires Dutch"))
        )

        assert assess_job(**CORE_JOB, profile=profile).red_flags == ["requires Dutch"]

    def test_missing_fields_get_safe_defaults(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(json.dumps({"fit_score": 50})))

        result = assess_job(**CORE_JOB, profile=profile)

        # 50 is below the core band, so it is raised to its floor of 75. The
        # verdict is then derived from that: strong_match starts at 80, so the
        # bottom of the core band is "possible".
        assert result.verdict == "possible"
        assert result.red_flags == []
        assert result.summary == ""

    def test_blocked_is_captured_separately_from_the_score(self, monkeypatch, profile):
        """A great role that requires Dutch: high score AND blocked."""
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm(valid_assessment(fit_score=85, blocked=True, dutch_required=True)),
        )

        result = assess_job(**CORE_JOB, profile=profile)

        assert result.fit_score == 85
        assert result.verdict == "strong_match"
        assert result.blocked is True
        assert result.dutch_required is True

    def test_profile_text_reaches_the_prompt(self, monkeypatch, profile):
        fake = FakeLlm(valid_assessment())
        monkeypatch.setattr(service, "chat", fake)

        assess_job(**CORE_JOB, profile=profile)

        user_message = fake.calls[0]["messages"][1]["content"]
        assert "Backend Developer" in user_message
        assert "Dutch" in user_message

    def test_vacancy_facts_reach_the_prompt(self, monkeypatch, profile):
        fake = FakeLlm(valid_assessment())
        monkeypatch.setattr(service, "chat", fake)

        assess_job(
            **CORE_JOB,
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

        assert len(assess_job(**CORE_JOB, profile=profile).red_flags) <= 8


class TestEligibilityTiers:
    """Red / orange / green, decided in code from what the advert states.

    Red must come from the advert ruling the candidate out, not from the job
    merely being on site - the candidate is willing to relocate.
    """

    @pytest.fixture
    def needs_permit(self, profile):
        profile.location = {
            "work_authorization": "No EU work permit; needs sponsorship",
            "relocation": "Willing to relocate",
        }
        return profile

    def test_onsite_and_silent_on_permits_is_relocation_not_blocked(
        self, monkeypatch, needs_permit
    ):
        """The commonest case: a Dutch advert that never mentions permits."""
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm(valid_assessment(location_policy="onsite", requires_existing_permit=False)),
        )

        result = assess_job(**CORE_JOB, profile=needs_permit)

        assert result.eligibility == "relocation"
        assert result.blocked is False

    def test_an_explicit_permit_requirement_blocks(self, monkeypatch, needs_permit):
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm(
                valid_assessment(
                    requires_existing_permit=True,
                    permit_evidence="you must already hold a valid EU work permit",
                )
            ),
        )

        result = assess_job(
            title="Laravel Developer",
            description="We hire in Utrecht. Note: you must already hold a valid EU work permit.",
            profile=needs_permit,
        )

        assert result.eligibility == "blocked"
        assert result.requires_existing_permit is True

    def test_a_quote_that_is_not_in_the_advert_is_ignored(self, monkeypatch, needs_permit):
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm(
                valid_assessment(
                    requires_existing_permit=True,
                    permit_evidence="you must already hold a valid EU work permit",
                )
            ),
        )

        result = assess_job(**CORE_JOB, profile=needs_permit)  # advert says no such thing

        assert result.requires_existing_permit is False
        assert result.eligibility == "relocation"

    def test_a_real_quote_about_something_else_is_ignored(self, monkeypatch, needs_permit):
        """The model quoted "Laravel developer wanted for our Utrecht office."

        A real sentence from the advert that says nothing about permits. A
        verbatim check alone let that through.
        """
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm(
                valid_assessment(
                    requires_existing_permit=True,
                    permit_evidence="Build Laravel and PHP services.",
                )
            ),
        )

        result = assess_job(**CORE_JOB, profile=needs_permit)

        assert result.requires_existing_permit is False
        assert result.eligibility == "relocation"

    def test_fully_remote_is_green(self, monkeypatch, needs_permit):
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(location_policy="remote_worldwide"))
        )

        result = assess_job(**CORE_JOB, profile=needs_permit)

        assert result.eligibility == "remote_ok"
        assert result.sponsorship_required is False

    def test_dutch_requirement_still_blocks(self, monkeypatch, needs_permit):
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(dutch_required=True))
        )

        assert assess_job(**CORE_JOB, profile=needs_permit).eligibility == "blocked"

    def test_an_authorised_candidate_onsite_is_not_a_relocation(self, monkeypatch, profile):
        profile.location = {"work_authorization": "EU citizen, free to work in the NL"}
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(location_policy="onsite"))
        )

        result = assess_job(**CORE_JOB, profile=profile)

        assert result.sponsorship_required is False
        assert result.eligibility == "relocation"

    def test_sponsorship_offered_is_surfaced(self, monkeypatch, needs_permit):
        monkeypatch.setattr(
            service, "chat", FakeLlm(valid_assessment(offers_sponsorship=True))
        )

        result = assess_job(**CORE_JOB, profile=needs_permit)

        assert result.offers_sponsorship is True
        assert result.eligibility == "relocation"


class TestTruncatedAssessments:
    """Recovering an assessment that was cut off mid-write.

    About one reply in two hundred runs to the token ceiling: the model falls
    into a repetition loop and the JSON ends unterminated. fit_score and verdict
    are written first, so they are usually intact and worth keeping - redoing
    the call costs 20 seconds of GPU for no better answer.
    """

    def test_truncated_mid_string_keeps_the_finished_fields(self, monkeypatch, profile):
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm('{"fit_score": 82, "verdict": "strong_match", "summary": "This role invol'),
        )

        result = assess_job(**CORE_JOB, profile=profile)

        assert result.fit_score == 82
        assert result.verdict == "strong_match"

    def test_truncated_mid_array_keeps_the_finished_fields(self, monkeypatch, profile):
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm('{"fit_score": 90, "verdict": "strong_match", "red_flags": ["needs Dut'),
        )

        assert assess_job(**CORE_JOB, profile=profile).fit_score == 90

    def test_a_repetition_loop_is_still_salvaged(self, monkeypatch, profile):
        monkeypatch.setattr(
            service,
            "chat",
            FakeLlm('{"fit_score": 60, "verdict": "possible", "summary": "a a a a a a a a'),
        )

        assert assess_job(**CORE_JOB, profile=profile).verdict == "possible"

    def test_salvage_returns_none_when_there_is_no_object(self):
        assert service._close_truncated_json("I cannot assess this vacancy.") is None

    def test_a_reply_with_no_json_raises_a_typed_error(self, monkeypatch, profile):
        """Typed, so the workflow can leave the vacancy ungraded rather than die."""
        monkeypatch.setattr(
            service, "chat", FakeLlm("I cannot assess this.", "Still cannot.")
        )

        with pytest.raises(service.AssessmentUnparseable):
            assess_job(**CORE_JOB, profile=profile)

    def test_a_complete_reply_is_untouched_by_the_salvage_path(self, monkeypatch, profile):
        monkeypatch.setattr(service, "chat", FakeLlm(valid_assessment(fit_score=88)))

        result = assess_job(**CORE_JOB, profile=profile)

        assert result.fit_score == 88
        assert result.summary != ""
