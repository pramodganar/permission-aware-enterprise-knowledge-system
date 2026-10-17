"""The demo behind the Streamlit page, with the fake embedder and model."""

import threading

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.rag.answer import NO_INFORMATION_MESSAGE
from fakes import FakeEmbedder, FakeLLM
from questions import MAY_READ
from ui import demo as demo_module
from ui.demo import Demo, build_demo

HOTEL = "What is the hotel limit when travelling?"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM(["The hotel limit is 160 EUR [S1]."])


@pytest.fixture
def demo(settings: Settings, llm: FakeLLM) -> Demo:
    return build_demo(settings, llm, FakeEmbedder())


def test_the_whole_company_is_loaded(demo: Demo) -> None:
    people = {person.key: person for person in demo.people()}

    assert set(people) == set(MAY_READ)
    for key, person in people.items():
        assert set(person.reads) == MAY_READ[key], key


def test_a_person_is_described_by_their_department_and_roles(demo: Demo) -> None:
    grace = next(p for p in demo.people() if p.key == "grace")

    assert grace.department == "Executive"
    assert "executive" in grace.roles
    assert list(grace.reads) == sorted(grace.reads)


def test_asking_goes_through_the_real_answer_path(demo: Demo, llm: FakeLLM) -> None:
    answer = demo.ask("frank", HOTEL)

    assert answer.answered
    assert answer.citations
    assert {s.document_title for s in answer.context.sources} <= MAY_READ["frank"]
    assert len(llm.prompts) == 1


def test_people_who_read_nothing_get_no_information(demo: Demo, llm: FakeLLM) -> None:
    answer = demo.ask("henry", HOTEL)

    assert answer.text == NO_INFORMATION_MESSAGE
    assert llm.prompts == []


def test_the_same_question_from_the_same_person_is_answered_once(demo: Demo, llm: FakeLLM) -> None:
    first = demo.ask("frank", HOTEL)
    again = demo.ask("frank", f"  {HOTEL} ")

    assert again is first
    assert len(llm.prompts) == 1


def test_a_kept_answer_is_never_given_to_someone_else(demo: Demo, llm: FakeLLM) -> None:
    frank = demo.ask("frank", HOTEL)
    grace = demo.ask("grace", HOTEL)

    assert grace is not frank
    assert grace.user_id != frank.user_id
    assert len(llm.prompts) == 2


def test_the_oldest_kept_answer_is_dropped_first(
    demo: Demo, llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo_module, "MAX_KEPT_ANSWERS", 2)

    demo.ask("frank", HOTEL)
    demo.ask("alice", HOTEL)
    demo.ask("frank", HOTEL)  # kept, and now the newest
    demo.ask("bob", HOTEL)  # pushes out alice
    demo.ask("frank", HOTEL)
    demo.ask("alice", HOTEL)

    assert len(llm.prompts) == 4


def test_without_a_model_there_are_passages_but_no_answers(settings: Settings) -> None:
    demo = build_demo(settings, None, FakeEmbedder())

    assert not demo.answers_on
    with pytest.raises(RuntimeError):
        demo.ask("frank", HOTEL)
    context = demo.search("frank", HOTEL)
    assert context.sources
    assert {s.document_title for s in context.sources} <= MAY_READ["frank"]


def test_many_visitors_at_once(demo: Demo) -> None:
    errors: list[BaseException] = []

    def visit(key: str) -> None:
        try:
            for n in range(3):
                answer = demo.ask(key, f"{HOTEL} {n}")
                assert {s.document_title for s in answer.context.sources} <= MAY_READ[key]
                demo.people()
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=visit, args=(key,)) for key in MAY_READ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
