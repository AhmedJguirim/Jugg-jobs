"""A tiny HTML tree built on the standard library.

Listing parsers need to ask "what is in the element with this class?" and "give
me that element's inner HTML", which `html.parser` alone does not answer. This
builds just enough of a tree for that (no CSS selectors, no repair beyond
closing what the page left open), so parsers need no third-party dependency.
"""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from typing import Iterator, Optional

_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "source", "track", "wbr",
}
_BLOCKS = {
    "address", "article", "br", "div", "dl", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "ol", "p", "section", "table", "td", "th", "tr", "ul",
}
# Content of these is never part of the readable page.
_SKIP_TEXT = {"script", "style", "svg", "noscript", "template"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Optional[dict] = None, parent: "Node | None" = None) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list["Node | str"] = []
        self.parent = parent

    # -- searching --

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def iter(self) -> Iterator["Node"]:
        """Every descendant element, document order."""
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(self, tag: Optional[str] = None, cls: Optional[str] = None) -> list["Node"]:
        return [
            node
            for node in self.iter()
            if (tag is None or node.tag == tag) and (cls is None or cls in node.classes)
        ]

    def find(self, tag: Optional[str] = None, cls: Optional[str] = None) -> Optional["Node"]:
        return next(iter(self.find_all(tag, cls)), None)

    def find_class_prefix(self, prefix: str, tag: Optional[str] = None) -> list["Node"]:
        """Elements with a class starting `prefix` (for hashed CSS-module names)."""
        return [
            node
            for node in self.iter()
            if (tag is None or node.tag == tag) and any(c.startswith(prefix) for c in node.classes)
        ]

    # -- reading --

    def text(self) -> str:
        """Readable text, whitespace collapsed to single spaces."""
        parts: list[str] = []

        def walk(node: "Node") -> None:
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                elif child.tag not in _SKIP_TEXT:
                    # Blocks end a word: "<p>a</p><p>b</p>" reads "a b", not "ab".
                    block = child.tag in _BLOCKS
                    if block:
                        parts.append(" ")
                    walk(child)
                    if block:
                        parts.append(" ")

        walk(self)
        return " ".join("".join(parts).split())

    def inner_html(self, drop_attrs: frozenset = frozenset(), keep_attrs: Optional[frozenset] = None) -> str:
        return "".join(_render(child, drop_attrs, keep_attrs) for child in self.children)


def _render(node: "Node | str", drop: frozenset, keep: Optional[frozenset]) -> str:
    if isinstance(node, str):
        return escape(node, quote=False)
    if node.tag in _SKIP_TEXT:
        return ""
    attrs = "".join(
        f' {name}="{escape(value or "", quote=True)}"'
        for name, value in node.attrs.items()
        if name not in drop and (keep is None or name in keep)
    )
    if node.tag in _VOID:
        return f"<{node.tag}{attrs}>"
    inner = "".join(_render(child, drop, keep) for child in node.children)
    return f"<{node.tag}{attrs}>{inner}</{node.tag}>"


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self._stack: list[Node] = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {name: value for name, value in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {name: value for name, value in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        # Close up to the nearest matching open element; an unmatched close tag
        # is ignored rather than allowed to unbalance the tree.
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def parse_html(html_text: str) -> Node:
    builder = _Builder()
    builder.feed(html_text)
    builder.close()
    return builder.root
