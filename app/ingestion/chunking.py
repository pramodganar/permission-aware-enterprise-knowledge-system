"""Splitting a document's stored text into chunks.

The method and the reasons for it are in docs/chunking.md. In short: the text
is cut into lines, consecutive lines are packed into chunks up to a size
limit, a chunk is closed at the best nearby boundary, and each chunk starts
with the last lines of the one before.

Every chunk is an exact slice of the text it came from, so it can be checked
and cited by its offsets. This module works on one document's text at a time.
It never sees two documents, which is what keeps text from one out of the
chunks of another.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

# How much of a chunk may be given up to end it at a better place.
_LOOK_BACK = 0.25

_SENTENCE_END = re.compile(r"[.?!][\"')\]]*$")
_SENTENCE_BREAK = re.compile(r"[.?!][\"')\]]*\s")


@dataclass(frozen=True)
class TextChunk:
    """A slice of a document's text: ``text == document_text[start:end]``."""

    position: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Piece:
    """A line of the text, or part of a line that was too long to keep whole."""

    start: int
    end: int
    after_blank_line: bool
    ends_sentence: bool


def chunk_text(text: str, max_characters: int, overlap_characters: int) -> list[TextChunk]:
    """Split text into chunks of at most ``max_characters``.

    Consecutive chunks share up to ``overlap_characters`` of whole lines.
    The same text and settings always give the same chunks.
    """
    if max_characters < 1:
        raise ValueError("the chunk size must be at least 1")
    if not 0 <= overlap_characters < max_characters:
        raise ValueError("the overlap must be smaller than the chunk size")

    pieces = list(_pieces(text, max_characters))
    chunks: list[TextChunk] = []
    first = 0
    new_from = 0  # the first piece that is not in any chunk yet
    while first < len(pieces):
        # Take as many pieces as fit. One always fits, since no piece is over the limit.
        last = first
        while (
            last + 1 < len(pieces) and pieces[last + 1].end - pieces[first].start <= max_characters
        ):
            last += 1
        if last + 1 < len(pieces):
            last = _better_ending(pieces, first, last, new_from)

        start, end = pieces[first].start, pieces[last].end
        chunks.append(TextChunk(position=len(chunks), start=start, end=end, text=text[start:end]))
        if last + 1 == len(pieces):
            break
        new_from = last + 1
        first = _next_start(pieces, first, last, max_characters, overlap_characters)
    return chunks


def _pieces(text: str, max_characters: int) -> Iterator[_Piece]:
    """Yield the non-blank lines of the text, cutting any that are over the limit."""
    offset = 0
    after_blank_line = False
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            after_blank_line = True
        else:
            start = offset + line.index(stripped)
            for piece_start, piece_end in _cut(text, start, start + len(stripped), max_characters):
                yield _Piece(
                    start=piece_start,
                    end=piece_end,
                    after_blank_line=after_blank_line,
                    ends_sentence=_SENTENCE_END.search(text[piece_start:piece_end]) is not None,
                )
                after_blank_line = False
        offset += len(line) + 1


def _cut(text: str, start: int, end: int, max_characters: int) -> Iterator[tuple[int, int]]:
    """Cut text[start:end] into parts no longer than the limit.

    Each cut is made after the last sentence that fits, or failing that
    between words, or failing that in the middle of a word.
    """
    while end - start > max_characters:
        window = text[start : start + max_characters + 1]
        sentence_breaks = [match.end() - 1 for match in _SENTENCE_BREAK.finditer(window)]
        if sentence_breaks:
            length = sentence_breaks[-1]
        else:
            spaces = [index for index, character in enumerate(window) if character.isspace()]
            length = spaces[-1] if spaces else max_characters
        yield start, start + len(window[:length].rstrip())
        start += length
        while text[start].isspace():
            start += 1
    yield start, end


def _better_ending(pieces: list[_Piece], first: int, last: int, new_from: int) -> int:
    """Return where to end a chunk that could run from ``first`` to ``last``.

    Looks back over the end of the chunk for a better place to stop: after a
    finished sentence that is followed by a blank line, then after any finished
    sentence, then before any blank line. Ending on a heading or in the middle
    of a sentence is what this avoids.

    The chunk always keeps at least one piece from ``new_from`` on. Without
    that, a chunk that starts with overlap could be cut back to the overlap
    alone and add nothing.
    """
    shortest = pieces[first].start + (pieces[last].end - pieces[first].start) * (1 - _LOOK_BACK)
    candidates = [index for index in range(last, new_from - 1, -1) if pieces[index].end >= shortest]

    # A blank line alone is not enough: a heading is followed by one too.
    for index in candidates:
        if pieces[index].ends_sentence and pieces[index + 1].after_blank_line:
            return index
    for index in candidates:
        if pieces[index].ends_sentence:
            return index
    for index in candidates:
        if pieces[index + 1].after_blank_line:
            return index
    return last


def _next_start(
    pieces: list[_Piece], first: int, last: int, max_characters: int, overlap_characters: int
) -> int:
    """Return the piece the next chunk starts with, after one ending at ``last``.

    It goes back over as many whole pieces as fit in the overlap. It never goes
    back to ``first``, so every chunk starts later than the one before.
    """
    start = last + 1
    while start - 1 > first and pieces[last].end - pieces[start - 1].start <= overlap_characters:
        start -= 1
    # If the overlap would leave no room for the next new piece, drop it. The
    # chunk would otherwise only repeat text that is already in this one.
    while start <= last and pieces[last + 1].end - pieces[start].start > max_characters:
        start += 1
    return start
