"""The HTTP service's skeleton: startup and shutdown, request IDs, errors and health."""

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api import components as components_module
from app.api.components import Components
from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.main import create_app
from app.retrieval import reranking
from app.retrieval.index import build_index
from app.retrieval.index_versions import publish_index
from fakes import FakeEmbedder, FakeLLM, FakeReranker
from logcheck import everything_logged

HEX_ID = re.compile(r"[0-9a-f]{32}")
SECRET = "Senior Engineer 68,000 to 92,000"


class ClosingLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class Login(BaseModel):
    email: str
    password: str


def with_test_routes(app: FastAPI) -> FastAPI:
    # Routes that fail in each way an endpoint can, to see what a client gets.
    @app.post("/test/body")
    def body(login: Login) -> dict[str, str]:
        return {"email": login.email}

    @app.get("/test/not-found")
    def not_found() -> None:
        raise NotFoundError("document 16 is restricted")

    @app.get("/test/crash")
    def crash() -> None:
        raise RuntimeError(f"library failed while reading: {SECRET}")

    return app


@pytest.fixture
def components(db: Session) -> Components:
    embedder = FakeEmbedder()
    return Components(embedder=embedder, index=build_index(db, embedder), provider=ClosingLLM())


@pytest.fixture
def client(settings: Settings, engine: Engine, components: Components) -> Iterator[TestClient]:
    app = with_test_routes(create_app(settings, engine=engine, components=components))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# --- health -----------------------------------------------------------------------------


def test_health_says_only_that_the_service_is_up(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_needs_no_token(client: TestClient) -> None:
    assert client.get("/health", headers={"Authorization": "Bearer nonsense"}).status_code == 200


def break_the_database(client: TestClient) -> None:
    class Broken:
        def __enter__(self) -> "Broken":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def execute(self, *_: object) -> None:
            raise OSError("disk gone")

    client.app.state.session_factory = Broken  # type: ignore[attr-defined]


def test_health_fails_when_the_database_does(client: TestClient) -> None:
    break_the_database(client)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "disk" not in response.text


def handled(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.message == "request handled"]


def test_a_passing_health_check_is_logged_only_at_debug(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # Docker asks every 30 seconds; at INFO that would be most of the log.
    with caplog.at_level(logging.DEBUG):
        client.get("/health")

    (record,) = handled(caplog)
    assert record.levelno == logging.DEBUG
    assert record.status == 200  # type: ignore[attr-defined]


def test_a_failing_health_check_is_logged_like_any_request(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    break_the_database(client)

    with caplog.at_level(logging.INFO):
        client.get("/health")

    (record,) = handled(caplog)
    assert record.levelno == logging.INFO
    assert record.status == 503  # type: ignore[attr-defined]


# /docs answers 200, like a passing health check, on another route.
@pytest.mark.parametrize("path", ["/docs", "/test/not-found", "/auth/me", "/health/extra"])
def test_every_other_request_is_still_logged_at_info(
    client: TestClient, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    with caplog.at_level(logging.INFO):
        client.get(path)

    (record,) = handled(caplog)
    assert record.levelno == logging.INFO


# --- request IDs ------------------------------------------------------------------------


def test_every_response_has_a_request_id(client: TestClient) -> None:
    first = client.get("/health").headers["X-Request-ID"]
    second = client.get("/health").headers["X-Request-ID"]

    assert HEX_ID.fullmatch(first)
    assert first != second


def test_a_safe_request_id_from_the_client_is_kept(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-42-abc"})

    assert response.headers["X-Request-ID"] == "trace-42-abc"


@pytest.mark.parametrize(
    "sent",
    [
        "x" * 65,
        "has space",
        "line\tbreak",
        '{"level": "ERROR"}',
        "../../etc/passwd",
        "",
    ],
)
def test_an_unsafe_request_id_is_replaced(client: TestClient, sent: str) -> None:
    response = client.get("/health", headers={"X-Request-ID": sent})

    assert HEX_ID.fullmatch(response.headers["X-Request-ID"])


def test_an_error_body_carries_the_request_id(client: TestClient) -> None:
    response = client.get("/test/not-found", headers={"X-Request-ID": "abc-1"})

    assert response.json()["error"]["request_id"] == "abc-1"
    assert response.headers["X-Request-ID"] == "abc-1"


def test_each_request_is_logged_without_its_query(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # DEBUG, where a passing health check is logged.
    with caplog.at_level(logging.DEBUG):
        response = client.get("/health?question=salary+bands+for+carol")

    record = next(r for r in caplog.records if r.message == "request handled")
    assert record.method == "GET"  # type: ignore[attr-defined]
    assert record.path == "/health"  # type: ignore[attr-defined]
    assert record.status == 200  # type: ignore[attr-defined]
    assert record.milliseconds >= 0  # type: ignore[attr-defined]
    # The service's own logs. The test client logs the URL it sent, but that
    # is the client's side.
    service = [r for r in caplog.records if r.name.startswith("app.")]
    assert all("salary" not in str(vars(r)) for r in service)
    assert response.status_code == 200


def test_the_log_shows_the_route_pattern_not_the_ids_in_it(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    def one(document_id: int) -> dict[str, int]:
        return {"id": document_id}

    app: FastAPI = client.app  # type: ignore[assignment]
    app.add_api_route("/test/documents/{document_id}", one)

    with caplog.at_level(logging.INFO):
        client.get("/test/documents/16")

    record = next(r for r in caplog.records if r.message == "request handled")
    assert record.path == "/test/documents/{document_id}"  # type: ignore[attr-defined]


def test_a_path_that_matches_no_route_is_not_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        client.get('/what-is-carols-salary"}{"level": "ERROR"')

    record = next(r for r in caplog.records if r.message == "request handled")
    assert record.path == "(no matching route)"  # type: ignore[attr-defined]
    service = [r for r in caplog.records if r.name.startswith("app.")]
    assert all("carol" not in str(vars(r)) for r in service)


# --- errors -----------------------------------------------------------------------------


def test_the_service_s_own_errors_keep_their_fixed_message(client: TestClient) -> None:
    response = client.get("/test/not-found")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["message"] == "Not found."
    # Not the detail, which names the document. (Not the whole body either:
    # the random request ID can contain "16".)
    assert "restricted" not in response.text


def test_a_bad_body_names_the_fields_and_never_echoes_them(client: TestClient) -> None:
    response = client.post("/test/body", json={"email": 42, "password": ["hunter2-secret"]})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_input"
    assert error["fields"] == ["body.email", "body.password"]
    assert "hunter2" not in response.text
    # Everything but the random request ID, which could contain "42".
    del error["request_id"]
    assert "42" not in str(error)


def test_a_body_that_is_not_json_is_refused_the_same_way(client: TestClient) -> None:
    response = client.post(
        "/test/body", content=b"password=hunter2", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"
    assert "hunter2" not in response.text


def test_an_unknown_route_is_a_plain_not_found(client: TestClient) -> None:
    response = client.get("/admin/secret-report")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "request_id" in response.json()["error"]


def test_the_wrong_method_is_refused(client: TestClient) -> None:
    response = client.delete("/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_an_unexpected_error_is_a_fixed_500_with_its_request_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        response = client.get("/test/crash", headers={"X-Request-ID": "crash-1"})

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Something went wrong.",
            "request_id": "crash-1",
        }
    }
    assert response.headers["X-Request-ID"] == "crash-1"
    # The log says what failed and where, not the message, which here holds document text.
    record = next(r for r in caplog.records if r.message == "unexpected error")
    assert record.error == "RuntimeError"  # type: ignore[attr-defined]
    assert "crash" in record.where  # type: ignore[attr-defined]
    assert SECRET not in everything_logged(caplog)
    assert SECRET not in response.text


# --- startup and shutdown ---------------------------------------------------------------


def test_shutdown_closes_the_provider(
    settings: Settings, engine: Engine, components: Components
) -> None:
    with TestClient(create_app(settings, engine=engine, components=components)):
        pass

    assert isinstance(components.provider, ClosingLLM)
    assert components.provider.closed == 1


def test_the_interactive_docs_are_off_in_production(
    settings: Settings, engine: Engine, components: Components
) -> None:
    production = settings.model_copy(update={"environment": "production"})

    with TestClient(create_app(production, engine=engine, components=components)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_the_interactive_docs_are_on_elsewhere(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200


@pytest.fixture
def fake_model_settings(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    # Startup loads the real model by name; here it loads the fake one.
    monkeypatch.setattr(components_module, "load_embedder", lambda _: FakeEmbedder())
    return settings.model_copy(
        update={"embedding_model": "fake-embedder", "index_directory": tmp_path / "index"}
    )


def test_startup_loads_the_current_index(
    fake_model_settings: Settings, engine: Engine, db: Session, caplog: pytest.LogCaptureFixture
) -> None:
    publish_index(db, FakeEmbedder(), fake_model_settings, fake_model_settings.index_directory)

    with (
        caplog.at_level(logging.INFO),
        TestClient(create_app(fake_model_settings, engine=engine)) as client,
    ):
        loaded = client.app.state.components  # type: ignore[attr-defined]
        assert client.get("/health").status_code == 200

    assert loaded.provider is None
    assert loaded.reranker is None
    assert not any("behind the database" in r.message for r in caplog.records)


def test_startup_loads_the_reranker_the_settings_name(
    fake_model_settings: Settings, engine: Engine, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_index(db, FakeEmbedder(), fake_model_settings, fake_model_settings.index_directory)
    names: list[str] = []

    def load(name: str, *, offline: bool) -> FakeReranker:
        names.append(name)
        return FakeReranker()

    monkeypatch.setattr(reranking, "CrossEncoderReranker", load)
    with_reranker = fake_model_settings.model_copy(update={"reranker_model": "some-cross-encoder"})

    with TestClient(create_app(with_reranker, engine=engine)) as client:
        loaded = client.app.state.components  # type: ignore[attr-defined]

    assert isinstance(loaded.reranker, FakeReranker)
    assert names == ["some-cross-encoder"]


def test_startup_refuses_without_an_index(fake_model_settings: Settings, engine: Engine) -> None:
    with (
        pytest.raises(Exception, match="index"),
        TestClient(create_app(fake_model_settings, engine=engine)),
    ):
        pass


def test_startup_refuses_an_index_built_with_another_model(
    fake_model_settings: Settings, engine: Engine, db: Session
) -> None:
    publish_index(db, FakeEmbedder(), fake_model_settings, fake_model_settings.index_directory)
    other_model = fake_model_settings.model_copy(update={"embedding_model": "another-model"})

    with (
        pytest.raises(Exception, match="another-model"),
        TestClient(create_app(other_model, engine=engine)),
    ):
        pass


def test_startup_warns_when_the_index_is_behind_the_database(
    fake_model_settings: Settings,
    engine: Engine,
    db: Session,
    company: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The index is built before the company's documents exist.
    publish_index(db, FakeEmbedder(), fake_model_settings, fake_model_settings.index_directory)
    from scripts.seed import seed

    seed(db, fake_model_settings, "startup-test-password")

    with (
        caplog.at_level(logging.WARNING),
        TestClient(create_app(fake_model_settings, engine=engine)),
    ):
        pass

    assert any("behind the database" in r.message for r in caplog.records)


def test_an_engine_the_app_made_is_disposed_at_shutdown(
    settings: Settings, components: Components, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import main

    disposed: list[bool] = []
    from app.core.database import create_db_engine as real

    def recording(url: str) -> Engine:
        engine = real(url)
        monkeypatch.setattr(engine, "dispose", lambda: disposed.append(True))
        return engine

    monkeypatch.setattr(main, "create_db_engine", recording)

    with TestClient(main.create_app(settings, components=components)):
        assert disposed == []

    assert disposed == [True]


def test_an_engine_given_to_the_app_is_left_open(
    settings: Settings, engine: Engine, components: Components
) -> None:
    with TestClient(create_app(settings, engine=engine, components=components)):
        pass

    # Still usable: whoever made it disposes of it.
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar() == 1


def test_serve_sets_up_logging_and_builds_the_app(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import main

    levels: list[str] = []
    quiet = settings.model_copy(update={"log_level": "WARNING"})
    monkeypatch.setattr(main, "get_settings", lambda: quiet)
    monkeypatch.setattr(main, "configure_logging", levels.append)

    app = main.serve()

    assert isinstance(app, FastAPI)
    assert levels == ["WARNING"]
