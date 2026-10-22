"""Timing answer_question stage by stage, over the fake embedder.

The times themselves mean nothing here. What's checked is that each stage's
time lands where it belongs, that a failure is told apart, and that the
service's own functions are put back however the measurement ends.
"""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.models import Chunk, Document
from app.rag.llm import LLMError
from app.rag.prompt import Prompt
from app.retrieval.scope import access_scope
from evaluation import latency
from evaluation.company import Company, loaded_company, measuring_settings
from evaluation.dataset import PEOPLE, QuestionSet, default_set
from evaluation.models import RepeatEverything
from evaluation.retrieval import RERANKER_MODEL
from fakes import FakeEmbedder, FakeLLM, FakeReranker

NO_GATE = -1.0
RETRIEVER_STAGES = ("embed", "scope", "search", "load_and_check", "can_read", "context")


@pytest.fixture(scope="module")
def company() -> Iterator[Company]:
    with loaded_company(measuring_settings(retrieval_min_score=NO_GATE), FakeEmbedder()) as c:
        yield c


def one(question_id: str) -> QuestionSet:
    return QuestionSet(
        version=1, questions=tuple(q for q in default_set().questions if q.id == question_id)
    )


def current(stage: str) -> Any:
    module, name = latency.STAGES[stage]
    return getattr(module, name)


def originals() -> dict[str, Any]:
    return {stage: current(stage) for stage in latency.STAGES}


def test_every_question_is_timed_in_every_stage_after_the_warm_up(company: Company) -> None:
    timings = latency.run(company, RepeatEverything(), default_set(), rounds=2)
    assert len(timings) == 2 * 73
    for t in timings:
        assert set(RETRIEVER_STAGES) <= t.stages.keys(), t.question_id
        # With the gate off, every question with a scope reaches the model.
        assert t.reached_model
        assert t.failed is None
        assert t.unfiltered_search > 0
        assert t.total >= sum(
            t.stages[s] for s in ("embed", "scope", "search", "load_and_check", "context", "model")
        )
        # can_read runs inside the loading, so it's part of that stage.
        assert t.stages["can_read"] <= t.stages["load_and_check"]


def test_the_time_of_a_slow_stage_lands_in_that_stage(company: Company) -> None:
    def slow(prompt: Prompt) -> str:
        time.sleep(0.05)
        return "Twenty-five days [S1]."

    t = latency.run(company, FakeLLM(slow), one("alice-annual-leave"), rounds=1)[0]
    assert t.stages["model"] >= 0.05
    assert t.total >= 0.05
    assert t.stages["embed"] < 0.05
    assert t.answered


def test_reranking_is_timed_when_it_runs(company: Company) -> None:
    reranker = FakeReranker()
    t = latency.run(
        company, RepeatEverything(), one("alice-annual-leave"), rounds=1, reranker=reranker
    )[0]
    assert "rerank" in t.stages
    assert len(reranker.calls) == 2  # the warm-up and the measured round


def test_a_question_the_model_fails_on_is_marked_failed(company: Company) -> None:
    timings = latency.run(company, FakeLLM([LLMError("400")]), one("alice-annual-leave"), rounds=1)
    assert timings[0].failed == "LLMError"
    assert not timings[0].answered
    assert latency.summary(timings)["failed"] == ["alice-annual-leave"]


def test_the_services_functions_are_put_back_after_a_run(company: Company) -> None:
    before = originals()
    latency.run(company, FakeLLM([LLMError("400")]), one("alice-annual-leave"), rounds=1)
    assert originals() == before


def test_the_services_functions_are_put_back_when_the_measurement_fails() -> None:
    before = originals()
    clock = latency.Clock()
    with pytest.raises(RuntimeError), latency.timing(clock):
        assert current("scope") is not before["scope"]
        raise RuntimeError("stopped")
    assert originals() == before
    assert current("model") is before["model"]


def test_without_a_warm_up_every_round_counts_and_each_question_is_paced(
    company: Company,
) -> None:
    waits: list[float] = []
    timings = latency.run(
        company,
        RepeatEverything(),
        one("alice-annual-leave"),
        rounds=1,
        warm_up=False,
        pause=20.0,
        sleep=waits.append,
    )
    assert len(timings) == 1
    assert waits == [20.0]


def test_the_report_says_what_was_measured_and_where(company: Company) -> None:
    timings = latency.run(company, RepeatEverything(), one("alice-annual-leave"), rounds=1)
    numbers = latency.report(timings, company, choice="worst", reranked=False, rounds=1)
    assert numbers["settings"]["chunks_indexed"] == company.index.size
    assert numbers["machine"]["cpus"] >= 1
    assert numbers["stages_ms"]["total"]["count"] == 1
    assert any("total" in line for line in latency.lines(numbers))


def test_the_command_writes_the_report(
    company: Company,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    @contextmanager
    def same_company(settings: object) -> Iterator[Company]:
        yield company

    monkeypatch.setattr(latency, "loaded_company", same_company)
    out = tmp_path / "latency.json"
    assert latency.main(["--rounds", "1", "--output", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["timings"] == 73
    assert "Failed: none" in capsys.readouterr().out


def test_with_groq_one_paced_round_and_no_warm_up(
    company: Company, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    @contextmanager
    def same_company(settings: object) -> Iterator[Company]:
        yield company

    asked: dict[str, Any] = {}

    def run(*args: Any, **pacing: Any) -> list[latency.Timed]:
        asked.update(pacing)
        return []

    monkeypatch.setattr(latency, "loaded_company", same_company)
    monkeypatch.setattr(latency, "settings_for", lambda choice: company.settings)
    monkeypatch.setattr(latency, "provider_for", lambda choice, settings: RepeatEverything())
    monkeypatch.setattr(latency, "run", run)
    monkeypatch.setattr(latency, "report", lambda timings, company, **kw: {"kw": kw})
    monkeypatch.setattr(latency, "lines", lambda numbers: [])

    assert latency.main(["--model", "groq", "--output", str(tmp_path / "g.json")]) == 0
    assert asked == {"rounds": 1, "warm_up": False, "reranker": None, "pause": 20.0}


def test_reranking_loads_the_cross_encoder_named_for_it(
    company: Company, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    @contextmanager
    def same_company(settings: Any) -> Iterator[Company]:
        named.append(settings.reranker_model)
        yield company

    named: list[str | None] = []
    reranker = FakeReranker()
    monkeypatch.setattr(latency, "loaded_company", same_company)
    monkeypatch.setattr(latency, "load_reranker", lambda settings: reranker)

    out = tmp_path / "r.json"
    assert latency.main(["--rerank", "--rounds", "1", "--output", str(out)]) == 0
    assert named == [RERANKER_MODEL]
    assert json.loads(out.read_text(encoding="utf-8"))["reranked"] is True
    assert reranker.calls


# --- at a larger size ---------------------------------------------------------------------


@contextmanager
def fresh_company() -> Iterator[Company]:
    # Copying chunks changes the database, so not the module's company.
    with loaded_company(measuring_settings(), FakeEmbedder()) as c:
        yield c


def test_copies_keep_who_may_read_what_and_are_all_indexed() -> None:
    with fresh_company() as company:
        before = {
            name: len(access_scope(company.db, company.person(name)).chunk_ids) for name in PEOPLE
        }
        chunks = company.index.size
        index = latency.scaled(company, 3)
        assert index.size == 3 * chunks
        assert index.chunk_ids == frozenset(company.db.scalars(select(Chunk.id)))
        for name in PEOPLE:
            scope = access_scope(company.db, company.person(name))
            assert len(scope.chunk_ids) == 3 * before[name], name
        # Every copy belongs to a document that already had chunks.
        documents = set(company.db.scalars(select(Chunk.document_id)))
        assert len(documents) == len(set(company.db.scalars(select(Document.id))))


def test_each_reader_is_timed_on_the_larger_index() -> None:
    with fresh_company() as company:
        index = latency.scaled(company, 2)
        numbers = latency.at_scale(company, index, repeats=3)
    assert numbers["chunks_indexed"] == index.size
    people = numbers["people"]
    assert set(people) == set(PEOPLE)
    assert people["henry"]["chunks_in_scope"] == 0
    assert people["grace"]["chunks_in_scope"] > people["alice"]["chunks_in_scope"] > 0
    for p in people.values():
        assert p["scope_ms"]["count"] == p["search_ms"]["count"] == 3
    assert any("grace" in line for line in latency.scale_lines(numbers))


def test_the_command_measures_at_scale_instead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    @contextmanager
    def company_for(settings: object) -> Iterator[Company]:
        with fresh_company() as c:
            yield c

    def no_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the questions were asked")

    monkeypatch.setattr(latency, "loaded_company", company_for)
    monkeypatch.setattr(latency, "run", no_run)
    out = tmp_path / "scale.json"
    assert latency.main(["--scale", "2", "--output", str(out)]) == 0
    numbers = json.loads(out.read_text(encoding="utf-8"))
    assert numbers["copies"] == 2
    assert numbers["people"]["grace"]["chunks_in_scope"] > 0
