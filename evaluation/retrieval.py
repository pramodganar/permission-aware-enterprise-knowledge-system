"""Retrieval quality on the evaluation set: are the right chunks found, and how high?

    python -m evaluation.retrieval [--rerank] [--output PATH]

Every answerable question is asked by its asker, through the same
``retrieve`` the service uses, with the person's own access scope. The
relevance gate is turned off for the ranking, so the ranking is judged on
its own; whether the gate would have let the question through is reported
separately, because an answerable question the gate stops is a wrongful
refusal at retrieval.

The relevant chunks of a question are the chunks of its document that hold
one of its evidence quotes (evaluation/dataset.py). Each person is measured
only against what they may read, which the set's own checks guarantee.

For K in 1, 3 and 5, per question and then averaged over questions:

    hit@K        whether any relevant chunk is in the top K
    doc_hit@K    whether any chunk of the right document is in the top K, which
                 tells a wrong document apart from the wrong part of the right one
    recall@K     relevant chunks in the top K / relevant chunks
    precision@K  relevant chunks in the top K / K
    MRR          1 / rank of the first relevant chunk, 0 if none in the top 5

Most questions have one relevant chunk, so precision@5 can be at most 0.2
for them: it says how much of what the model is given is beside the point,
not how good the ranking is.

The results are written as JSON, rounded so that a rerun on the same code
gives the same file. evaluation/results/retrieval.json is the baseline;
a change to retrieval that moves it shows up as a diff.
"""

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any

from sqlalchemy import select

from app.models import Chunk, Document, DocumentStatus
from app.retrieval.query import process_question
from app.retrieval.reranking import Reranker, load_reranker
from app.retrieval.retriever import retrieve
from evaluation.company import Company, loaded_company, measuring_settings
from evaluation.dataset import Kind, Question, QuestionSet, contains, default_set

KS = (1, 3, 5)
DEPTH = max(KS)
RESULTS = Path(__file__).with_name("results")
NO_GATE = -1.0


@dataclass(frozen=True)
class Ranked:
    """Where one question's relevant chunks came out."""

    question_id: str
    asker: str
    relevant: frozenset[int]
    # The chunk IDs retrieved, best first, at most DEPTH, and the document
    # each one belongs to.
    ranked: tuple[int, ...]
    ranked_documents: tuple[int, ...]
    document_id: int
    # The best score among the chunks the person may read.
    best_score: float | None
    passes_gate: bool

    def found(self, k: int) -> int:
        return len(self.relevant.intersection(self.ranked[:k]))

    def hit(self, k: int) -> float:
        return 1.0 if self.found(k) else 0.0

    def document_hit(self, k: int) -> float:
        return 1.0 if self.document_id in self.ranked_documents[:k] else 0.0

    def recall(self, k: int) -> float:
        return self.found(k) / len(self.relevant)

    def precision(self, k: int) -> float:
        return self.found(k) / k

    @property
    def first_relevant(self) -> int | None:
        """The rank, from 1, of the first relevant chunk, if any is in the top DEPTH."""
        return next((i + 1 for i, c in enumerate(self.ranked) if c in self.relevant), None)

    @property
    def reciprocal_rank(self) -> float:
        return 0.0 if self.first_relevant is None else 1.0 / self.first_relevant


def answering_document(company: Company, question: Question) -> int:
    document_id: int = company.db.scalars(
        select(Document.id).where(
            Document.title == question.document, Document.status == DocumentStatus.ACTIVE
        )
    ).one()
    return document_id


def relevant_chunks(company: Company, question: Question) -> frozenset[int]:
    """The chunks of the question's document that hold one of its evidence quotes."""
    rows = company.db.execute(
        select(Chunk.id, Chunk.text).where(
            Chunk.document_id == answering_document(company, question)
        )
    ).all()
    found = frozenset(
        chunk_id for chunk_id, text in rows if any(contains(text, q) for q in question.evidence)
    )
    if not found:
        raise ValueError(f"{question.id}: no chunk holds any of its evidence")
    return found


def rank(company: Company, question: Question, reranker: Reranker | None = None) -> Ranked:
    settings = company.settings
    retrieval = retrieve(
        company.db,
        company.index,
        company.person(question.asker),
        process_question(question.question, settings, company.embedder),
        DEPTH,
        NO_GATE,
        reranker=reranker,
        candidates=max(settings.rerank_candidates, DEPTH) if reranker else None,
    )
    best = retrieval.best_score
    return Ranked(
        question_id=question.id,
        asker=question.asker,
        relevant=relevant_chunks(company, question),
        ranked=tuple(chunk.chunk_id for chunk in retrieval.chunks),
        ranked_documents=tuple(chunk.document_id for chunk in retrieval.chunks),
        document_id=answering_document(company, question),
        best_score=best,
        passes_gate=best is not None and best >= settings.retrieval_min_score,
    )


def evaluate(
    company: Company, questions: QuestionSet, reranker: Reranker | None = None
) -> list[Ranked]:
    return [rank(company, q, reranker) for q in questions.of_kind(Kind.ANSWERABLE)]


def summary(results: Sequence[Ranked]) -> dict[str, Any]:
    """The averages over a group of questions."""
    if not results:
        raise ValueError("nothing to summarise")
    numbers: dict[str, Any] = {"questions": len(results)}
    for k in KS:
        numbers[f"hit@{k}"] = _round(fmean(r.hit(k) for r in results))
        numbers[f"doc_hit@{k}"] = _round(fmean(r.document_hit(k) for r in results))
        numbers[f"recall@{k}"] = _round(fmean(r.recall(k) for r in results))
        numbers[f"precision@{k}"] = _round(fmean(r.precision(k) for r in results))
    numbers["mrr"] = _round(fmean(r.reciprocal_rank for r in results))
    numbers["stopped_by_gate"] = sum(not r.passes_gate for r in results)
    return numbers


def report(
    results: Sequence[Ranked], *, questions: QuestionSet, company: Company, reranked: bool
) -> dict[str, Any]:
    settings = company.settings
    people = sorted({r.asker for r in results})
    return {
        "set_version": questions.version,
        "settings": {
            "embedding_model": settings.embedding_model,
            "chunk_max_characters": settings.chunk_max_characters,
            "chunk_overlap_characters": settings.chunk_overlap_characters,
            "relevance_threshold": settings.retrieval_min_score,
            "reranker": settings.reranker_model if reranked else None,
        },
        "overall": summary(results),
        "by_person": {name: summary([r for r in results if r.asker == name]) for name in people},
        # Every question, so a change in the numbers can be traced to the
        # questions that moved.
        "questions": [
            {
                "id": r.question_id,
                "asker": r.asker,
                "first_relevant": r.first_relevant,
                "right_document_first": r.document_hit(1) == 1.0,
                "relevant": len(r.relevant),
                "best_score": None if r.best_score is None else _round(r.best_score),
                "passes_gate": r.passes_gate,
            }
            for r in results
        ],
    }


def _round(value: float) -> float:
    return round(value, 3)


def table(numbers: dict[str, Any]) -> list[str]:
    """The report as lines of text for the terminal."""
    columns = [
        "questions",
        *(f"hit@{k}" for k in KS),
        "doc_hit@1",
        "recall@5",
        "precision@5",
        "mrr",
    ]
    rows = [("overall", numbers["overall"]), *numbers["by_person"].items()]
    lines = [" ".join(f"{c:>11}" for c in ["", *columns])]
    for name, row in rows:
        lines.append(" ".join([f"{name:>11}", *(f"{row[c]:>11}" for c in columns)]))
    missed = [q["id"] for q in numbers["questions"] if q["first_relevant"] is None]
    stopped = [q["id"] for q in numbers["questions"] if not q["passes_gate"]]
    lines.append(f"No relevant chunk in the top {DEPTH}: {', '.join(missed) or 'none'}")
    lines.append(f"Stopped by the relevance gate: {', '.join(stopped) or 'none'}")
    return lines


def write(numbers: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(numbers, indent=2) + "\n", encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure retrieval on the evaluation set.")
    parser.add_argument("--rerank", action="store_true", help="rerank with the cross-encoder first")
    parser.add_argument("--output", type=Path, help="where to write the results (JSON)")
    arguments = parser.parse_args(list(argv) if argv is not None else None)

    reranker_model = "cross-encoder/ms-marco-MiniLM-L6-v2" if arguments.rerank else None
    settings = measuring_settings(reranker_model=reranker_model)
    output = arguments.output or RESULTS / (
        "retrieval-reranked.json" if arguments.rerank else "retrieval.json"
    )
    questions = default_set()
    with loaded_company(settings) as company:
        reranker = load_reranker(settings)
        numbers = report(
            evaluate(company, questions, reranker),
            questions=questions,
            company=company,
            reranked=reranker is not None,
        )
    write(numbers, output)
    print("\n".join(table(numbers)))
    print(f"Written to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
