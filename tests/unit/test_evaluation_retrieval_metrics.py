"""The retrieval metrics, worked out by hand on small rankings."""

import json
from pathlib import Path

import pytest

from evaluation.retrieval import Ranked, summary, table, write


def ranked(
    ranking: tuple[int, ...],
    relevant: set[int],
    *,
    documents: tuple[int, ...] | None = None,
    document_id: int = 1,
    passes_gate: bool = True,
    asker: str = "alice",
) -> Ranked:
    return Ranked(
        question_id=f"q-{len(ranking)}-{min(relevant)}",
        asker=asker,
        relevant=frozenset(relevant),
        ranked=ranking,
        ranked_documents=documents or tuple(1 for _ in ranking),
        document_id=document_id,
        best_score=0.5,
        passes_gate=passes_gate,
    )


def test_a_relevant_chunk_first_is_a_perfect_score() -> None:
    first = ranked((7, 2, 3, 4, 5), {7})

    assert (first.hit(1), first.recall(1), first.precision(1)) == (1.0, 1.0, 1.0)
    assert first.precision(5) == pytest.approx(0.2)
    assert (first.first_relevant, first.reciprocal_rank) == (1, 1.0)


def test_a_relevant_chunk_third_counts_from_k_three() -> None:
    third = ranked((1, 2, 7, 4, 5), {7})

    assert (third.hit(1), third.hit(3), third.hit(5)) == (0.0, 1.0, 1.0)
    assert third.first_relevant == 3
    assert third.reciprocal_rank == pytest.approx(1 / 3)


def test_two_relevant_chunks_share_recall() -> None:
    both = ranked((7, 2, 8, 4, 5), {7, 8, 9})

    assert both.recall(1) == pytest.approx(1 / 3)
    assert both.recall(3) == pytest.approx(2 / 3)
    assert both.precision(3) == pytest.approx(2 / 3)
    assert both.reciprocal_rank == 1.0


def test_nothing_relevant_in_the_top_five_scores_zero() -> None:
    missed = ranked((1, 2, 3, 4, 5), {7})

    assert (missed.hit(5), missed.recall(5), missed.reciprocal_rank) == (0.0, 0.0, 0.0)
    assert missed.first_relevant is None


def test_fewer_than_k_retrieved_still_divides_precision_by_k() -> None:
    short = ranked((7,), {7})

    assert short.precision(5) == pytest.approx(0.2)


def test_the_right_document_first_is_told_apart_from_the_right_chunk() -> None:
    neighbour = ranked((1, 7, 3), {7}, documents=(4, 4, 9), document_id=4)
    elsewhere = ranked((1, 7, 3), {7}, documents=(9, 4, 4), document_id=4)

    assert (neighbour.hit(1), neighbour.document_hit(1)) == (0.0, 1.0)
    assert (elsewhere.hit(1), elsewhere.document_hit(1), elsewhere.document_hit(3)) == (
        0.0,
        0.0,
        1.0,
    )


def test_a_summary_averages_over_questions_and_counts_the_gate() -> None:
    results = [
        ranked((7, 2, 3, 4, 5), {7}),
        ranked((1, 2, 7, 4, 5), {7}, passes_gate=False),
        ranked((1, 2, 3, 4, 5), {7}),
    ]

    numbers = summary(results)

    assert numbers["questions"] == 3
    assert numbers["hit@1"] == pytest.approx(0.333)
    assert numbers["hit@3"] == pytest.approx(0.667)
    assert numbers["mrr"] == pytest.approx(round((1 + 1 / 3 + 0) / 3, 3))
    assert numbers["stopped_by_gate"] == 1


def test_a_summary_of_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="nothing"):
        summary([])


def test_the_table_names_the_misses_and_the_stopped(tmp_path: Path) -> None:
    results = [ranked((7, 2), {7}), ranked((1, 2), {7}, passes_gate=False, asker="bob")]
    numbers = {
        "overall": summary(results),
        "by_person": {"alice": summary(results[:1]), "bob": summary(results[1:])},
        "questions": [
            {"id": "found", "first_relevant": 1, "passes_gate": True},
            {"id": "lost", "first_relevant": None, "passes_gate": False},
        ],
    }

    lines = table(numbers)

    assert lines[0].split() == [
        "questions",
        "hit@1",
        "hit@3",
        "hit@5",
        "doc_hit@1",
        "recall@5",
        "precision@5",
        "mrr",
    ]
    assert [line.split()[0] for line in lines[1:4]] == ["overall", "alice", "bob"]
    assert lines[-2] == "No relevant chunk in the top 5: lost"
    assert lines[-1] == "Stopped by the relevance gate: lost"

    path = tmp_path / "out" / "retrieval.json"
    write(numbers, path)
    assert json.loads(path.read_text(encoding="utf-8")) == numbers
