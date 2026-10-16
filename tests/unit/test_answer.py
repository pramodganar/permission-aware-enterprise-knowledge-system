"""From a question to an answer: when the model is asked, and what the person gets back."""

import dataclasses
import logging

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.service import can_read
from app.core.config import Settings
from app.core.exceptions import InvalidInputError
from app.models import Document, User
from app.rag.answer import (
    NO_INFORMATION_MESSAGE,
    Answer,
    NoAnswerReason,
    Outcome,
    answer_question,
)
from app.rag.llm import Completion, LLMError, LLMUnavailableError, StopReason
from app.rag.prompt import NOT_IN_SOURCES, PROMPT_VERSION
from app.retrieval.filters import SearchFilters
from app.retrieval.index import build_index
from fakes import FakeEmbedder, FakeLLM, FakeReranker
from logcheck import everything_logged
from scripts.seed import seed

HOTEL = "What is the hotel limit when travelling?"
ANSWER = "The hotel limit is 160 EUR per night [S1]."
# With the fake embedder, scores mean little; this lets every question reach the model.
NO_GATE = -1.0


class Company:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings.model_copy(update={"retrieval_min_score": NO_GATE})
        self.embedder = FakeEmbedder()
        self.index = build_index(db, self.embedder)
        self.pauses: list[float] = []

    def user(self, name: str) -> User:
        return self.db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()

    def ask(
        self,
        name: str,
        question: str = HOTEL,
        llm: FakeLLM | None = None,
        **changes: object,
    ) -> Answer:
        options = {k: changes.pop(k) for k in ("filters", "reranker") if k in changes}
        return answer_question(
            self.db,
            self.index,
            self.embedder,
            llm or FakeLLM([ANSWER]),
            self.user(name),
            question,
            self.settings.model_copy(update=changes),
            sleep=self.pauses.append,
            **options,  # type: ignore[arg-type]
        )


@pytest.fixture
def acme(db: Session, settings: Settings) -> Company:
    seed(db, settings, "answer-test-password")
    return Company(db, settings)


def completion(text: str, stop: StopReason = StopReason.END) -> Completion:
    return Completion(
        text=text, model="fake-model-v2", stop_reason=stop, input_tokens=900, output_tokens=40
    )


# --- an answer --------------------------------------------------------------------------


def test_the_model_answers_from_the_context(acme: Company) -> None:
    llm = FakeLLM([completion(ANSWER)])

    answer = acme.ask("frank", llm=llm)

    assert answer.answered
    assert answer.outcome is Outcome.ANSWERED
    assert answer.text == ANSWER
    assert answer.reason is None
    assert not answer.cut_off
    assert answer.context.sources
    assert len(llm.prompts) == 1
    assert llm.prompts[0].labels == tuple(s.label for s in answer.context.sources)


def test_the_answer_records_what_produced_it(acme: Company) -> None:
    answer = acme.ask("frank", llm=FakeLLM([completion(ANSWER)]))

    assert answer.model == "fake-model-v2"
    assert answer.prompt_version == PROMPT_VERSION
    assert answer.input_tokens == 900
    assert answer.output_tokens == 40
    assert answer.user_id == acme.user("frank").id


def test_the_question_is_kept_as_it_was_searched_with(acme: Company) -> None:
    answer = acme.ask("frank", "  What is the   hotel limit\nwhen travelling?  ")

    assert answer.question == HOTEL


def test_the_settings_decide_how_much_is_retrieved(acme: Company) -> None:
    answer = acme.ask("grace", retrieval_top_k=1)

    assert len(answer.context.chunk_ids) == 1


def test_the_settings_are_passed_to_the_model(acme: Company) -> None:
    llm = FakeLLM([ANSWER])

    acme.ask("frank", llm=llm, llm_max_output_tokens=123, llm_timeout_seconds=7.0)

    assert llm.calls == [{"max_output_tokens": 123, "timeout_seconds": 7.0}]


# --- no answer --------------------------------------------------------------------------


def test_nothing_relevant_means_no_model_call(acme: Company) -> None:
    llm = FakeLLM([ANSWER])

    answer = acme.ask("frank", llm=llm, retrieval_min_score=0.99)

    assert not answer.answered
    assert answer.outcome is Outcome.NO_INFORMATION
    assert answer.reason is NoAnswerReason.NOTHING_RELEVANT
    assert answer.text == NO_INFORMATION_MESSAGE
    assert answer.context.is_empty
    assert answer.model is None
    assert answer.prompt_version is None
    assert llm.prompts == []


def test_someone_who_reads_nothing_gets_no_information_without_a_model_call(
    acme: Company,
) -> None:
    # Henry, the knowledge admin, may read no documents.
    llm = FakeLLM([ANSWER])

    answer = acme.ask("henry", llm=llm)

    assert answer.reason is NoAnswerReason.NOTHING_RELEVANT
    assert llm.prompts == []


def test_the_model_saying_not_in_sources_gives_no_information(acme: Company) -> None:
    answer = acme.ask("frank", llm=FakeLLM([NOT_IN_SOURCES]))

    assert not answer.answered
    assert answer.reason is NoAnswerReason.NOT_IN_SOURCES
    assert answer.text == NO_INFORMATION_MESSAGE
    # The model did read the context, so it stays on the answer for the audit log.
    assert answer.context.sources
    assert answer.model == "fake-model"
    assert answer.prompt_version == PROMPT_VERSION


def test_both_kinds_of_no_answer_look_the_same_to_the_person(acme: Company) -> None:
    nothing = acme.ask("frank", retrieval_min_score=0.99)
    not_in_sources = acme.ask("frank", llm=FakeLLM([NOT_IN_SOURCES]))

    assert nothing.text == not_in_sources.text == NO_INFORMATION_MESSAGE
    assert nothing.outcome is not_in_sources.outcome


@pytest.mark.parametrize(
    "reply",
    [
        "NOT_IN_SOURCES.",
        "not_in_sources",
        "NOT IN SOURCES",
        "Not in sources.",
        "**NOT_IN_SOURCES**",
        "`NOT_IN_SOURCES`",
        "\nNOT_IN_SOURCES\n",
        # Says the sources don't answer it, then answers anyway: from somewhere else.
        "NOT_IN_SOURCES. But the hotel limit is usually about 150 EUR.",
        "The hotel limit is usually 150 EUR. NOT_IN_SOURCES",
    ],
)
def test_the_marker_counts_however_it_is_written_and_wherever_it_is(
    acme: Company, reply: str
) -> None:
    answer = acme.ask("frank", llm=FakeLLM([reply]))

    assert answer.reason is NoAnswerReason.NOT_IN_SOURCES
    assert answer.text == NO_INFORMATION_MESSAGE


@pytest.mark.parametrize(
    "reply",
    [
        "The sources say the hotel limit is 160 EUR [S1].",
        "It is 160 EUR; this is not in the meal section [S1].",
        "NOT_IN_SOURCE_LIST is not a word here, 160 EUR [S1].",
    ],
)
def test_an_answer_that_only_mentions_sources_is_an_answer(acme: Company, reply: str) -> None:
    answer = acme.ask("frank", llm=FakeLLM([reply]))

    assert answer.answered
    assert answer.text == reply


def test_a_refusal_by_the_provider_gives_no_information(acme: Company) -> None:
    answer = acme.ask("frank", llm=FakeLLM([completion("I can't help.", StopReason.REFUSED)]))

    assert answer.reason is NoAnswerReason.REFUSED_BY_PROVIDER
    assert answer.text == NO_INFORMATION_MESSAGE
    assert not answer.cut_off


# --- cut off ----------------------------------------------------------------------------


def test_an_answer_cut_off_at_the_cap_is_returned_marked(acme: Company) -> None:
    text = "The hotel limit is 160 EUR [S1]. In high-cost cities the"

    answer = acme.ask("frank", llm=FakeLLM([completion(text, StopReason.MAX_TOKENS)]))

    assert answer.answered
    assert answer.cut_off
    assert answer.text == text


def test_a_cut_off_reply_with_the_marker_is_no_information_not_cut_off(acme: Company) -> None:
    answer = acme.ask(
        "frank", llm=FakeLLM([completion("NOT_IN_SOURCES but", StopReason.MAX_TOKENS)])
    )

    assert answer.reason is NoAnswerReason.NOT_IN_SOURCES
    assert not answer.cut_off


@pytest.mark.parametrize("stop", [StopReason.END, StopReason.OTHER])
def test_only_the_length_cap_marks_an_answer_cut_off(acme: Company, stop: StopReason) -> None:
    answer = acme.ask("frank", llm=FakeLLM([completion(ANSWER, stop)]))

    assert answer.answered
    assert not answer.cut_off


# --- failures ---------------------------------------------------------------------------


def test_a_provider_that_stays_down_fails_the_question(acme: Company) -> None:
    llm = FakeLLM([LLMUnavailableError("down")])

    with pytest.raises(LLMUnavailableError):
        acme.ask("frank", llm=llm)

    # Retried as the settings say, pausing through the given sleep.
    assert len(llm.prompts) == acme.settings.llm_max_retries + 1
    assert acme.pauses == [0.5, 1.0]


def test_a_provider_that_recovers_gives_the_answer(acme: Company) -> None:
    answer = acme.ask("frank", llm=FakeLLM([LLMUnavailableError("busy"), ANSWER]))

    assert answer.text == ANSWER


def test_an_empty_reply_fails_the_question(acme: Company) -> None:
    with pytest.raises(LLMError):
        acme.ask("frank", llm=FakeLLM([completion("   ", StopReason.MAX_TOKENS)]))


def test_a_question_that_can_not_be_searched_never_reaches_the_model(acme: Company) -> None:
    llm = FakeLLM([ANSWER])

    with pytest.raises(InvalidInputError):
        acme.ask("frank", "?!", llm=llm)

    assert llm.prompts == []


# --- what the model is given ------------------------------------------------------------

QUESTIONS = [
    HOTEL,
    "How are executive bonuses calculated?",
    "Which employees are on a performance improvement plan?",
    "What acquisitions is the company considering?",
    "What are the salary bands for senior engineers?",
]


@pytest.mark.parametrize("name", ["alice", "bob", "carol", "david", "emma", "frank", "grace"])
def test_the_model_only_ever_sees_what_the_person_may_read(acme: Company, name: str) -> None:
    user = acme.user(name)
    readable = [d for d in acme.db.scalars(select(Document)) if can_read(user, d)]
    unreadable = [d for d in acme.db.scalars(select(Document)) if not can_read(user, d)]
    llm = FakeLLM([ANSWER])

    for question in QUESTIONS:
        answer = acme.ask(name, question, llm=llm)

        for source in answer.context.sources:
            assert any(source.text in d.content for d in readable)

    sent = "\n".join(p.system + p.user for p in llm.prompts)
    assert len(llm.prompts) == len(QUESTIONS)
    for document in unreadable:
        assert f'document="{document.title}"' not in sent
    # Nothing about the person goes to the model.
    assert user.email not in sent
    assert name.capitalize() not in sent


def test_filters_narrow_what_the_model_is_given(acme: Company) -> None:
    travel = acme.db.scalars(
        select(Document).where(Document.title == "Travel and Expense Policy")
    ).one()

    answer = acme.ask("grace", filters=SearchFilters(document_ids=[travel.id]))

    assert {s.document_title for s in answer.context.sources} == {"Travel and Expense Policy"}


def test_a_reranker_is_used_when_given(acme: Company) -> None:
    reranker = FakeReranker()

    acme.ask("frank", reranker=reranker)

    assert reranker.calls


# --- logs -------------------------------------------------------------------------------


def logged_answer(
    acme: Company,
    caplog: pytest.LogCaptureFixture,
    llm: FakeLLM | None = None,
    **changes: object,
) -> Answer:
    with caplog.at_level(logging.INFO):
        return acme.ask(
            "carol", "Which employees are on a performance improvement plan?", llm, **changes
        )


def test_an_answer_is_logged_with_counts_and_timings(
    acme: Company, caplog: pytest.LogCaptureFixture
) -> None:
    answer = logged_answer(acme, caplog, llm=FakeLLM(["Employee E-2291 is on a plan [S1]."]))

    record = next(r for r in caplog.records if r.message == "question answered")
    assert record.outcome == "answered"  # type: ignore[attr-defined]
    assert record.reason is None  # type: ignore[attr-defined]
    assert record.sources == len(answer.context.sources)  # type: ignore[attr-defined]
    assert record.cut_off is False  # type: ignore[attr-defined]
    assert record.retrieval_milliseconds >= 0  # type: ignore[attr-defined]
    assert record.generation_milliseconds >= 0  # type: ignore[attr-defined]


def test_no_answer_is_logged_with_why(acme: Company, caplog: pytest.LogCaptureFixture) -> None:
    logged_answer(acme, caplog, retrieval_min_score=0.99)

    record = next(r for r in caplog.records if r.message == "question answered")
    assert record.outcome == "no_information"  # type: ignore[attr-defined]
    assert record.reason == "nothing_relevant"  # type: ignore[attr-defined]
    # The model wasn't asked, so there is no time for it.
    assert record.generation_milliseconds is None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "reply", ["Employee E-2291 is on a plan [S1].", NOT_IN_SOURCES], ids=["answer", "marker"]
)
def test_the_logs_hold_no_question_answer_or_document_text(
    acme: Company, caplog: pytest.LogCaptureFixture, reply: str
) -> None:
    answer = logged_answer(acme, caplog, llm=FakeLLM([reply]))

    logged = everything_logged(caplog)
    assert "performance improvement plan" not in logged.lower()
    assert "E-2291" not in logged
    for source in answer.context.sources:
        assert source.document_title not in logged
        assert source.text[:40] not in logged


def test_an_answer_can_not_be_changed(acme: Company) -> None:
    answer = acme.ask("frank")

    with pytest.raises(dataclasses.FrozenInstanceError):
        answer.text = "something else"  # type: ignore[misc]
