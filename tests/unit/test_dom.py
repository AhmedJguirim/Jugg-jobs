"""The small HTML tree the listing parsers use."""

from __future__ import annotations

from app.text.dom import parse_html


def test_finds_by_tag_and_class():
    root = parse_html('<div class="a b"><h1 class="t">Hi</h1><p>x</p></div>')

    assert root.find("h1", "t").text() == "Hi"
    assert root.find(cls="b").tag == "div"
    assert root.find("h2") is None


def test_text_collapses_whitespace_and_skips_scripts():
    root = parse_html("<p> a \n  <b>b</b> <script>var x = 1;</script> c </p>")

    assert root.find("p").text() == "a b c"


def test_inner_html_round_trips_and_filters_attributes():
    root = parse_html('<div id="d"><p style="x" class="y">a &amp; <a href="/u" target="_blank">b</a><br></p></div>')

    html = root.find(cls=None, tag="div").inner_html(keep_attrs=frozenset({"href"}))

    assert html == '<p>a &amp; <a href="/u">b</a><br></p>'


def test_tolerates_unclosed_and_stray_tags():
    # `</span>` closes nothing and is ignored; `</div>` closes the div even
    # though two <p> are still open, so <i> ends up outside it.
    root = parse_html("<div><p>one<p>two</span></div><i>after</i>")

    assert root.find("i").parent is root
    assert root.find("div").text() == "one two"


def test_class_prefix_matches_hashed_module_names():
    root = parse_html('<div class="_notFoundContent_b9ljl_6">x</div>')

    assert root.find_class_prefix("_notFoundContent")


class TestSafeSerialisation:
    ALLOWED = frozenset({"p", "a", "strong"})

    def render(self, html):
        return parse_html(f"<div>{html}</div>").find("div").inner_html(
            keep_attrs=frozenset({"href", "src"}), allowed_tags=self.ALLOWED
        )

    def test_unlisted_tags_are_unwrapped_not_kept(self):
        assert self.render("<p>a<iframe src='https://x'>b</iframe><form>c</form></p>") == "<p>abc</p>"

    def test_script_and_style_content_is_dropped(self):
        assert self.render("<p>a</p><script>alert(1)</script><style>p{}</style>") == "<p>a</p>"

    def test_javascript_links_lose_their_href(self):
        assert self.render('<a href="javascript:alert(1)">x</a>') == "<a>x</a>"
        assert self.render('<a href=" JavaScript:alert(1)">x</a>') == "<a>x</a>"

    def test_web_and_mail_links_survive(self):
        assert self.render('<a href="https://a.test/?x=1&amp;y=2">x</a>') == '<a href="https://a.test/?x=1&amp;y=2">x</a>'
        assert self.render('<a href="mailto:a@b.test">x</a>') == '<a href="mailto:a@b.test">x</a>'

    def test_event_handlers_are_never_kept(self):
        assert self.render('<p onclick="x()">a</p>') == "<p>a</p>"
