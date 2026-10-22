"""Scoring answers by rule: what counts as citing the evidence, and as abstaining."""

from typing import Any

from app.rag.answer import NO_INFORMATION_MESSAGE
from evaluation.answers import Generated, GivenSource, score
from evaluation.dataset import Kind, Question, QuestionSet

QUESTIONS = QuestionSet(
    version=1,
    questions=(
        Question(
            id="leave",
            kind=Kind.ANSWERABLE,
            asker="alice",
            question="How much leave?",
            document="Leave Policy",
            evidence=("25 days of paid annual leave",),
            reference="25 days.",
        ),
        Question(
            id="moving",
            kind=Kind.RELATED,
            asker="alice",
            question="Leave for moving house?",
            document="Leave Policy",
            note="Not in the policy.",
        ),
        Question(id="capital", kind=Kind.OFF_TOPIC, asker="grace", question="Capital of France?"),
    ),
)

EVIDENCE = GivenSource(
    label="S1",
    document="Leave Policy",
    chunk_ids=(1,),
    text="Full-time employees get 25 days\nof paid annual leave a year.",
)
NEARBY = GivenSource(label="S2", document="Leave Policy", chunk_ids=(2,), text="Sick leave.")
# The same words, in a document that isn't the question's.
ELSEWHERE = GivenSource(
    label="S3", document="Travel Policy", chunk_ids=(9,), text="25 days of paid annual leave"
)


def answer(question_id: str, **changes: Any) -> Generated:
    question = next(q for q in QUESTIONS.questions if q.id == question_id)
    values: dict[str, Any] = {
        "id": question.id,
        "kind": question.kind,
        "asker": question.asker,
        "question": question.question,
        "status": "answered",
        "text": "25 days [S1].",
        "cited": ("S1",),
        "sources": (EVIDENCE, NEARBY, ELSEWHERE),
    }
    return Generated(**(values | changes))


def abstained(question_id: str, reason: str = "not_in_sources") -> Generated:
    return answer(
        question_id, status="no_information", reason=reason, text=NO_INFORMATION_MESSAGE, cited=()
    )


def answerable(*answers: Generated) -> dict[str, Any]:
    found: dict[str, Any] = score(answers, QUESTIONS)["answerable"]
    return found


def test_citing_the_source_with_the_quote_is_citing_the_evidence() -> None:
    found = answerable(answer("leave"))
    assert found["given_the_evidence"] == 1
    assert found["answered_citing_the_evidence"] == 1
    assert found["answered_citing_its_document"] == 1
    assert found["answered_without_citing_the_evidence"] == []


def test_citing_another_part_of_the_right_document_is_not_citing_the_evidence() -> None:
    found = answerable(answer("leave", text="Sick leave [S2].", cited=("S2",)))
    assert found["answered_citing_the_evidence"] == 0
    assert found["answered_citing_its_document"] == 1
    assert found["answered_without_citing_the_evidence"] == ["leave"]


def test_the_quote_in_another_document_is_not_the_evidence() -> None:
    found = answerable(
        answer("leave", text="25 days [S3].", cited=("S3",), sources=(NEARBY, ELSEWHERE))
    )
    assert found["given_the_evidence"] == 0
    assert found["answered_citing_the_evidence"] == 0
    assert found["answered_citing_its_document"] == 0
    assert (found["citations"], found["citations_to_its_document"]) == (1, 0)


def test_an_uncited_cut_off_answer_with_made_up_citations_is_counted_each_way() -> None:
    found = answerable(
        answer("leave", text="25 days.", cited=(), cut_off=True, unknown_citations=2)
    )
    assert (found["uncited"], found["cut_off"], found["with_made_up_citations"]) == (1, 1, 1)
    assert found["answered_citing_its_document"] == 0


def test_a_failed_question_is_listed_and_measures_nothing() -> None:
    gone = answer("leave", status="failed", reason="LLMUnavailableError", text="", cited=())
    numbers = score([gone, answer("moving", status="failed", text="", cited=())], QUESTIONS)
    assert numbers["failed"] == ["leave", "moving"]
    assert numbers["answerable"]["measured"] == 0
    assert numbers["answerable"]["not_answered"] == []
    assert numbers["abstention"]["related"] == {
        "measured": 0,
        "abstained": 0,
        "by_the_gate": 0,
        "by_the_model": 0,
        "answered": [],
    }


def test_an_answerable_question_without_an_answer_is_listed() -> None:
    found = answerable(abstained("leave"))
    assert (found["measured"], found["answered"]) == (1, 0)
    assert found["not_answered"] == ["leave"]
    assert found["given_the_evidence"] == 1


def test_abstaining_is_told_apart_by_who_abstained() -> None:
    numbers = score(
        [abstained("moving", "not_in_sources"), abstained("capital", "nothing_relevant")],
        QUESTIONS,
    )["abstention"]
    assert numbers["related"] | {"answered": None} == {
        "measured": 1,
        "abstained": 1,
        "by_the_gate": 0,
        "by_the_model": 1,
        "answered": None,
    }
    assert (numbers["off_topic"]["by_the_gate"], numbers["off_topic"]["by_the_model"]) == (1, 0)


def test_answering_a_related_question_is_not_abstaining() -> None:
    numbers = score([answer("moving", text="Two days [S2].", cited=("S2",))], QUESTIONS)
    assert numbers["abstention"]["related"]["abstained"] == 0
    assert numbers["abstention"]["related"]["answered"] == ["moving"]


def test_only_exactly_the_no_information_response_is_abstaining() -> None:
    worded = answer("moving", status="no_information", text="I don't know.", cited=())
    with_a_source = abstained("capital").model_copy(update={"cited": ("S1",)})
    numbers = score([worded, with_a_source], QUESTIONS)["abstention"]
    assert numbers["related"]["answered"] == ["moving"]
    assert numbers["off_topic"]["answered"] == ["capital"]


def test_the_digest_names_the_text_of_the_answer() -> None:
    first = answer("leave")
    assert first.digest == answer("leave", cited=()).digest
    assert first.digest != answer("leave", text="25 days [S1]. ").digest
    assert len(first.digest) == 16
