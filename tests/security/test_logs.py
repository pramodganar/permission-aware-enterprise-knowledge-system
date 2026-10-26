"""What the service writes to its log, read the way an operator would (NFR-9, threat T21).

The tests of each route look at the records the service's own code makes.
This one looks at the output: every line, from every logger, third-party
libraries included, at DEBUG, the most anyone can switch on. The service is
used the way people use it, failures included, and the log must hold no
piece of any document, no question, no password and no token.

The failures are the likely place for text to get through, so two are
made to carry it: a model that fails with the prompt in its error message,
and a reranker that crashes with the passages it was given in its own. The
first is an error the service expects, the second one it doesn't. Neither
message may reach the log.
"""

from collections.abc import Callable, Iterator, Sequence

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from apiclient import PASSWORD, company_client, email, token
from app.core.config import Settings
from app.core.logging_setup import configure_logging
from app.models import Chunk
from app.rag.llm import LLMUnavailableError
from app.rag.prompt import Prompt
from fakes import FakeLLM, FakeReranker, Reply
from files import make_docx, make_pdf
from logcheck import logging_restored

UPLOADS = {
    "booth.pdf": make_pdf(["Booth crates for the spring fair ship from dock four on Tuesdays."]),
    "stands.docx": make_docx(["Exhibition stands are booked through the events desk."]),
    "badges.txt": b"Visitor badges are printed at reception and returned on leaving.",
}
QUESTIONS = [
    "What are the salary bands for senior engineers?",
    "Which employees are on a performance improvement plan?",
    "What acquisitions is the company considering?",
]
FAILS = "Why did the payments outage happen?"
BREAKS = "What is the hotel limit when travelling?"
WRONG_PASSWORD = "not-the-password-at-all"
PIECE = 40


def reply(prompt: Prompt) -> Reply:
    if FAILS in prompt.user:
        return LLMUnavailableError(f"provider down while reading: {prompt.user}")
    return f"{prompt.user} [S1] [S2]"


class BreakingReranker(FakeReranker):
    def score(self, question: str, passages: Sequence[str]) -> list[float]:
        if question == BREAKS:
            raise RuntimeError(f"could not score {question!r} against {list(passages)!r}")
        return super().score(question, passages)


@pytest.fixture
def written(capsys: pytest.CaptureFixture[str]) -> Iterator[Callable[[], str]]:
    """Log as the service does when it runs, at DEBUG, and give back what it wrote.

    Logging is set up when the test starts, not here: the output pytest
    captures during setup is closed by then.
    """
    with logging_restored():
        yield lambda: capsys.readouterr().out


def pieces(text: str) -> list[str]:
    return [text[n : n + PIECE] for n in range(0, len(text) - PIECE + 1, PIECE // 2)]


def test_nothing_anyone_typed_or_read_is_in_the_log_even_at_debug(
    db: Session, engine: Engine, settings: Settings, written: Callable[[], str]
) -> None:
    changed = settings.model_copy(
        update={"retrieval_min_score": -1.0, "llm_max_output_tokens": 4000, "llm_max_retries": 0}
    )
    configure_logging("DEBUG")
    tokens = []
    with company_client(db, engine, changed, FakeLLM(reply), BreakingReranker()) as client:
        for name in ("carol", "emma", "grace", "david"):
            tokens.append(token(client, name))
            auth = {"Authorization": f"Bearer {tokens[-1]}"}
            client.get("/documents", headers=auth)
            for n in range(1, 20):
                client.get(f"/documents/{n}", headers=auth)
            for question in QUESTIONS:
                client.post("/search", json={"question": question}, headers=auth)
                client.post("/ask", json={"question": question}, headers=auth)
                narrowed = {"question": question, "filters": {"document_ids": [16, 17]}}
                client.post("/ask", json=narrowed, headers=auth)
        auth = {"Authorization": f"Bearer {tokens[-1]}"}
        assert client.post("/ask", json={"question": FAILS}, headers=auth).status_code == 503
        assert client.post("/ask", json={"question": BREAKS}, headers=auth).status_code == 500
        client.post("/auth/login", json={"email": email("david"), "password": WRONG_PASSWORD})

        tokens.append(token(client, "henry"))
        admin = {"Authorization": f"Bearer {tokens[-1]}"}
        # Seeding read the company's own files; these come through the upload route.
        for name, data in UPLOADS.items():
            response = client.post(
                "/admin/documents",
                headers=admin,
                files={"file": (name, data)},
                data={"title": f"Uploaded {name}", "department": "sales"}
                | {"classification": "general"},
            )
            assert response.status_code == 201, response.text
        tokens.append(token(client, "irene"))
        client.get("/audit", headers={"Authorization": f"Bearer {tokens[-1]}"})

        texts = list(db.scalars(select(Chunk.text)))

    log = written()
    # Something was written: the requests, an ingestion, and the unexpected error.
    assert '"logger": "app.api.middleware"' in log
    assert '"logger": "app.ingestion.pipeline"' in log
    assert '"message": "unexpected error"' in log
    assert texts

    found = [piece for text in texts for piece in pieces(text) if piece in log]
    assert found == []
    typed = [*QUESTIONS, FAILS, BREAKS, PASSWORD, WRONG_PASSWORD, *tokens]
    assert [value for value in typed if value in log] == []
    assert settings.jwt_secret_key.get_secret_value() not in log
