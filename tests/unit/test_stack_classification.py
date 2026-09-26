"""Unit tests for deterministic stack-tier classification.

This decides which score band a vacancy can reach, so it is the highest-leverage
piece of the grading strategy and the one most worth pinning down. No model is
involved, so these are fast and exact.
"""

from __future__ import annotations

import pytest

from app.llm.profile import CandidateProfile
from app.llm.service import STACK_BANDS, classify_stack


@pytest.fixture
def profile() -> CandidateProfile:
    return CandidateProfile(
        name="test",
        skills={
            "core": ["PHP", "Laravel", "Symfony", "MySQL"],
            "proficient": ["ReactJS", "NodeJS", "PostgreSQL"],
            "familiar": ["Python", "Java", "Ruby on Rails"],
        },
    )


class TestTierSelection:
    def test_core_stack_in_the_title(self, profile):
        tier, hits = classify_stack("Laravel Developer", "Build things", profile)

        assert tier == "core"
        assert "Laravel" in hits

    def test_proficient_stack(self, profile):
        tier, _ = classify_stack("React Developer", "ReactJS and NodeJS", profile)

        assert tier == "proficient"

    def test_familiar_stack(self, profile):
        """Python is in the familiar tier, so it is familiar - never unrelated."""
        tier, hits = classify_stack("Python Developer", "Write Python daily", profile)

        assert tier == "familiar"
        assert hits == ["Python"]

    def test_stack_in_no_tier_is_unrelated(self, profile):
        tier, hits = classify_stack("Go Engineer", "Go, gRPC and Kafka", profile)

        assert tier == "unrelated"
        assert hits == []

    def test_non_software_role_is_non_technical(self, profile):
        tier, _ = classify_stack("Verpleegkundige", "Zorg voor patienten", profile)

        assert tier == "non_technical"

    def test_software_role_without_known_tech_is_unrelated_not_non_technical(self, profile):
        """It is still a developer job, just not on a stack in the profile."""
        tier, _ = classify_stack("Software Engineer", "Build cloud systems", profile)

        assert tier == "unrelated"

    def test_dutch_developer_wording_counts_as_technical(self, profile):
        tier, _ = classify_stack("Softwareontwikkelaar", "Bouw mooie systemen", profile)

        assert tier == "unrelated"


class TestWeighting:
    def test_title_beats_a_passing_mention_in_the_body(self, profile):
        """A Python role listing PHP as a nice-to-have is a Python role.

        First-match classification got this wrong and called it core PHP work.
        """
        tier, _ = classify_stack(
            "Python Developer",
            "You write Python all day. Some legacy PHP exists in one service.",
            profile,
        )

        assert tier == "familiar"

    def test_core_in_the_title_wins_over_familiar_in_the_body(self, profile):
        tier, _ = classify_stack(
            "Laravel Developer",
            "We also have Python scripts and some Java tooling around it.",
            profile,
        )

        assert tier == "core"

    def test_repeated_body_mentions_outweigh_a_single_one(self, profile):
        tier, _ = classify_stack(
            "Developer",
            "Laravel work: Laravel upgrades, Laravel packages, plus one Python script.",
            profile,
        )

        assert tier == "core"

    def test_ties_break_towards_the_stronger_tier(self, profile):
        tier, _ = classify_stack("Developer", "We use PHP and Python equally.", profile)

        assert tier == "core"

    def test_all_matching_skills_are_reported(self, profile):
        _, hits = classify_stack("Backend Developer", "PHP, Symfony and MySQL", profile)

        assert set(hits) == {"PHP", "Symfony", "MySQL"}


class TestWordBoundaries:
    def test_java_does_not_match_inside_javascript(self, profile):
        """Without \\b anchors this would classify a JS role as familiar Java."""
        tier, hits = classify_stack("Frontend Developer", "Modern JavaScript tooling", profile)

        assert "Java" not in hits

    def test_matching_is_case_insensitive(self, profile):
        tier, _ = classify_stack("laravel developer", "php and mysql", profile)

        assert tier == "core"

    def test_multi_word_skills_match(self, profile):
        _, hits = classify_stack("Developer", "We use Ruby on Rails here", profile)

        assert "Ruby on Rails" in hits


class TestEdgeCases:
    def test_empty_inputs_are_non_technical(self, profile):
        assert classify_stack("", "", profile)[0] == "non_technical"

    def test_none_inputs_do_not_crash(self, profile):
        assert classify_stack(None, None, profile)[0] == "non_technical"

    def test_profile_without_tiers_falls_back_to_flat_skills(self):
        """Older flat profiles must keep working."""
        flat = CandidateProfile(name="old", strong_skills=["PHP"], familiar_skills=["Python"])

        assert classify_stack("PHP Developer", "", flat)[0] == "core"
        assert classify_stack("Python Developer", "", flat)[0] == "familiar"


class TestBands:
    def test_every_tier_has_a_band(self):
        for tier in ("core", "proficient", "familiar", "unrelated", "non_technical"):
            assert tier in STACK_BANDS

    def test_bands_are_ordered_and_do_not_overlap_downward(self):
        order = ["non_technical", "unrelated", "familiar", "proficient", "core"]
        lows = [STACK_BANDS[tier][0] for tier in order]

        assert lows == sorted(lows)

    def test_bands_are_valid_ranges(self):
        for low, high in STACK_BANDS.values():
            assert 0 <= low < high <= 100
