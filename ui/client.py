"""The UI's way to the service: calls to the HTTP API, and nothing else.

The UI is a separate process and holds no rules of its own (docs/architecture.md).
It never imports the service's code, reads the database or loads the index;
everything it shows came from an API response, decided by the service for
the token it sent. Hiding a page from someone in the UI is a convenience,
never the check.

    api = Api("http://127.0.0.1:8000")
    login = api.login("david@acmecorp.example", password)
    answer = api.ask(login.token, "How many days of annual leave do I get?")

The response models here mirror the API's. They are written out again,
rather than imported, for the same reason; a contract test checks the two
stay in step (tests/unit/test_ui_client.py). Fields the API adds later are
ignored, so the UI keeps working with a newer service.

Every failure is an ``ApiError`` with the service's code and message, and
the request ID to quote. A 401 is ``NotSignedIn``: the token is missing,
expired, or its account was deactivated, and the person has to sign in again.

The token is only ever sent in the Authorization header. It is not logged,
not put in a URL, and not kept anywhere but where the caller keeps it.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

DEFAULT_API_URL = "http://127.0.0.1:8000"
# A question waits on the language model; everything else should be quick.
TIMEOUT_SECONDS = 30.0
ASK_TIMEOUT_SECONDS = 360.0


# --- what the API returns ---------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Department(_Model):
    code: str
    name: str


class Me(_Model):
    id: int
    email: str
    full_name: str
    department: Department
    roles: list[str]
    permissions: list[str]

    def can(self, permission: str) -> bool:
        return permission in self.permissions


class DocumentSummary(_Model):
    id: UUID
    title: str
    department: Department
    classification: str
    file_type: str
    version: int
    updated_at: datetime


class DocumentList(_Model):
    total: int
    documents: list[DocumentSummary]


class DocumentDetail(DocumentSummary):
    source_filename: str
    size_bytes: int
    created_at: datetime
    content: str


class DocumentRef(_Model):
    id: UUID
    title: str
    version: int


class Passage(_Model):
    document: DocumentRef
    start_offset: int
    end_offset: int
    text: str
    score: float


class SearchResult(_Model):
    results: list[Passage]
    message: str | None = None


class Citation(_Model):
    label: str
    document: DocumentRef
    text: str


class Answer(_Model):
    answered: bool
    answer: str
    citations: list[Citation]
    cut_off: bool
    uncited: bool


class Grant(_Model):
    email: str
    expires_at: datetime | None


class ManagedDocument(_Model):
    id: UUID
    version: int
    status: str
    department: str
    classification: str
    file_type: str
    size_bytes: int
    chunks: int
    supersedes_id: UUID | None
    grants: list[Grant]
    in_index: bool


class ManagedList(_Model):
    total: int
    documents: list[ManagedDocument]


class Actor(_Model):
    id: int
    email: str


class AuditEntry(_Model):
    id: int
    created_at: datetime
    request_id: str | None
    actor: Actor | None
    action: str
    outcome: str
    document_id: UUID | None
    question: str | None
    chunk_ids: list[int]
    details: dict[str, Any]


class AuditPage(_Model):
    total: int
    records: list[AuditEntry]


@dataclass(frozen=True)
class Login:
    token: str
    expires_at: datetime

    def expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


@dataclass(frozen=True)
class Filters:
    """What a search or question may be narrowed to. Empty means no filter."""

    departments: tuple[str, ...] = ()
    classifications: tuple[str, ...] = ()
    document_ids: tuple[UUID, ...] = ()

    def body(self) -> dict[str, list[Any]] | None:
        chosen: dict[str, list[Any]] = {
            "departments": list(self.departments),
            "classifications": list(self.classifications),
            "document_ids": [str(document_id) for document_id in self.document_ids],
        }
        # The API refuses an empty list, so a filter that isn't set is left out.
        kept = {name: values for name, values in chosen.items() if values}
        return kept or None


# --- what can go wrong ------------------------------------------------------------------


class ApiError(Exception):
    """The service refused the request, or couldn't be reached."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        request_id: str | None = None,
        fields: tuple[str, ...] = (),
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.fields = fields
        self.retry_after = retry_after


class NotSignedIn(ApiError):
    """401: sign in again."""


class NotAllowed(ApiError):
    """403: this account may not do this."""


class NotFound(ApiError):
    """404: no such thing, or one this account may not see. They look the same."""


class InvalidInput(ApiError):
    """422: the request was refused; ``message`` and ``fields`` say why."""


class TooManyAttempts(ApiError):
    """429: wait ``retry_after`` seconds."""


class Unavailable(ApiError):
    """503, or the service couldn't be reached at all."""


_BY_STATUS: dict[int, type[ApiError]] = {
    401: NotSignedIn,
    403: NotAllowed,
    404: NotFound,
    422: InvalidInput,
    429: TooManyAttempts,
    503: Unavailable,
}

M = TypeVar("M", bound=BaseModel)


# --- the calls --------------------------------------------------------------------------


class Api:
    def __init__(
        self,
        base_url: str = DEFAULT_API_URL,
        *,
        http: httpx.Client | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=TIMEOUT_SECONDS)
        self._clock = clock

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Api":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # Identity

    def login(self, email: str, password: str) -> Login:
        response = self._send("POST", "/auth/login", json={"email": email, "password": password})
        body = _json(response)
        expires_in = body.get("expires_in")
        token = body.get("access_token")
        if not isinstance(token, str) or not isinstance(expires_in, int):
            raise _bad_response(response)
        return Login(token=token, expires_at=self._clock() + timedelta(seconds=expires_in))

    def me(self, token: str) -> Me:
        return self._get(Me, token, "/auth/me")

    def logout(self, token: str) -> None:
        """Withdraw this token, and every other the person holds, at the service."""
        self._send("POST", "/auth/logout", token=token)

    # Reading

    def documents(
        self,
        token: str,
        *,
        department: str | None = None,
        classification: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> DocumentList:
        params: dict[str, str | int] = {"limit": limit, "offset": offset}
        if department:
            params["department"] = department
        if classification:
            params["classification"] = classification
        return self._get(DocumentList, token, "/documents", params=params)

    def document(self, token: str, document_id: UUID) -> DocumentDetail:
        return self._get(DocumentDetail, token, f"/documents/{_id(document_id)}")

    def search(self, token: str, question: str, filters: Filters | None = None) -> SearchResult:
        return self._post(SearchResult, token, "/search", _question(question, filters))

    def ask(self, token: str, question: str, filters: Filters | None = None) -> Answer:
        return self._post(
            Answer, token, "/ask", _question(question, filters), timeout=ASK_TIMEOUT_SECONDS
        )

    # Managing documents (documents:manage)

    def upload(
        self,
        token: str,
        *,
        filename: str,
        data: bytes,
        title: str,
        department: str,
        classification: str,
        readers: tuple[str, ...] = (),
    ) -> ManagedDocument:
        form: dict[str, Any] = {
            "title": title,
            "department": department,
            "classification": classification,
        }
        if readers:
            form["readers"] = list(readers)
        response = self._send(
            "POST",
            "/admin/documents",
            token=token,
            files={"file": (filename, data)},
            data=form,
        )
        return _parse(ManagedDocument, response)

    def managed_documents(self, token: str, *, limit: int = 100, offset: int = 0) -> ManagedList:
        params = {"limit": limit, "offset": offset}
        return self._get(ManagedList, token, "/admin/documents", params=params)

    def managed(self, token: str, document_id: UUID) -> ManagedDocument:
        return self._get(ManagedDocument, token, f"/admin/documents/{_id(document_id)}")

    def new_version(
        self, token: str, document_id: UUID, *, filename: str, data: bytes, title: str | None = None
    ) -> ManagedDocument:
        response = self._send(
            "POST",
            f"/admin/documents/{_id(document_id)}/versions",
            token=token,
            files={"file": (filename, data)},
            data={"title": title} if title else {},
        )
        return _parse(ManagedDocument, response)

    def relabel(
        self,
        token: str,
        document_id: UUID,
        *,
        classification: str | None = None,
        department: str | None = None,
    ) -> ManagedDocument:
        body = {
            key: value
            for key, value in {"classification": classification, "department": department}.items()
            if value is not None
        }
        response = self._send(
            "PATCH", f"/admin/documents/{_id(document_id)}", token=token, json=body
        )
        return _parse(ManagedDocument, response)

    def delete(self, token: str, document_id: UUID) -> None:
        self._send("DELETE", f"/admin/documents/{_id(document_id)}", token=token)

    def grant(
        self, token: str, document_id: UUID, email: str, expires_at: datetime | None = None
    ) -> ManagedDocument:
        body: dict[str, Any] = {"email": email}
        if expires_at is not None:
            body["expires_at"] = expires_at.isoformat()
        response = self._send(
            "PUT", f"/admin/documents/{_id(document_id)}/grants", token=token, json=body
        )
        return _parse(ManagedDocument, response)

    def revoke(self, token: str, document_id: UUID, email: str) -> None:
        # The email is a path segment: quoted, so "/" or "?" in it can't change the route.
        quoted = quote(email, safe="@")
        self._send("DELETE", f"/admin/documents/{_id(document_id)}/grants/{quoted}", token=token)

    # The audit trail (audit:read)

    def audit(
        self,
        token: str,
        *,
        actor: str | None = None,
        action: str | None = None,
        document_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> AuditPage:
        params: dict[str, str | int] = {"limit": limit, "offset": offset}
        for name, value in (
            ("actor", actor),
            ("action", action),
            ("document_id", None if document_id is None else _id(document_id)),
            ("since", since.isoformat() if since else None),
            ("until", until.isoformat() if until else None),
        ):
            if value is not None:
                params[name] = value
        return self._get(AuditPage, token, "/audit", params=params)

    # Plumbing

    def _get(
        self, model: type[M], token: str, path: str, params: Mapping[str, str | int] | None = None
    ) -> M:
        return _parse(model, self._send("GET", path, token=token, params=params))

    def _post(
        self,
        model: type[M],
        token: str,
        path: str,
        body: dict[str, Any],
        timeout: float | None = None,
    ) -> M:
        return _parse(model, self._send("POST", path, token=token, json=body, timeout=timeout))

    def _send(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        if token is not None and not token.strip():
            # Nothing to send: whoever this is hasn't signed in.
            raise NotSignedIn(401, "not_signed_in", "Sign in first.")
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        try:
            response = self._http.request(
                method,
                path,
                headers=headers,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                **kwargs,
            )
        except httpx.TimeoutException:
            raise Unavailable(0, "timeout", "The service took too long to answer.") from None
        except httpx.TransportError:
            raise Unavailable(0, "unreachable", "The service could not be reached.") from None
        if response.status_code >= 400:
            raise _error(response)
        return response


def _id(document_id: UUID) -> str:
    # Read again as a UUID, so nothing but an ID can reach the path.
    return str(UUID(str(document_id)))


def _question(question: str, filters: Filters | None) -> dict[str, Any]:
    body: dict[str, Any] = {"question": question}
    narrowed = filters.body() if filters else None
    if narrowed:
        body["filters"] = narrowed
    return body


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise _bad_response(response) from None
    if not isinstance(body, dict):
        raise _bad_response(response)
    return body


def _parse(model: type[M], response: httpx.Response) -> M:
    try:
        return model.model_validate(_json(response))
    except ValidationError:
        raise _bad_response(response) from None


def _error(response: httpx.Response) -> ApiError:
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])
    except (ValueError, KeyError, TypeError):
        return _bad_response(response)
    retry_after = response.headers.get("Retry-After")
    kind = _BY_STATUS.get(response.status_code, ApiError)
    return kind(
        response.status_code,
        code,
        message,
        request_id=error.get("request_id"),
        fields=tuple(error.get("fields") or ()),
        retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
    )


def _bad_response(response: httpx.Response) -> ApiError:
    return ApiError(
        response.status_code,
        "bad_response",
        "The service answered in a way the UI doesn't understand.",
        request_id=response.headers.get("X-Request-ID"),
    )
