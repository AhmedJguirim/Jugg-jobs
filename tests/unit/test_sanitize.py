"""Unit tests for HTML sanitising, language detection and chunking.

These guard the input to the model. Every case marked "real corpus" reflects
something actually observed in the ingested vacancies.
"""

from __future__ import annotations

import pytest

from app.text.sanitize import (
    clean_lines,
    content_hash,
    detect_language,
    sanitize_description,
    strip_html,
    unescape_entities,
)


class TestUnescapeEntities:
    def test_decodes_a_single_pass(self):
        assert unescape_entities("caf&eacute;") == "café"

    def test_decodes_double_encoded_text(self):
        """Real corpus: 61 of 68 descriptions contain &amp;eacute;, not &eacute;."""
        assert unescape_entities("caf&amp;eacute;") == "café"

    def test_decodes_triple_encoded_text(self):
        assert unescape_entities("caf&amp;amp;eacute;") == "café"

    def test_leaves_plain_text_alone(self):
        assert unescape_entities("plain text") == "plain text"

    def test_pass_cap_is_respected(self):
        assert unescape_entities("caf&amp;amp;eacute;", max_passes=1) == "caf&amp;eacute;"


class TestStripHtml:
    def test_removes_tags(self):
        assert "<p>" not in strip_html("<p>Hello</p>")

    def test_paragraphs_become_separate_lines(self):
        text = strip_html("<p>One</p><p>Two</p>")

        assert "One" in text and "Two" in text
        assert text.index("One") < text.index("Two")
        assert "\n" in text

    def test_list_items_get_a_bullet_marker(self):
        assert "- First" in strip_html("<ul><li>First</li></ul>")

    def test_inline_tags_do_not_split_words(self):
        assert "Python" in strip_html("<p>Py<b>thon</b></p>").replace("\n", "")

    def test_script_content_is_dropped(self):
        assert "alert" not in strip_html("<p>Hi</p><script>alert(1)</script>")

    def test_entities_are_decoded(self):
        assert "&" in strip_html("<p>R&amp;D</p>")

    def test_non_breaking_spaces_are_normalised(self):
        assert "\xa0" not in strip_html("<p>a\xa0b</p>")


class TestCleanLines:
    def test_collapses_internal_whitespace(self):
        assert clean_lines("a     b") == ["a b"]

    def test_drops_empty_and_marker_only_lines(self):
        assert clean_lines("real line\n\n-\n   ") == ["real line"]

    def test_drops_repeated_short_lines(self):
        """Real corpus: title/city/hours repeat from navigation blocks."""
        lines = clean_lines("Rotterdam\nSome real content here\nRotterdam")

        assert lines.count("Rotterdam") == 1

    def test_keeps_repeated_long_lines(self):
        """A long paragraph appearing twice is far more likely to be content."""
        paragraph = (
            "This is a genuinely long sentence of real prose that happens to appear "
            "twice in the same document and must survive de-duplication."
        )
        lines = clean_lines(f"{paragraph}\n{paragraph}")

        assert len(lines) == 2

    def test_deduplication_is_case_insensitive(self):
        assert len(clean_lines("Rotterdam\nrotterdam")) == 1


class TestSanitizeDescription:
    def test_produces_clean_text(self):
        result = sanitize_description("<p>Wij zoeken een ontwikkelaar.</p>")

        assert result.text == "Wij zoeken een ontwikkelaar."

    def test_reports_character_reduction(self):
        result = sanitize_description("<p><b>Hello</b></p>")

        assert result.original_chars > result.clean_chars
        assert 0 < result.reduction < 1

    def test_handles_none(self):
        result = sanitize_description(None)

        assert result.text == ""
        assert result.language == "unknown"

    def test_handles_empty_string(self):
        assert sanitize_description("   ").text == ""

    def test_hash_is_stable_for_identical_input(self):
        first = sanitize_description("<p>Wij zoeken een ontwikkelaar.</p>")
        second = sanitize_description("<p>Wij zoeken een ontwikkelaar.</p>")

        assert first.content_hash == second.content_hash

    def test_hash_changes_when_text_changes(self):
        first = sanitize_description("<p>Wij zoeken een ontwikkelaar.</p>")
        second = sanitize_description("<p>Wij zoeken een tester.</p>")

        assert first.content_hash != second.content_hash

    def test_markup_only_change_does_not_change_the_hash(self):
        """The hash tracks meaning, so re-tagged HTML must not re-run the GPU."""
        first = sanitize_description("<p>Wij zoeken een ontwikkelaar.</p>")
        second = sanitize_description("<div>Wij zoeken een ontwikkelaar.</div>")

        assert first.content_hash == second.content_hash

    def test_truncates_on_a_line_boundary(self):
        text = "<p>" + "</p><p>".join(f"line number {i} here" for i in range(40)) + "</p>"
        result = sanitize_description(text, max_chars=100)

        assert result.truncated is True
        assert len(result.text) <= 100

    def test_no_truncation_flag_when_it_fits(self):
        assert sanitize_description("<p>short</p>", max_chars=1000).truncated is False


class TestDetectLanguage:
    def test_detects_dutch(self):
        assert detect_language("Wij zoeken een enthousiaste collega voor ons team") == "nl"

    def test_detects_english(self):
        assert detect_language("We are looking for an enthusiastic colleague for our team") == "en"

    def test_empty_text_is_unknown(self):
        assert detect_language("") == "unknown"

    def test_text_without_stopwords_is_unknown(self):
        assert detect_language("Python Kubernetes PostgreSQL") == "unknown"

    def test_dutch_vacancy_prose_is_dutch(self):
        text = (
            "Ben jij een Python Developer die techniek en praktijk weet te verbinden? "
            "Bij ons ontwikkel je slimme digitale oplossingen voor onze klanten."
        )
        assert detect_language(text) == "nl"

    def test_english_text_is_recognised(self):
        """Postings are mostly English; the language is recorded with the text."""
        result = sanitize_description("<p>We are hiring a backend engineer for our team.</p>")

        assert result.language == "en"
        assert result.is_dutch is False


def test_content_hash_is_hex_sha256():
    value = content_hash("anything")

    assert len(value) == 64
    assert all(character in "0123456789abcdef" for character in value)
