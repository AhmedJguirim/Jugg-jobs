"""Unit tests for candidate profile loading and prompt rendering."""

from __future__ import annotations

import json

import pytest

from app.llm import profile as profile_module
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
    def test_includes_target_roles(self):
        text = load_profile("default").as_prompt_text()

        assert "Target roles:" in text

    def test_states_languages_not_spoken(self):
        """The Dutch gap is the single most decision-relevant fact in the profile."""
        text = load_profile("default").as_prompt_text()

        assert "Languages NOT spoken:" in text
        assert "Dutch" in text

    def test_states_dealbreakers(self):
        assert "Dealbreakers:" in load_profile("default").as_prompt_text()

    def test_renders_an_empty_profile_without_crashing(self):
        text = CandidateProfile(name="empty").as_prompt_text()

        assert "unspecified" in text

    def test_is_plain_text_not_json(self):
        """A 4B model echoes JSON back; labelled lines work better."""
        text = load_profile("default").as_prompt_text()

        assert not text.strip().startswith("{")
