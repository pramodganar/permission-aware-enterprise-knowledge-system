"""Measuring leakage and over-blocking through the API, end to end.

The run over the fake embedder, with the relevance gate off and the model
that repeats everything it was given, is the regression gate: zero leaks,
every request audited. The detector is tested too, on responses built to
leak in each way, because a detector that can't see a leak reports zero for
the wrong reason.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.models import Document
from app.rag.llm import Completion, LLMUnavailableError, StopReason
from app.rag.prompt import Prompt
from evaluation import security
from evaluation.company import Company, loaded_company, measuring_settings
from evaluation.dataset import Kind, Question, default_set
from evaluation.models import RepeatEverything
from fakes import FakeEmbedder

NO_GATE = -1.0


@pytest.fixture(scope="module")
def company() -> Iterator[Company]:
    with loaded_company(measuring_settings(retrieval_min_score=NO_GATE), FakeEmbedder()) as c:
        yield c


@pytest.fixture(scope="module")
def measured(company: Company) -> dict[str, Any]:
    with security.started(company, "worst") as service:
        return security.report(
            *security.evaluate(service, default_set()), choice="worst", service=service
        )


def test_nothing_leaks_even_when_every_probe_reaches_a_model_that_repeats_everything(
    measured: dict[str, Any],
) -> None:
    assert measured["probes"] == 18
    assert measured["leaks"] == 0
    assert measured["unauthorized_retrieval"] == 0
    assert measured["unauthorized_answer"] == 0
    assert measured["existence_leaks"] == 0
    # With the gate off, every probe is answered from what the asker may read.
    assert measured["answered_from_allowed_documents"] == 18


def test_every_request_is_audited_and_none_fails(measured: dict[str, Any]) -> None:
    assert measured["requests"] == 3 * 18 + 34
    assert measured["requests_without_audit_record"] == 0
    assert measured["failed_requests"] == []


def test_the_controls_and_the_answerable_questions_get_answers(measured: dict[str, Any]) -> None:
    assert measured["controls_passed"] == 18
    assert measured["controls_passed_by_kind"] == {"forbidden": "12 of 12", "injection": "6 of 6"}
    assert measured["wrongful_refusals"] == 0
    assert measured["answerable_measured"] == 34


# --- the detector, on responses built to leak -------------------------------------------


@dataclass
class Scripted:
    """Stands in for the service: gives each path the response a test wrote."""

    company: Company
    question: Question
    responses: dict[str, security.Asked]
    asked: list[tuple[str, str]] = field(default_factory=list)

    def ask(self, path: str, name: str, question: str) -> security.Asked:
        self.asked.append((path, name))
        key = path if name == self.question.asker else f"{path} control"
        return self.responses[key]


def case_file_probe() -> Question:
    return next(q for q in default_set().questions if q.id == "emma-forbidden-who-is-on-a-plan")


def document_id(company: Company, title: str) -> int:
    found: int = company.db.scalars(select(Document.id).where(Document.title == title)).one()
    return found


def run_probe(
    company: Company,
    question: Question,
    ask: dict[str, Any] | None = None,
    search: dict[str, Any] | None = None,
    given: frozenset[int] = frozenset(),
) -> security.Probe:
    case = document_id(company, "HR Case File 2026-014")
    control = {
        "answered": True,
        "answer": "E-2291 [S1]",
        "citations": [
            {"label": "S1", "document": {"id": case, "title": "x", "version": 1}, "text": ""}
        ],
        "cut_off": False,
        "uncited": False,
    }
    scripted = Scripted(
        company,
        question,
        {
            "/ask": security.Asked(200, ask or security.NO_INFORMATION, given),
            "/search": security.Asked(200, search or {"results": [], "message": "x"}, frozenset()),
            "/ask control": security.Asked(200, control, frozenset({case})),
        },
    )
    return security.probe(scripted, question)  # type: ignore[arg-type]


def test_a_clean_refusal_is_no_leak(company: Company) -> None:
    result = run_probe(company, case_file_probe())

    assert not result.leaked
    assert result.refused
    assert result.control_passed


def test_a_secret_in_the_answer_is_a_leak(company: Company) -> None:
    result = run_probe(
        company,
        case_file_probe(),
        ask={"answered": True, "answer": "It is e-2291.", "citations": []},
    )

    assert result.unauthorized_answer
    assert result.leaked


def test_the_title_in_a_search_response_is_an_existence_leak(company: Company) -> None:
    result = run_probe(
        company, case_file_probe(), search={"results": [], "message": "See HR Case File 2026-014"}
    )

    assert result.existence_leak
    assert not result.unauthorized_answer


def test_a_citation_to_the_forbidden_document_is_a_leak(company: Company) -> None:
    case = document_id(company, "HR Case File 2026-014")
    cited = {
        "answered": True,
        "answer": "See [S1].",
        "citations": [
            {"label": "S1", "document": {"id": case, "title": "?", "version": 1}, "text": ""}
        ],
    }

    result = run_probe(company, case_file_probe(), ask=cited)

    assert result.unauthorized_answer
    assert result.existence_leak


def test_a_chunk_of_the_forbidden_document_given_to_the_model_is_a_leak(company: Company) -> None:
    case = document_id(company, "HR Case File 2026-014")

    result = run_probe(company, case_file_probe(), given=frozenset({case}))

    assert result.unauthorized_retrieval


def test_a_chunk_of_any_unreadable_document_is_a_leak(company: Company) -> None:
    budget = document_id(company, "Headcount Budget 2027")

    result = run_probe(company, case_file_probe(), given=frozenset({budget}))

    assert result.unauthorized_retrieval


def test_the_askers_own_words_are_not_a_leak(company: Company) -> None:
    grace = next(q for q in default_set().questions if q.id == "grace-forbidden-case-file")
    echoed = {"answered": True, "answer": f"You asked: {grace.question}", "citations": []}

    result = run_probe(company, grace, ask=echoed)

    assert not result.existence_leak
    assert result.answered_from_allowed


def test_a_control_that_isnt_answered_from_the_document_fails(company: Company) -> None:
    question = case_file_probe()
    scripted = Scripted(
        company,
        question,
        {
            "/ask": security.Asked(200, security.NO_INFORMATION, frozenset()),
            "/search": security.Asked(200, {"results": []}, frozenset()),
            "/ask control": security.Asked(200, security.NO_INFORMATION, frozenset()),
        },
    )

    result = security.probe(scripted, question)  # type: ignore[arg-type]

    assert not result.control_passed
    assert scripted.asked == [("/ask", "emma"), ("/search", "emma"), ("/ask", "carol")]


def test_a_control_answered_from_another_document_fails(company: Company) -> None:
    question = case_file_probe()
    leave = document_id(company, "Leave Policy")
    elsewhere = {
        "answered": True,
        "answer": "See [S1].",
        "citations": [
            {"label": "S1", "document": {"id": leave, "title": "x", "version": 1}, "text": ""}
        ],
    }
    scripted = Scripted(
        company,
        question,
        {
            "/ask": security.Asked(200, security.NO_INFORMATION, frozenset()),
            "/search": security.Asked(200, {"results": []}, frozenset()),
            "/ask control": security.Asked(200, elsewhere, frozenset({leave})),
        },
    )

    result = security.probe(scripted, question)  # type: ignore[arg-type]

    assert not result.control_passed


def test_only_the_exact_no_information_response_counts_as_refused(company: Company) -> None:
    worded = {**security.NO_INFORMATION, "answer": "I can't tell you about the HR case file."}

    result = run_probe(company, case_file_probe(), ask=worded)

    assert not result.refused
    assert not result.answered_from_allowed


def test_shown_ignores_case_and_spacing_but_not_the_asker_words() -> None:
    assert security.shown("Employee  E-2291", "e-2291", "Who?")
    assert not security.shown(
        "You asked about HR case file 2026-014",
        "HR Case File 2026-014",
        "about HR case file 2026-014",
    )


# --- failures, retries and the report ---------------------------------------------------


class FlakyModel:
    """Fails the first ``failures`` times, then repeats everything."""

    name = "flaky"
    model = "flaky"

    def __init__(self, failures: int) -> None:
        self.failures = failures

    def complete(
        self, prompt: Prompt, *, max_output_tokens: int, timeout_seconds: float
    ) -> Completion:
        if self.failures:
            self.failures -= 1
            raise LLMUnavailableError("rate limited")
        return RepeatEverything().complete(
            prompt, max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds
        )


@contextmanager
def flaky_service(company: Company, failures: int, retries: int) -> Iterator[security.Service]:
    flaky = company.settings.model_copy(update={"llm_max_retries": 0})
    with (
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(security, "provider_for", lambda choice, settings: FlakyModel(failures))
        patched = Company(
            company.db, flaky, company.embedder, company.index, company.engine, company.password
        )
        with security.started(patched, "worst", retries=retries) as service:
            yield service


def test_a_failed_request_is_retried_and_every_attempt_counted(company: Company) -> None:
    question = default_set().of_kind(Kind.ANSWERABLE)[0]

    with flaky_service(company, failures=2, retries=3) as service:
        asked = service.ask("/ask", question.asker, question.question)

    assert asked.status == 200
    assert service.requests == 3
    assert service.unaudited == 0


def test_the_chunks_of_every_attempt_are_checked_not_just_the_last(
    company: Company, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed attempt may already have sent chunks to the model.
    question = default_set().of_kind(Kind.ANSWERABLE)[0]
    given = iter([{1}, {2}])

    with flaky_service(company, failures=1, retries=1) as service:
        monkeypatch.setattr(service, "_given", lambda request_id: next(given))
        asked = service.ask("/ask", question.asker, question.question)

    assert asked.given_documents == {1, 2}


def test_a_request_with_no_audit_record_is_counted(company: Company) -> None:
    with security.started(company, "worst") as service:
        assert service._given("a-request-nobody-made") == set()

    assert (service.requests, service.unaudited) == (1, 1)


def test_a_request_that_still_fails_is_not_a_refusal(company: Company) -> None:
    question = default_set().of_kind(Kind.ANSWERABLE)[0]

    with flaky_service(company, failures=5, retries=1) as service:
        result = security.answerable(service, question)
        numbers = security.report([], [result], choice="worst", service=service)

    assert result.status == 503
    assert numbers["wrongful_refusals"] == 0
    assert numbers["answerable_measured"] == 0
    assert numbers["failed_requests"] == [question.id]
    assert service.requests == 2


def test_the_command_writes_the_report_and_fails_on_a_leak(
    company: Company,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    @contextmanager
    def same_company(settings: object) -> Iterator[Company]:
        yield company

    monkeypatch.setattr(security, "loaded_company", same_company)
    out = tmp_path / "security.json"

    assert security.main(["--output", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["leaks"] == 0
    assert "Leaks: 0 of 18 probes" in capsys.readouterr().out

    leaky = security.Probe(
        "x", "forbidden", "emma", 200, False, True, False, True, False, 200, True
    )
    monkeypatch.setattr(security, "evaluate", lambda service, questions: ([leaky], []))
    assert security.main(["--output", str(out)]) == 1


def test_groq_needs_a_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(security, "groq_key", lambda: (_ for _ in ()).throw(SystemExit("no key")))

    with pytest.raises(SystemExit, match="no key"):
        security.main(["--model", "groq", "--output", str(tmp_path / "x.json")])


def test_the_worst_model_hands_back_everything_and_cites_every_source() -> None:
    prompt = Prompt(version="v", system="rules", user="Sources and question", labels=("S1", "S2"))

    reply = RepeatEverything().complete(prompt, max_output_tokens=10, timeout_seconds=1)

    assert reply.text == "Sources and question\n[S1] [S2]"
    assert reply.stop_reason is StopReason.END
