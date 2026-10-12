"""The chunker, checked against the rules in docs/chunking.md."""

import random
from itertools import pairwise
from pathlib import Path

import pytest

from app.ingestion.chunking import TextChunk, chunk_text
from app.ingestion.cleaning import clean_text
from app.ingestion.extractors import extract_text

DOCUMENTS = Path(__file__).resolve().parents[2] / "data" / "acmecorp" / "documents"
COMPANY_FILES = sorted(path for path in DOCUMENTS.rglob("*") if path.is_file())

POLICY = """Leave Policy
Owner: Human Resources

1. Annual leave

Full-time employees receive 25 days of paid annual leave per calendar year.
Up to 5 unused days can be carried over into the next year.

2. Sick leave

Employees receive up to 10 days of paid sick leave per year.
An absence of more than 3 consecutive working days needs a note from a doctor."""

SIZES = [(100, 0), (100, 30), (100, 90), (250, 40), (800, 120), (800, 0), (2000, 400)]


def stored_text(path: Path) -> str:
    return clean_text(extract_text(path.name, path.read_bytes()))


def random_text(seed: int) -> str:
    """Text with lines of every length, blank lines, and words longer than a chunk."""
    rng = random.Random(seed)  # noqa: S311
    lines = []
    for _ in range(rng.randint(1, 60)):
        kind = rng.random()
        if kind < 0.15:
            lines.append("")
        elif kind < 0.25:
            lines.append("x" * rng.randint(1, 700))
        else:
            words = ["word" * rng.randint(1, 4) for _ in range(rng.randint(1, 90))]
            lines.append(" ".join(words) + rng.choice([".", "", "?", ":"]))
    return clean_text("\n".join(lines))


COMPANY_TEXTS = [stored_text(path) for path in COMPANY_FILES]
RANDOM_TEXTS = [random_text(seed) for seed in range(40)]
ALL_TEXTS = [POLICY, *COMPANY_TEXTS, *RANDOM_TEXTS]


def every_case() -> list[tuple[str, int, int]]:
    return [(text, size, overlap) for text in ALL_TEXTS for size, overlap in SIZES]


def test_there_are_company_documents_to_check() -> None:
    assert len(COMPANY_FILES) == 17


# --- the rules ---------------------------------------------------------------


def test_a_chunk_is_an_exact_slice_of_the_text() -> None:
    for text, size, overlap in every_case():
        for chunk in chunk_text(text, size, overlap):
            assert chunk.text == text[chunk.start : chunk.end]


def test_no_chunk_is_over_the_limit() -> None:
    for text, size, overlap in every_case():
        assert all(len(chunk.text) <= size for chunk in chunk_text(text, size, overlap))


def test_nothing_is_lost() -> None:
    for text, size, overlap in every_case():
        covered = [False] * len(text)
        for chunk in chunk_text(text, size, overlap):
            covered[chunk.start : chunk.end] = [True] * (chunk.end - chunk.start)
        missed = [text[index] for index, seen in enumerate(covered) if not seen]
        assert all(character.isspace() for character in missed)


def test_chunks_have_no_surrounding_whitespace_and_are_never_empty() -> None:
    for text, size, overlap in every_case():
        for chunk in chunk_text(text, size, overlap):
            assert chunk.text
            assert chunk.text == chunk.text.strip()


def test_every_chunk_starts_and_ends_later_than_the_one_before() -> None:
    for text, size, overlap in every_case():
        chunks = chunk_text(text, size, overlap)
        for earlier, later in pairwise(chunks):
            assert later.start > earlier.start
            assert later.end > earlier.end


def test_positions_count_from_zero() -> None:
    for text, size, overlap in every_case():
        chunks = chunk_text(text, size, overlap)
        assert [chunk.position for chunk in chunks] == list(range(len(chunks)))


def test_the_overlap_is_never_more_than_allowed() -> None:
    for text, size, overlap in every_case():
        chunks = chunk_text(text, size, overlap)
        for earlier, later in pairwise(chunks):
            assert earlier.end - later.start <= overlap


def test_without_overlap_no_text_is_in_two_chunks() -> None:
    for text in ALL_TEXTS:
        chunks = chunk_text(text, 300, 0)
        for earlier, later in pairwise(chunks):
            assert later.start >= earlier.end


def test_the_same_input_gives_the_same_chunks() -> None:
    for text, size, overlap in every_case():
        assert chunk_text(text, size, overlap) == chunk_text(text, size, overlap)


def test_a_chunk_holds_text_from_one_document_only() -> None:
    # Chunking is done one document at a time, so nothing from the case file
    # can be in a chunk of the leave policy, whatever the overlap.
    case_file = stored_text(DOCUMENTS / "hr" / "case-file-2026-014.txt")
    leave_policy = stored_text(DOCUMENTS / "hr" / "leave-policy.pdf")

    for chunk in chunk_text(leave_policy, 800, 120):
        assert chunk.text in leave_policy
        assert "E-2291" not in chunk.text
    for chunk in chunk_text(case_file, 800, 120):
        assert chunk.text in case_file


# --- where chunks end ----------------------------------------------------------


def test_a_short_text_is_one_chunk() -> None:
    assert chunk_text(POLICY, 800, 120) == [
        TextChunk(position=0, start=0, end=len(POLICY), text=POLICY)
    ]


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n", " \n \n "])
def test_text_with_nothing_in_it_gives_no_chunks(text: str) -> None:
    assert chunk_text(text, 800, 120) == []


def test_a_chunk_ends_at_a_blank_line_when_one_is_near() -> None:
    first, second = chunk_text(POLICY, 240, 0)

    assert first.text.endswith("carried over into the next year.")
    assert second.text.startswith("2. Sick leave")


def test_a_heading_is_not_left_at_the_end_of_a_chunk() -> None:
    text = "\n".join(
        [
            "The first rule applies to every employee in the company.",
            "The second rule applies to contractors and directors.",
            "3. Gifts",
            "Gifts above the limit must be declined or handed to Legal.",
        ]
    )

    first, second = chunk_text(text, 125, 0)

    assert first.text.endswith("contractors and directors.")
    assert second.text.startswith("3. Gifts")


def test_a_chunk_ends_on_a_finished_sentence_in_wrapped_text() -> None:
    # Lines cut at the page width, the way PDF text arrives.
    text = "\n".join(
        [
            "Requests are made in the HR portal and approved by",
            "the line manager within three working days.",
            "A manager may refuse a request",
            "when too many people are already away.",
        ]
    )

    first, second = chunk_text(text, 130, 0)

    assert first.text.endswith("within three working days.")
    assert second.text.startswith("A manager may refuse")


def test_looking_back_gives_up_at_most_a_quarter_of_the_chunk() -> None:
    text = "\n".join(["Done.", *["no stop here"] * 20])

    first = chunk_text(text, 100, 0)[0]

    # The only sentence end is at the very start, which is too far back to use.
    assert len(first.text) > 75


def test_a_line_ending_in_a_colon_is_not_treated_as_finished() -> None:
    text = "\n".join(
        [
            "All of the conditions for opening a plan have been met by the manager and by HR.",
            "The concerns were:",
            "late reports on nine occasions between April and June",
        ]
    )

    first, _ = chunk_text(text, 110, 0)

    assert first.text.endswith("and by HR.")


# --- overlap -------------------------------------------------------------------


def test_the_next_chunk_starts_with_the_last_line_of_the_one_before() -> None:
    lines = [f"Rule {number} applies to all employees." for number in range(1, 9)]

    first, second, *_ = chunk_text("\n".join(lines), 120, 40)

    assert first.text.split("\n")[-1] == second.text.split("\n")[0]


def test_the_overlap_is_made_of_whole_lines() -> None:
    lines = [f"Rule {number} applies to all employees." for number in range(1, 9)]
    text = "\n".join(lines)

    for chunk in chunk_text(text, 120, 40):
        assert all(line in lines for line in chunk.text.split("\n"))


def test_overlap_never_reaches_back_to_the_start_of_the_chunk() -> None:
    # A short chunk that fits inside the overlap. Starting the next chunk at the
    # same place would repeat it for ever.
    text = "\n".join(["A short rule.", "b", "y" * 99])

    chunks = chunk_text(text, 100, 90)

    assert [chunk.text for chunk in chunks] == ["A short rule.", "b", "y" * 99]


def test_overlap_is_dropped_when_it_would_only_repeat_text() -> None:
    # After the first chunk, the overlap plus the long line would not fit.
    text = "\n".join(["short line one.", "short line two.", "y" * 95])

    chunks = chunk_text(text, 100, 40)

    assert [chunk.text for chunk in chunks] == ["short line one.\nshort line two.", "y" * 95]


# --- lines longer than a chunk -----------------------------------------------------


def test_a_long_line_is_cut_after_a_sentence() -> None:
    text = "The limit is five thousand euros. Larger payments need a manager. " * 4

    chunks = chunk_text(text.strip(), 80, 0)

    assert len(chunks) > 1
    assert all(chunk.text.endswith(".") for chunk in chunks)


def test_a_long_sentence_is_cut_between_words() -> None:
    text = " ".join(["approval"] * 40)

    chunks = chunk_text(text, 50, 0)

    assert all(set(chunk.text.split(" ")) == {"approval"} for chunk in chunks)
    assert " ".join(chunk.text for chunk in chunks) == text


def test_a_word_longer_than_a_chunk_is_cut_anyway() -> None:
    chunks = chunk_text("z" * 250, 100, 0)

    assert [len(chunk.text) for chunk in chunks] == [100, 100, 50]


def test_a_decimal_number_is_not_treated_as_a_sentence_end() -> None:
    text = "Business journeys are reimbursed at 0.42 EUR per kilometre " * 3

    for chunk in chunk_text(text.strip(), 70, 0):
        assert not chunk.text.endswith("0.")
        assert not chunk.text.startswith("42 EUR")


# --- settings ------------------------------------------------------------------


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (-5, 0), (100, 100), (100, 150), (100, -1)])
def test_sizes_that_make_no_sense_are_refused(size: int, overlap: int) -> None:
    with pytest.raises(ValueError, match=r"chunk size|overlap"):
        chunk_text(POLICY, size, overlap)


# --- the company documents at the default sizes ------------------------------------------


def test_the_company_documents_at_the_default_sizes() -> None:
    counts = [len(chunk_text(text, 800, 120)) for text in COMPANY_TEXTS]

    assert min(counts) >= 2
    assert max(counts) <= 5


def test_no_company_chunk_ends_on_a_section_heading() -> None:
    for text in COMPANY_TEXTS:
        for chunk in chunk_text(text, 800, 120)[:-1]:
            last_line = chunk.text.split("\n")[-1]
            assert (
                last_line.endswith((".", "?", "!", ":"))
                or " | " in last_line
                or len(last_line) > 40
            ), last_line
