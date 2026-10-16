"""Turning retrieved chunks into the context the LLM reads.

The only input is a ``Retrieval``: chunks that passed both access checks and
the relevance gate. This module has no database session and no other source
of text, so it can't add anything the person may not read. It can only
leave chunks out.

Chunks become numbered sources, S1 being the most relevant. Neighbouring
chunks of the same document that share text are joined into one source, with
the shared text kept once. The model then reads a passage as it was written
instead of the same lines twice, and the size cap isn't spent on repeats.
Neighbours that share nothing stay apart: the text between them, a blank
line, isn't in either chunk. So every source is exactly a piece of a stored
document.

The cap (NFR-3) is on the characters of document text. A chunk that doesn't
fit is left out whole, never cut: half a table row or the start of a
sentence is worse than nothing.

Document text is data, not instructions. ``format_sources`` wraps each source
in tags and escapes the text, so a document that contains "</source>" can't
close its block and pass the rest off as something else (threat T14). That
doesn't stop a model from being swayed by what a document says. It does
mean the prompt can tell the model exactly where documents begin and end.
"""

import html
import logging
from collections import defaultdict
from dataclasses import dataclass
from itertools import pairwise

from app.retrieval.retriever import Retrieval, RetrievedChunk

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Source:
    """One passage of one document, made of one or more neighbouring chunks."""

    label: str
    document_id: int
    document_title: str
    document_version: int
    # In document order.
    chunk_ids: tuple[int, ...]
    text: str
    # The best score among its chunks.
    score: float


@dataclass(frozen=True)
class Context:
    user_id: int
    # Most relevant first. Labelled S1, S2, ... in that order.
    sources: tuple[Source, ...]
    # Retrieved chunks that didn't fit under the cap.
    left_out: int

    @property
    def is_empty(self) -> bool:
        return not self.sources

    @property
    def characters(self) -> int:
        return sum(len(source.text) for source in self.sources)

    @property
    def chunk_ids(self) -> frozenset[int]:
        return frozenset(chunk_id for source in self.sources for chunk_id in source.chunk_ids)

    def source(self, label: str) -> Source | None:
        return next((source for source in self.sources if source.label == label), None)


def build_context(retrieval: Retrieval, max_characters: int) -> Context:
    """Choose and join the retrieved chunks, most relevant first, up to the cap."""
    rank = {chunk.chunk_id: index for index, chunk in enumerate(retrieval.chunks)}
    chosen: dict[int, dict[int, RetrievedChunk]] = defaultdict(dict)
    used = 0
    left_out = 0

    for chunk in retrieval.chunks:
        # What it costs is what it adds: next to a chunk already chosen, the
        # shared text is already paid for, and a chunk already chosen costs
        # nothing.
        chosen[chunk.document_id][chunk.position] = chunk
        cost = _length(chosen) - used
        if used + cost > max_characters:
            del chosen[chunk.document_id][chunk.position]
            left_out += 1
            continue
        used += cost

    groups = [run for runs in chosen.values() for run in _runs(runs)]
    # A source is as relevant as its best chunk.
    groups.sort(key=lambda run: min(rank[chunk.chunk_id] for chunk in run))
    sources = tuple(
        Source(
            label=f"S{number}",
            document_id=run[0].document_id,
            document_title=run[0].document_title,
            document_version=run[0].document_version,
            chunk_ids=tuple(chunk.chunk_id for chunk in run),
            text=_joined(run),
            score=max(chunk.score for chunk in run),
        )
        for number, run in enumerate(groups, start=1)
    )
    context = Context(user_id=retrieval.user_id, sources=sources, left_out=left_out)

    # Counts only, as everywhere else.
    logger.info(
        "context built",
        extra={
            "user_id": retrieval.user_id,
            "sources": len(sources),
            "chunks": len(context.chunk_ids),
            "left_out": left_out,
            "characters": context.characters,
        },
    )
    return context


def format_sources(context: Context) -> str:
    """The sources as the LLM will read them, each in its own tagged block."""
    blocks = [
        f'<source id="{source.label}" document="{html.escape(source.document_title)}" '
        f'version="{source.document_version}">\n'
        f"{html.escape(source.text, quote=False)}\n"
        "</source>"
        for source in context.sources
    ]
    return "\n\n".join(blocks)


def _runs(by_position: dict[int, RetrievedChunk]) -> list[list[RetrievedChunk]]:
    """Group one document's chunks into runs of neighbours that share text."""
    runs: list[list[RetrievedChunk]] = []
    for position in sorted(by_position):
        chunk = by_position[position]
        previous = runs[-1][-1] if runs else None
        if (
            previous is not None
            and previous.position == position - 1
            and previous.end_offset >= chunk.start_offset
        ):
            runs[-1].append(chunk)
        else:
            runs.append([chunk])
    return runs


def _joined(run: list[RetrievedChunk]) -> str:
    """The text of a run of chunks, with the text they share kept once."""
    text = run[0].text
    for previous, chunk in pairwise(run):
        text += chunk.text[previous.end_offset - chunk.start_offset :]
    return text


def _length(chosen: dict[int, dict[int, RetrievedChunk]]) -> int:
    return sum(len(_joined(run)) for runs in chosen.values() for run in _runs(runs))
