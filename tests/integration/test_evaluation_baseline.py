"""The committed retrieval results are what the code measures now.

evaluation/results/ holds the baseline the docs quote. If a change to
chunking, embedding or retrieval moves the numbers, this fails until the
results are measured again and committed with the change, so the docs never
quote numbers the code no longer gives.

Loads the real embedding model and the reranker: pytest -m model
"""

import json

import pytest

from app.retrieval.reranking import load_reranker
from evaluation import retrieval
from evaluation.company import loaded_company, measuring_settings
from evaluation.dataset import default_set

pytestmark = pytest.mark.model

RERANKER = "cross-encoder/ms-marco-MiniLM-L6-v2"


@pytest.mark.parametrize(
    ("reranker_model", "baseline"),
    [(None, "retrieval.json"), (RERANKER, "retrieval-reranked.json")],
    ids=["plain", "reranked"],
)
def test_the_committed_results_are_what_the_code_measures(
    reranker_model: str | None, baseline: str
) -> None:
    settings = measuring_settings(reranker_model)
    questions = default_set()
    with loaded_company(settings) as company:
        reranker = load_reranker(settings)
        measured = retrieval.report(
            retrieval.evaluate(company, questions, reranker),
            questions=questions,
            company=company,
            reranked=reranker is not None,
        )

    committed = json.loads((retrieval.RESULTS / baseline).read_text(encoding="utf-8"))
    assert measured == committed
