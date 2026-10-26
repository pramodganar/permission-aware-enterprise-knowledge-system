"""One request's log, read the way an operator reads it (NFR-8).

Every line of a request carries its request ID, and each stage says how long
it took, so a slow answer can be traced to the step that was slow: embedding
the question, deciding the scope, searching, reranking or the model. The
lines are read as the service writes them, JSON on stdout, not as records.
"""

import json
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from apiclient import company_client, headers
from app.core.config import Settings
from app.core.logging_setup import configure_logging
from app.rag.prompt import Prompt
from fakes import FakeLLM, FakeReranker
from logcheck import logging_restored

QUESTION = "How many days of annual leave do I get?"
# Long enough to tell apart from everything else a request does here.
SLOW = 0.15

# Each stage's line and what it times.
STAGES = {
    "question processed": "checking and embedding the question",
    "access scope decided": "working out what the person may read",
    "search done": "the vector search",
    "chunks retrieved": "the whole retrieval, reranking included",
}


class SlowReranker(FakeReranker):
    def score(self, question: str, passages: Sequence[str]) -> list[float]:
        time.sleep(SLOW)
        return super().score(question, passages)


def slow_reply(prompt: Prompt) -> str:
    time.sleep(SLOW)
    return "Twenty-five days a year [S1]."


Lines = dict[str, dict[str, Any]]


@pytest.fixture
def run(
    db: Session, engine: Engine, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> Iterator[Callable[[str], Lines]]:
    """Make one request; get back its log lines by message, all with one request ID."""
    changed = settings.model_copy(update={"retrieval_min_score": -1.0})
    with (
        logging_restored(),
        company_client(db, engine, changed, FakeLLM(slow_reply), SlowReranker()) as client,
    ):
        auth = headers(client, "david")

        def one(path: str) -> Lines:
            capsys.readouterr()
            configure_logging("INFO")
            response = client.post(path, json={"question": QUESTION}, headers=auth)
            assert response.status_code == 200, response.text
            ours = response.headers["X-Request-ID"]
            lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
            mine = [line for line in lines if line.get("request_id") == ours]
            # Every line of the service's own is this request's.
            assert mine == [line for line in lines if line["logger"].startswith("app.")]
            return {line["message"]: line for line in mine}

        yield one


@pytest.mark.parametrize("path", ["/search", "/ask"])
def test_every_stage_says_how_long_it_took(run: Callable[[str], Lines], path: str) -> None:
    lines = run(path)

    for stage in [*STAGES, "request handled"]:
        assert isinstance(lines[stage]["milliseconds"], int), stage
        assert lines[stage]["milliseconds"] >= 0, stage


@pytest.mark.parametrize("path", ["/search", "/ask"])
def test_a_slow_reranker_shows_in_reranking_and_nowhere_before_it(
    run: Callable[[str], Lines], path: str
) -> None:
    lines = run(path)
    slow = SLOW * 1000

    assert lines["chunks retrieved"]["rerank_milliseconds"] >= slow
    assert (
        lines["chunks retrieved"]["milliseconds"]
        >= lines["chunks retrieved"]["rerank_milliseconds"]
    )
    assert lines["search done"]["milliseconds"] < slow
    assert lines["access scope decided"]["milliseconds"] < slow
    assert lines["question processed"]["milliseconds"] < slow


def test_a_slow_model_shows_in_generation_not_retrieval(run: Callable[[str], Lines]) -> None:
    lines = run("/ask")
    slow = SLOW * 1000
    answered = lines["question answered"]

    assert lines["model answered"]["milliseconds"] >= slow
    assert answered["generation_milliseconds"] >= slow
    # Retrieval holds the slow reranker, and nothing of the model: beyond its
    # own stages, what is left is far less than the model's wait.
    within = lines["question processed"]["milliseconds"] + lines["chunks retrieved"]["milliseconds"]
    assert answered["retrieval_milliseconds"] >= slow
    assert answered["retrieval_milliseconds"] - within < slow / 2


@pytest.mark.parametrize("path", ["/search", "/ask"])
def test_the_request_takes_at_least_as_long_as_its_stages(
    run: Callable[[str], Lines], path: str
) -> None:
    lines = run(path)
    stages = lines["question processed"]["milliseconds"] + lines["chunks retrieved"]["milliseconds"]
    if path == "/ask":
        stages += lines["model answered"]["milliseconds"]

    # Rounding can put each stage up to half a millisecond over.
    assert lines["request handled"]["milliseconds"] >= stages - 2


def test_without_a_reranker_there_is_no_reranking_time(
    db: Session, engine: Engine, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with logging_restored(), company_client(db, engine, settings) as client:
        auth = headers(client, "david")
        configure_logging("INFO")
        capsys.readouterr()
        client.post("/search", json={"question": QUESTION}, headers=auth)
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    (retrieved,) = [line for line in lines if line["message"] == "chunks retrieved"]
    assert retrieved["rerank_milliseconds"] is None
    assert isinstance(retrieved["milliseconds"], int)
