"""Unit tests for candidate profile loading and prompt rendering."""

from __future__ import annotations

import json

import pytest

from app.llm import profile as profile_module
from app.llm.global_grader import profile_text
from app.llm.profile import CandidateProfile, ProfileNotFound, load_profile


class TestLoading:
    def test_loads_the_default_profile(self):
        loaded = load_profile("default")

        assert loaded.name == "default"
        assert loaded.target_roles

    def test_unknown_profile_raises(self):
        with pytest.raises(ProfileNotFound):
            load_profile("no-such-profile")

    def test_loads_from_a_custom_directory(self, tmp_path, monkeypatch):
        (tmp_path / "custom.json").write_text(
            json.dumps({"headline": "Tester", "target_roles": ["QA"]}), encoding="utf-8"
        )
        monkeypatch.setattr(profile_module, "PROFILE_DIR", str(tmp_path))
        load_profile.cache_clear()

        loaded = load_profile("custom")

        assert loaded.headline == "Tester"
        assert loaded.name == "custom"
        load_profile.cache_clear()

    def test_unknown_keys_are_ignored_but_kept_in_raw(self):
        loaded = CandidateProfile.from_dict(
            {"name": "x", "headline": "H", "_comment": "note", "future_field": 1}
        )

        assert loaded.headline == "H"
        assert loaded.raw["_comment"] == "note"


class TestPromptRendering:
    """The profile as the grader hands it to the model."""

    def test_includes_target_roles(self):
        assert "Target roles:" in profile_text(load_profile("default"))

    def test_states_languages_not_spoken(self):
        """A language the candidate lacks is what decides "blocked" most often."""
        text = profile_text(load_profile("default"))

        assert "Languages NOT spoken:" in text
        assert "Dutch" in text

    def test_states_dealbreakers(self):
        assert "Dealbreakers:" in profile_text(load_profile("default"))

    def test_renders_an_empty_profile_without_crashing(self):
        assert "unspecified" in profile_text(CandidateProfile(name="empty"))

    def test_states_education(self):
        assert "Highest education:" in profile_text(load_profile("default"))

    def test_a_profile_without_education_still_renders(self):
        assert "Headline:" in profile_text(CandidateProfile(name="none"))

    def test_dealbreakers_name_licences_not_education_levels(self):
        """"Professional qualification" was being read as any stated level."""
        dealbreakers = " ".join(load_profile("default").dealbreakers).lower()

        assert "licensed profession" in dealbreakers

    def test_is_plain_text_not_json(self):
        """A 4B model echoes JSON back; labelled lines work better."""
        assert not profile_text(load_profile("default")).strip().startswith("{")
