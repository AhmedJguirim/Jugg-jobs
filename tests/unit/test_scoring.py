"""The rules decided in code rather than by the model (`app/llm/scoring.py`).

The stack tier has its own file (`test_stack_classification.py`); the full grade
is exercised through the grader in `test_global_grader.py`.
"""

from __future__ import annotations

import pytest

from app.llm import scoring
from app.llm.profile import CandidateProfile


class TestParsingTheReply:
    def test_plain_json(self):
        assert scoring._parse_assessment('{"fit_score": 80}') == {"fit_score": 80}

    def test_json_in_code_fences(self):
        assert scoring._parse_assessment('```json\n{"fit_score": 80}\n```') == {"fit_score": 80}

    def test_json_wrapped_in_prose(self):
        reply = 'Here is my assessment: {"fit_score": 70, "verdict": "possible"} Hope it helps.'
        assert scoring._parse_assessment(reply)["verdict"] == "possible"

    def test_a_reply_with_no_json_raises_a_typed_error(self):
        """Typed, so the workflow can leave the listing ungraded rather than die."""
        with pytest.raises(scoring.AssessmentUnparseable):
            scoring._parse_assessment("I cannot assess this.")


class TestTruncatedReplies:
    """Recovering a reply that was cut off mid-write.

    About one reply in two hundred runs to the token ceiling: the model falls
    into a repetition loop and the JSON ends unterminated. fit_score and verdict
    are written first, so they are usually intact and worth keeping - redoing
    the call costs 20 seconds of GPU for no better answer.
    """

    def test_truncated_mid_string_keeps_the_finished_fields(self):
        data = scoring._parse_assessment('{"fit_score": 82, "verdict": "strong_match", "summary": "This role invol')
        assert (data["fit_score"], data["verdict"]) == (82, "strong_match")

    def test_truncated_mid_array_keeps_the_finished_fields(self):
        data = scoring._parse_assessment('{"fit_score": 90, "verdict": "strong_match", "red_flags": ["needs Ger')
        assert data["fit_score"] == 90

    def test_a_repetition_loop_is_still_salvaged(self):
        data = scoring._parse_assessment('{"fit_score": 60, "verdict": "possible", "summary": "a a a a a a a a')
        assert data["verdict"] == "possible"

    def test_salvage_returns_none_when_there_is_no_object(self):
        assert scoring._close_truncated_json("I cannot assess this posting.") is None


class TestPermitEvidence:
    """A permit requirement counts only when its quote is in the advert AND is
    about the right to work."""

    ADVERT = "We hire in Berlin. Build Laravel and PHP services. You must already hold a valid EU work permit."

    def test_a_real_quote_about_permits_counts(self):
        assert scoring._evidence_is_real("You must already hold a valid EU work permit.", self.ADVERT)

    def test_a_quote_that_is_not_in_the_advert_is_ignored(self):
        assert not scoring._evidence_is_real("Applicants must be US citizens.", self.ADVERT)

    def test_a_real_quote_about_something_else_is_ignored(self):
        """The model once "quoted" a real sentence that said nothing about permits."""
        assert not scoring._evidence_is_real("Build Laravel and PHP services.", self.ADVERT)

    def test_whitespace_and_case_do_not_matter(self):
        assert scoring._evidence_is_real("you must   already hold a valid eu work permit", self.ADVERT)

    def test_a_fragment_too_short_to_be_a_sentence_is_ignored(self):
        assert not scoring._evidence_is_real("permit", self.ADVERT)


class TestWorkAuthorisation:
    @pytest.mark.parametrize(
        "text, authorised",
        [
            ("", True),
            ("EU citizen, free to work anywhere in the EU", True),
            ("No EU work permit; needs sponsorship", False),
            ("Requires sponsorship for on-site roles", False),
        ],
    )
    def test_read_from_the_profile(self, text, authorised):
        profile = CandidateProfile(name="p", location={"work_authorization": text})
        assert scoring._profile_has_eu_authorization(profile) is authorised


@pytest.mark.parametrize(
    "score, verdict",
    [(95, "strong_match"), (80, "strong_match"), (60, "possible"), (30, "weak"), (5, "irrelevant")],
)
def test_a_verdict_follows_from_the_score(score, verdict):
    assert scoring._score_to_verdict(score) == verdict


def test_a_choice_outside_the_allowed_set_falls_back():
    assert scoring._coerce_choice("Senior", scoring.SENIORITIES, "unknown") == "senior"
    assert scoring._coerce_choice("wizard", scoring.SENIORITIES, "unknown") == "unknown"


def test_a_string_where_a_list_belongs_is_coerced_and_capped():
    assert scoring._coerce_list("just one") == ["just one"]
    assert len(scoring._coerce_list([str(n) for n in range(20)])) == 8
