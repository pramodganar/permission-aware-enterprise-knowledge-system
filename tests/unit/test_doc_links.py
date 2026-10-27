"""Every link between the project's pages goes to a file, and a heading, that exist.

A renamed page or heading breaks the links to it without a word; GitHub
just shows the top of the page. Links to other sites aren't followed.
"""

import re
from pathlib import Path

import pytest
from docrefs import ROOT, anchors, prose, slug

PAGES = [ROOT / "README.md", ROOT / "SECURITY.md", *sorted((ROOT / "docs").rglob("*.md"))]
_LINK = re.compile(r"\]\((?P<target>[^)\s]+)\)")
# Inline code, where `[S1](...)` is an example of a link, not one.
_INLINE_CODE = re.compile(r"`[^`\n]+`")


def links() -> list[tuple[str, str]]:
    found = []
    for page in PAGES:
        for match in _LINK.finditer(_INLINE_CODE.sub("", prose(page))):
            target = match["target"]
            if not target.startswith(("http://", "https://", "mailto:")):
                found.append((page.relative_to(ROOT).as_posix(), target))
    return found


def test_there_are_links_to_check() -> None:
    assert len(links()) > 200


@pytest.mark.parametrize(("page", "target"), links())
def test_every_link_goes_somewhere(page: str, target: str) -> None:
    path, _, anchor = target.partition("#")
    linked = (ROOT / page).parent / path if path else ROOT / page

    assert linked.resolve().exists(), f"{page} links to {target}"
    if anchor and linked.suffix == ".md":
        assert anchor in anchors(linked), f"{page} links to {target}: no such heading"


def test_the_slug_is_githubs() -> None:
    assert slug("Why from the log") == "why-from-the-log"
    held = "what-it-holds-back-and-what-it-doesnt"
    assert slug("What it holds back, and what it doesn't") == held
    assert slug("Too many failed logins") == "too-many-failed-logins"
    assert slug("access-check-disagreed") == "access-check-disagreed"


def test_a_link_shown_as_code_is_not_followed(tmp_path: Path) -> None:
    # Windows reads "docs/..." as "docs", so only Linux showed this one.
    page = tmp_path / "page.md"
    page.write_text("A link `[S1](...)` stays a citation.\n", encoding="utf-8")

    assert not _LINK.findall(_INLINE_CODE.sub("", prose(page)))


def test_a_comment_in_a_code_block_is_not_a_heading(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("# Title\n\n```\n# not a heading\n```\n", encoding="utf-8")

    assert anchors(page) == {"title"}
