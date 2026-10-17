"""The grader for vacancies from anywhere (ADR-0015), with the model stubbed.

The model only *reports*; these tests pin down what the code does with a report:
a restriction counts only with a real quote, and whether it bars the candidate is
a lookup against the profile.
"""

from __future__ import annotations

import json

import pytest

from app.llm import geo, global_grader
from app.llm.global_grader import assess_listing, decide, profile_text
from app.llm.global_prompts import GLOBAL_ASSESSMENT_PROMPT_VERSION
from app.llm.profile import CandidateProfile
from app.llm.service import AssessmentUnparseable, LlmResult


def make_profile(**overrides) -> CandidateProfile:
    data = {
        "name": "t",
        "headline": "Backend engineer",
        "target_roles": ["Backend Developer"],
        "skills": {"core": ["PHP", "Laravel"], "proficient": ["ReactJS"], "familiar": ["Python"]},
        "languages": {"native": ["Arabic"], "fluent": ["English"], "proficient": ["French"], "none": ["Dutch"]},
        "location": {
            "based_in": "Sousse, Tunisia",
            "work_authorization": "No EU work permit. Needs visa sponsorship.",
        },
    }
    data.update(overrides)
    return CandidateProfile.from_dict(data)


TEXT = (
    "Senior PHP Engineer\n"
    "Location: EU Remote\n"
    "We build with Laravel. You must be fluent in Mandarin Chinese to talk to our clients. "
    "Applicants must be a Russian citizen. A security clearance is required for this role. "
    "We can only hire in the UK and select EU countries on a remote basis. "
    "We do not sponsor visas. French is a plus."
).lower()


def report(**fields) -> dict:
    base = {"remote": "unknown", "remote_scope": "unknown", "remote_places": [], "sponsorship": "unknown"}
    base.update(fields)
    return base


class TestLanguages:
    def test_a_required_language_the_candidate_lacks_blocks(self):
        d, f = decide(
            report(languages_required=[{"language": "Mandarin", "level": "fluent",
                                        "evidence": "You must be fluent in Mandarin Chinese to talk to our clients"}]),
            haystack=TEXT, profile=make_profile(),
        )

        assert f["blocked"] and f["eligibility"] == "blocked"
        assert "requires fluent Mandarin" in d.blockers
        assert f["dutch_required"] is False

    def test_a_quote_that_is_not_in_the_advert_is_ignored(self):
        d, f = decide(
            report(languages_required=[{"language": "Mandarin", "level": "fluent",
                                        "evidence": "Chinese language skills are mandatory for all engineers"}]),
            haystack=TEXT, profile=make_profile(),
        )

        assert not f["blocked"] and d.ignored

    def test_a_real_quote_about_something_else_is_ignored(self):
        d, f = decide(
            report(languages_required=[{"language": "Mandarin", "level": "fluent",
                                        "evidence": "We build with Laravel."}]),
            haystack=TEXT, profile=make_profile(),
        )

        assert not f["blocked"]

    def test_nice_to_have_is_a_flag_not_a_block(self):
        d, f = decide(
            report(languages_required=[{"language": "German", "level": "preferred",
                                        "evidence": "German is a plus"}]),
            haystack=TEXT + " german is a plus", profile=make_profile(),
        )

        assert not f["blocked"] and "German preferred" in d.flags

    def test_a_language_the_candidate_speaks_does_not_block(self):
        _, f = decide(
            report(languages_required=[{"language": "French", "level": "fluent", "evidence": "French is a plus"}]),
            haystack=TEXT, profile=make_profile(),
        )

        assert not f["blocked"]

    def test_dutch_sets_the_dutch_flag(self):
        _, f = decide(
            report(languages_required=[{"language": "Dutch", "level": "fluent",
                                        "evidence": "fluent dutch is required"}]),
            haystack="fluent dutch is required", profile=make_profile(),
        )

        assert f["blocked"] and f["dutch_required"] is True


class TestCitizenshipAndClearance:
    def test_a_nationality_the_candidate_lacks_blocks(self):
        d, f = decide(
            report(citizenship_required=True, citizenship_nationalities=["Russian"],
                   citizenship_evidence="Applicants must be a Russian citizen"),
            haystack=TEXT, profile=make_profile(),
        )

        assert f["blocked"] and "citizenship of Russian" in d.blockers[0]

    def test_the_candidates_own_country_is_fine(self):
        _, f = decide(
            report(citizenship_required=True, citizenship_nationalities=["Tunisian"],
                   citizenship_evidence="applicants must be a tunisian citizen"),
            haystack="applicants must be a tunisian citizen", profile=make_profile(),
        )

        assert not f["blocked"]

    def test_declared_citizenship_wins_over_residence(self):
        profile = make_profile(citizenship=["French"])
        _, f = decide(
            report(citizenship_required=True, citizenship_nationalities=["French"],
                   citizenship_evidence="must be a french citizen"),
            haystack="must be a french citizen", profile=profile,
        )

        assert not f["blocked"]

    def test_residence_is_not_citizenship(self):
        _, f = decide(
            report(citizenship_required=True, citizenship_nationalities=["US"],
                   citizenship_evidence="Applicants must be a Russian citizen"),
            haystack=TEXT, profile=make_profile(),
        )

        assert f["blocked"]

    def test_unproven_citizenship_is_ignored(self):
        _, f = decide(
            report(citizenship_required=True, citizenship_nationalities=["Russian"],
                   citizenship_evidence="We love Russian literature and culture"),
            haystack=TEXT, profile=make_profile(),
        )

        assert not f["blocked"]

    def test_security_clearance(self):
        d, f = decide(
            report(security_clearance_required=True,
                   clearance_evidence="A security clearance is required for this role"),
            haystack=TEXT, profile=make_profile(),
        )

        assert f["blocked"] and "requires a security clearance" in d.blockers


class TestWhereThePersonMustBe:
    @pytest.mark.parametrize("places, blocked", [
        (["Poland"], True),
        (["EU"], True),
        (["UK", "select EU countries"], True),
        (["Tunisia"], False),
        (["Africa"], False),
        (["EMEA"], False),
        (["worldwide"], False),
    ])
    def test_remote_restricted_to_places(self, places, blocked):
        evidence = "Remote hiring only in " + ", ".join(places)
        _, f = decide(
            report(remote="remote", remote_scope="countries", remote_places=places, location_evidence=evidence),
            haystack=evidence.lower(), profile=make_profile(),
        )

        assert f["blocked"] is blocked

    def test_the_restriction_quote_must_name_a_place(self):
        d, f = decide(
            report(remote="remote", remote_scope="countries", remote_places=["Poland"],
                   location_evidence="We build with Laravel."),
            haystack=TEXT, profile=make_profile(),
        )

        assert not f["blocked"] and d.ignored

    def test_unrestricted_remote_is_green(self):
        _, f = decide(report(remote="remote"), haystack=TEXT, profile=make_profile())

        assert f["eligibility"] == "remote_ok" and f["sponsorship_required"] is False

    def test_onsite_is_reachable_with_a_conversation(self):
        _, f = decide(report(remote="onsite"), haystack=TEXT, profile=make_profile())

        assert f["eligibility"] == "relocation" and f["sponsorship_required"] is True

    def test_nothing_stated_is_relocation_not_blocked(self):
        _, f = decide(report(), haystack=TEXT, profile=make_profile())

        assert f["eligibility"] == "relocation" and not f["blocked"]


class TestPermitAndSponsorship:
    def test_existing_right_to_work_required_for_an_onsite_role(self):
        _, f = decide(
            report(remote="onsite", requires_existing_permit=True,
                   permit_evidence="You must already have the right to work in the UK"),
            haystack="you must already have the right to work in the uk", profile=make_profile(),
        )

        assert f["blocked"] and f["requires_existing_permit"]

    def test_it_does_not_bar_a_remote_role(self):
        _, f = decide(
            report(remote="remote", requires_existing_permit=True,
                   permit_evidence="You must already have the right to work in the UK"),
            haystack="you must already have the right to work in the uk", profile=make_profile(),
        )

        assert not f["blocked"]

    def test_no_sponsorship_blocks_onsite(self):
        d, f = decide(
            report(remote="onsite", sponsorship="not_offered", permit_evidence="We do not sponsor visas"),
            haystack=TEXT, profile=make_profile(),
        )

        assert f["blocked"] and "does not sponsor visas" in d.blockers

    def test_a_permit_claim_without_a_real_quote_is_ignored(self):
        _, f = decide(
            report(remote="onsite", requires_existing_permit=True,
                   permit_evidence="Senior PHP Engineer wanted for our office."),
            haystack=TEXT, profile=make_profile(),
        )

        assert not f["blocked"] and f["requires_existing_permit"] is False

    def test_a_candidate_who_can_already_work_there_is_not_barred(self):
        profile = make_profile(location={"based_in": "Utrecht, Netherlands", "work_authorization": "EU citizen"})
        _, f = decide(
            report(remote="onsite", sponsorship="not_offered", permit_evidence="We do not sponsor visas"),
            haystack=TEXT, profile=profile,
        )

        assert not f["blocked"]


class FakeLlm:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return LlmResult(content=self.replies.pop(0), model="qwen3.5:4b", prompt_tokens=10,
                         output_tokens=20, duration_s=0.5, raw={})


DESCRIPTION = (
    "We build with Laravel and PHP. You must be fluent in Mandarin Chinese to talk to our clients.\n"
    "- Senior PHP Laravel engineer"
)


def assess(monkeypatch, *replies, **kwargs):
    fake = FakeLlm(*replies)
    monkeypatch.setattr(global_grader, "chat", fake)
    outcome = assess_listing(
        title="PHP Laravel Engineer", description=DESCRIPTION, profile=make_profile(),
        company="Acme", facts={"Location": "EU Remote", "Employment Type": "Full time"}, **kwargs,
    )
    return outcome, fake


class TestAssessListing:
    REPLY = json.dumps({
        "fit_score": 99, "verdict": "strong_match", "remote": "remote",
        "languages_required": [{"language": "Mandarin Chinese", "level": "fluent",
                                "evidence": "You must be fluent in Mandarin Chinese to talk to our clients"}],
        "summary": "A PHP role.", "red_flags": ["needs Mandarin"],
    })

    def test_a_graded_listing_carries_the_global_prompt_version_and_blocks(self, monkeypatch):
        outcome, _ = assess(monkeypatch, self.REPLY)

        result = outcome.result
        assert result.prompt_version == GLOBAL_ASSESSMENT_PROMPT_VERSION
        assert result.blocked and result.eligibility == "blocked"
        assert "requires fluent Mandarin Chinese" in result.red_flags
        assert result.profile_name == "t"

    def test_the_score_is_clamped_into_the_stack_band(self, monkeypatch):
        outcome, _ = assess(monkeypatch, self.REPLY)

        assert outcome.result.stack_match == "core"
        assert 75 <= outcome.result.fit_score <= 95

    def test_the_prompt_is_not_dutch_specific(self, monkeypatch):
        _, fake = assess(monkeypatch, self.REPLY)

        sent = " ".join(m["content"] for m in fake.calls[0]["messages"])
        assert "Languages spoken: Arabic (native); English (fluent); French (proficient)" in sent
        assert "Location: EU Remote" in sent
        assert "Dutch education" not in sent and "Dutch vacancy" not in sent

    def test_unparseable_reply_is_repaired_once(self, monkeypatch):
        outcome, fake = assess(monkeypatch, "not json at all", self.REPLY)

        assert outcome.result.repaired and len(fake.calls) == 2

    def test_unreadable_even_after_repair_raises(self, monkeypatch):
        with pytest.raises(AssessmentUnparseable):
            assess(monkeypatch, "nope", "still nope")


class TestProfileText:
    def test_has_the_eligibility_facts_without_dutch_wording(self):
        text = profile_text(make_profile(citizenship=["Tunisian"]))

        assert "Currently based in: Sousse, Tunisia" in text
        assert "Citizenship: Tunisian" in text
        assert "Dutch education" not in text


class TestGeo:
    def test_country_of(self):
        assert geo.country_of("Sousse, Tunisia") == "tunisia"
        assert geo.country_of("London, UK") == "united kingdom"

    def test_regions(self):
        assert geo.places_include(["EU"], "tunisia") is False
        assert geo.places_include(["EMEA"], "tunisia") is True
        assert geo.places_include(["Europe"], "poland") is True
        assert geo.places_include(["Remote within Poland"], "poland") is True
        assert geo.places_include([], "tunisia") is False

    def test_nationalities(self):
        assert geo.nationality_country("Russian") == "russia"
        assert geo.nationality_country("Tunisian") == "tunisia"
        assert geo.nationality_country("Narnian") == ""
