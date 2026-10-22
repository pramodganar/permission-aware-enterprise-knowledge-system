"""Answering the evaluation set through answer_question, over the fake embedder.

The relevance gate is off, so every question reaches the model and has
sources to record. What's checked is what the file of answers can be
trusted for: that each answer's sources are exactly passages its asker may
read, and that a provider failing, refusing or abstaining is counted for
what it is.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import select

from app.authorization.service import can_read
from app.models import Chunk, Document
from app.rag.answer import NO_INFORMATION_MESSAGE
from app.rag.llm import Completion, LLMError, LLMUnavailableError, StopReason
from app.rag.prompt import NOT_IN_SOURCES, Prompt
from evaluation import answers, security
from evaluation.company import Company, loaded_company, measuring_settings
from evaluation.dataset import Kind, QuestionSet, default_set
from evaluation.models import RepeatEverything
from evaluation.retrieval import write
from fakes import FakeEmbedder

NO_GATE = -1.0


@pytest.fixture(scope="module")
def company() -> Iterator[Company]:
    with loaded_company(measuring_settings(retrieval_min_score=NO_GATE), FakeEmbedder()) as c:
        yield c


@pytest.fixture(scope="module")
def repeated(company: Company) -> answers.Answers:
    return answers.run(company, RepeatEverything(), default_set())


@dataclass
class Scripted:
    """A model that gives each call the next reply a test wrote; an exception is raised."""

    replies: list[str | Exception]
    calls: int = 0
    name: str = "scripted"
    model: str = "scripted-model"
    prompts: list[Prompt] = field(default_factory=list)

    def complete(
        self, prompt: Prompt, *, max_output_tokens: int, timeout_seconds: float
    ) -> Completion:
        self.calls += 1
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return Completion(reply, self.model, StopReason.END, None, None)


def one(question_id: str) -> QuestionSet:
    return QuestionSet(
        version=1, questions=tuple(q for q in default_set().questions if q.id == question_id)
    )


def test_every_question_asked_is_recorded_with_its_sources(repeated: answers.Answers) -> None:
    asked = default_set().of_kind(*answers.ASKED)
    assert [a.id for a in repeated.answers] == [q.id for q in asked]
    assert len(asked) == 55
    assert all(a.status == "answered" and a.sources for a in repeated.answers)
    assert repeated.summary["failed"] == []
    assert repeated.model == "repeat-everything"
    assert repeated.settings["relevance_threshold"] == NO_GATE


def test_every_recorded_source_is_a_passage_its_asker_may_read(
    company: Company, repeated: answers.Answers
) -> None:
    for answer in repeated.answers:
        asker = company.person(answer.asker)
        for source in answer.sources:
            chunks = company.db.scalars(select(Chunk).where(Chunk.id.in_(source.chunk_ids))).all()
            assert len(chunks) == len(source.chunk_ids)
            document = company.db.get_one(Document, chunks[0].document_id)
            assert {c.document_id for c in chunks} == {document.id}
            assert document.title == source.document
            assert can_read(asker, document), (answer.id, source.document)
            assert chunks[0].text[:40] in source.text


def test_the_model_saw_exactly_the_recorded_sources(company: Company) -> None:
    model = Scripted(["Twenty-five days [S1]."])
    found = answers.run(company, model, one("alice-annual-leave")).answers[0]
    sent = model.prompts[0]
    assert sent.labels == tuple(s.label for s in found.sources)
    for source in found.sources:
        assert f'<source id="{source.label}" document="{source.document}"' in sent.user
    assert found.cited == ("S1",)
    assert found.text == "Twenty-five days [S1]."


def test_scoring_the_answers_of_a_model_that_cites_everything(repeated: answers.Answers) -> None:
    numbers = repeated.summary["answerable"]
    assert (numbers["measured"], numbers["answered"]) == (34, 34)
    # Citing every source cites the document and the evidence whenever they
    # were given. The fake embedder doesn't always find the right document.
    documents = {q.id: q.document for q in default_set().questions}
    given_its_document = sum(
        any(s.document == documents[a.id] for s in a.sources)
        for a in repeated.answers
        if a.kind is Kind.ANSWERABLE
    )
    assert numbers["answered_citing_its_document"] == given_its_document > 0
    assert numbers["answered_citing_the_evidence"] == numbers["given_the_evidence"]
    assert numbers["uncited"] == numbers["cut_off"] == numbers["with_made_up_citations"] == 0
    # With the gate off, nothing is abstained from.
    for kind in ("related", "off_topic"):
        assert repeated.summary["abstention"][kind]["abstained"] == 0


def test_a_model_that_finds_nothing_in_its_sources_abstains_everywhere(company: Company) -> None:
    found = answers.run(company, Scripted([NOT_IN_SOURCES]), default_set())
    assert all(a.text == NO_INFORMATION_MESSAGE and not a.cited for a in found.answers)
    assert all(a.reason == "not_in_sources" for a in found.answers)
    numbers = found.summary
    assert numbers["answerable"]["answered"] == 0
    assert len(numbers["answerable"]["not_answered"]) == 34
    assert numbers["abstention"]["related"] | {"answered": None} == {
        "measured": 10,
        "abstained": 10,
        "by_the_gate": 0,
        "by_the_model": 10,
        "answered": None,
    }
    assert numbers["abstention"]["off_topic"]["abstained"] == 11


def test_the_gate_abstaining_is_told_apart_from_the_model(company: Company) -> None:
    gated = company.settings.model_copy(update={"retrieval_min_score": 1.1})
    model = Scripted(["Something [S1]."])
    original, company.settings = company.settings, gated
    try:
        found = answers.run(company, model, one("grace-off-topic-capital"))
    finally:
        company.settings = original
    assert model.calls == 0
    assert found.answers[0].reason == "nothing_relevant"
    assert found.answers[0].sources == ()
    assert found.summary["abstention"]["off_topic"]["by_the_gate"] == 1


def test_a_provider_that_is_rate_limiting_is_waited_for(company: Company) -> None:
    # The service tries three times itself; the measurement then waits and asks again.
    replies: list[str | Exception] = [LLMUnavailableError("429")] * 3
    model = Scripted([*replies, "Twenty-five days [S1]."])
    waits: list[float] = []
    found = answers.run(
        company,
        model,
        one("alice-annual-leave"),
        pause=20.0,
        retry_wait=60.0,
        retries=3,
        sleep=waits.append,
    )
    assert found.answers[0].status == "answered"
    assert model.calls == 4
    assert waits == [20.0, 0.5, 1.0, 60.0, 20.0]


def test_a_question_that_never_gets_an_answer_is_failed_not_abstained(company: Company) -> None:
    model = Scripted([LLMUnavailableError("429")])
    found = answers.run(company, model, one("alice-moving-leave"), retries=1, sleep=lambda _: None)
    assert model.calls == 6
    gone = found.answers[0]
    assert (gone.status, gone.reason, gone.sources) == ("failed", "LLMUnavailableError", ())
    assert found.summary["failed"] == ["alice-moving-leave"]
    assert found.summary["abstention"]["related"]["measured"] == 0


def test_a_request_the_provider_refuses_is_not_tried_again(company: Company) -> None:
    model = Scripted([LLMError("400")])
    found = answers.run(company, model, one("alice-annual-leave"), retries=3, sleep=lambda _: None)
    assert model.calls == 1
    assert (found.answers[0].status, found.answers[0].reason) == ("failed", "LLMError")
    assert found.summary["answerable"]["measured"] == 0


def test_the_answers_read_back_as_written(repeated: answers.Answers, tmp_path: Path) -> None:
    path = tmp_path / "answers.json"
    write(repeated.model_dump(mode="json"), path)
    loaded = answers.load(path)
    assert loaded == repeated
    assert answers.score(loaded.answers, default_set()) == repeated.summary
    assert loaded.get("alice-annual-leave").kind is Kind.ANSWERABLE


def test_the_command_writes_the_answers_and_what_they_score(
    company: Company,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    @contextmanager
    def same_company(settings: object) -> Iterator[Company]:
        yield company

    monkeypatch.setattr(answers, "loaded_company", same_company)
    out = tmp_path / "answers.json"

    assert answers.main(["--model", "worst", "--output", str(out)]) == 0
    written = answers.load(out)
    assert len(written.answers) == 55
    assert written.summary == answers.score(written.answers, default_set())
    printed = capsys.readouterr().out
    assert "Answerable: answered 34 of 34" in printed
    assert "Failed: none" in printed


def test_with_groq_the_command_paces_itself(
    company: Company, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    @contextmanager
    def same_company(settings: object) -> Iterator[Company]:
        yield company

    paced: dict[str, object] = {}

    def run(*args: object, **pacing: object) -> answers.Answers:
        paced.update(pacing)
        return answers.Answers(
            model="m",
            prompt_version="1",
            settings={},
            answers=(),
            summary=answers.score((), default_set()),
        )

    monkeypatch.setattr(answers, "loaded_company", same_company)
    monkeypatch.setattr(answers, "settings_for", lambda choice: company.settings)
    monkeypatch.setattr(answers, "provider_for", lambda choice, settings: RepeatEverything())
    monkeypatch.setattr(answers, "run", run)

    assert answers.main(["--output", str(tmp_path / "a.json")]) == 0
    assert paced == {"pause": 20.0, "retry_wait": 60.0, "retries": 3}


def test_groq_needs_a_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(security, "groq_key", lambda: (_ for _ in ()).throw(SystemExit("no key")))

    with pytest.raises(SystemExit, match="no key"):
        answers.main(["--model", "groq", "--output", str(tmp_path / "x.json")])
