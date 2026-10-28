"""Reading citations from an answer and resolving them against its context only."""

import dataclasses
from uuid import UUID

import pytest

from app.rag.citations import Citations, resolve_citations
from app.rag.context import Context, Source


def source(number: int, title: str = "Travel and Expense Policy") -> Source:
    return Source(
        label=f"S{number}",
        document_id=number * 10,
        document_public_id=UUID(int=number * 10),
        document_title=title,
        document_version=2,
        chunk_ids=(number * 100,),
        text=f"Passage {number}.",
        score=0.5,
    )


CONTEXT = Context(
    user_id=7,
    sources=(source(1), source(2, "Leave Policy"), source(3, "Code of Conduct")),
    left_out=0,
)


def labels(cited: Citations) -> list[str]:
    return [s.label for s in cited.sources]


# --- resolving --------------------------------------------------------------------------


def test_a_citation_resolves_to_its_source_in_the_context() -> None:
    cited = resolve_citations("The limit is 160 EUR [S1].", CONTEXT)

    assert cited.sources == (CONTEXT.sources[0],)
    assert cited.sources[0].document_title == "Travel and Expense Policy"
    assert cited.text == "The limit is 160 EUR [S1]."
    assert cited.unknown == 0
    assert not cited.uncited


def test_sources_are_listed_in_the_order_they_are_first_cited() -> None:
    cited = resolve_citations("Leave [S2]. Hotels [S1]. More leave [S2]. Gifts [S3].", CONTEXT)

    assert labels(cited) == ["S2", "S1", "S3"]


def test_a_source_cited_twice_is_listed_once() -> None:
    cited = resolve_citations("One [S1]. Two [S1].", CONTEXT)

    assert labels(cited) == ["S1"]


def test_sources_never_cited_are_not_listed() -> None:
    cited = resolve_citations("Only this [S2].", CONTEXT)

    assert labels(cited) == ["S2"]


# --- made-up labels ---------------------------------------------------------------------


def test_a_label_not_in_the_context_is_removed_and_counted() -> None:
    cited = resolve_citations("The limit is 160 EUR [S9].", CONTEXT)

    assert cited.sources == ()
    assert cited.unknown == 1
    assert cited.text == "The limit is 160 EUR."
    assert cited.uncited


def test_a_made_up_label_next_to_a_real_one_leaves_the_real_one() -> None:
    cited = resolve_citations("It is 160 EUR [S1][S7] and 220 EUR [S1, S8].", CONTEXT)

    assert labels(cited) == ["S1"]
    assert cited.unknown == 2
    assert cited.text == "It is 160 EUR [S1] and 220 EUR [S1]."


def test_label_zero_is_not_a_source() -> None:
    cited = resolve_citations("Text [S0].", CONTEXT)

    assert cited.sources == ()
    assert cited.unknown == 1


@pytest.mark.parametrize(
    "text",
    [
        "See [Salary Bands 2026].",
        "See [HR Case File 2026-014].",
        "See [document 12].",
        "See [chunk 4411].",
    ],
)
def test_naming_a_document_is_not_a_citation(text: str) -> None:
    # Only labels are looked up. A title or an ID in brackets resolves to
    # nothing and is left as the model wrote it.
    cited = resolve_citations(text, CONTEXT)

    assert cited.sources == ()
    assert cited.unknown == 0
    assert cited.text == text


def test_a_citation_can_only_resolve_to_a_source_of_its_own_context() -> None:
    other = Context(user_id=8, sources=(source(1, "Salary Bands 2026"),), left_out=0)

    mine = resolve_citations("Text [S1].", CONTEXT)
    theirs = resolve_citations("Text [S1].", other)

    assert mine.sources[0].document_title == "Travel and Expense Policy"
    assert theirs.sources[0].document_title == "Salary Bands 2026"


def test_an_empty_context_resolves_nothing() -> None:
    cited = resolve_citations("Text [S1].", Context(user_id=7, sources=(), left_out=0))

    assert cited.sources == ()
    assert cited.unknown == 1


# --- how models write them --------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "expected", "cited_labels"),
    [
        # All three seen from gpt-oss on Groq.
        ("Text [S1].", "Text [S1].", ["S1"]),
        ("Text [ S1 ].", "Text [S1].", ["S1"]),
        ("Text【S1】.", "Text[S1].", ["S1"]),
        ("Text [S1][S3].", "Text [S1][S3].", ["S1", "S3"]),
        ("Text [S1, S3].", "Text [S1][S3].", ["S1", "S3"]),
        ("Text [S1; S3].", "Text [S1][S3].", ["S1", "S3"]),
        ("Text [S1 S3].", "Text [S1][S3].", ["S1", "S3"]),
        ("Text [s2].", "Text [S2].", ["S2"]),
        ("Text [S01].", "Text [S1].", ["S1"]),
        ("Text 【 S2 , S3 】.", "Text [S2][S3].", ["S2", "S3"]),
        ("Text [S1, S1].", "Text [S1].", ["S1"]),
    ],
)
def test_every_way_of_writing_a_citation_is_read_and_written_one_way(
    written: str, expected: str, cited_labels: list[str]
) -> None:
    cited = resolve_citations(written, CONTEXT)

    assert cited.text == expected
    assert labels(cited) == cited_labels


def test_a_citation_on_its_own_line_is_read() -> None:
    cited = resolve_citations("- Up to 5,000 EUR: Finance Analyst\n\n[ S1 ]", CONTEXT)

    assert cited.text == "- Up to 5,000 EUR: Finance Analyst\n\n[S1]"
    assert labels(cited) == ["S1"]


def test_text_that_is_not_a_citation_is_left_alone() -> None:
    text = "Use deployctl rollback <service> --to previous. Level S1 is not a label, nor is S2."

    cited = resolve_citations(text, CONTEXT)

    assert cited.text == text
    assert cited.sources == ()
    assert cited.unknown == 0


def test_removing_a_made_up_label_changes_nothing_else() -> None:
    text = "Amounts: 5 , 000 and [x] stay as they are [S9]. Next line  [S1]."

    cited = resolve_citations(text, CONTEXT)

    assert cited.text == "Amounts: 5 , 000 and [x] stay as they are. Next line  [S1]."


def test_an_answer_with_no_citations_is_uncited() -> None:
    cited = resolve_citations("The outage was caused by connection pool exhaustion.", CONTEXT)

    assert cited.uncited
    assert cited.text == "The outage was caused by connection pool exhaustion."


def test_the_result_can_not_be_changed() -> None:
    cited = resolve_citations("Text [S1].", CONTEXT)

    with pytest.raises(dataclasses.FrozenInstanceError):
        cited.sources = ()  # type: ignore[misc]
