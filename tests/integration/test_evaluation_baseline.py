"""The committed results are what the code measures now.

evaluation/results/ holds the baseline the docs quote. If a change to
chunking, embedding or retrieval moves the numbers, this fails until the
results are measured again and committed with the change, so the docs never
quote numbers the code no longer gives.

Loads the real embedding model and the reranker: pytest -m model
"""

import json

import pytest

from app.retrieval.embeddings import load_embedder
from app.retrieval.reranking import load_reranker
from evaluation import injection, retrieval, security
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


@pytest.mark.parametrize(
    ("gate", "baseline"),
    [(None, "security-worst.json"), (retrieval.NO_GATE, "security-worst-no-gate.json")],
    ids=["gate", "no gate"],
)
def test_the_committed_security_results_are_what_the_code_measures(
    gate: float | None, baseline: str
) -> None:
    # The model that repeats everything is deterministic, so its run is a
    # baseline. A run with a real model is a record, not something to repeat.
    with (
        loaded_company(security.settings_for("worst", gate)) as company,
        security.started(company, "worst") as service,
    ):
        measured = security.report(
            *security.evaluate(service, default_set()), choice="worst", service=service
        )

    committed = json.loads((retrieval.RESULTS / baseline).read_text(encoding="utf-8"))
    assert measured == committed
    assert measured["leaks"] == 0


def test_the_committed_injection_results_are_what_the_code_measures() -> None:
    # The model that obeys every planted instruction is deterministic too.
    settings = security.settings_for("worst")
    embedder = load_embedder(settings)
    tried = injection.run(lambda: loaded_company(settings, embedder), injection.Obedient())
    measured = injection.summary(tried, model="obedient")

    committed = json.loads(
        (retrieval.RESULTS / "injection-obedient.json").read_text(encoding="utf-8")
    )
    assert measured == committed
    assert measured["access_harm"] == measured["link_harm"] == 0
