# API

The HTTP service (FR-15). Being built in phase 9; this page grows with it.

```
python -m scripts.seed           # once: the company data
python -m scripts.index build    # once, and after documents change
uvicorn app.main:serve --factory --no-access-log
```

Interactive documentation is at `/docs` except when `APP_ENVIRONMENT=production`, where it is switched off along with `/openapi.json`: a list of every route is not something to publish.

## Startup and shutdown

The slow parts are loaded once, at startup: the embedding model, the current index version, and the language model provider named in `APP_LLM_PROVIDER`. On the development laptop that takes about 4 seconds once the model is downloaded.

The service **refuses to start** without a usable index: missing, damaged, or built with another embedding model or other chunk settings (NFR-6). An index that is only **behind** the database starts, with a warning in the log. That is safe, because access is decided from the database on every request, so a document deleted or locked since the build is not served from it. What's missing is anything added since, until `python -m scripts.index build` runs again (NFR-7).

At shutdown the connection to the language model provider is closed.

## Every response

**A request ID**, in the `X-Request-ID` header and in every error body, tying together every log line of the request (NFR-8). A client may send its own for tracing, but only letters, digits and dashes, up to 64: anything else is replaced, because the value goes into every log line.

**Errors** always have one shape:

```json
{"error": {"code": "not_found", "message": "Not found.", "request_id": "6f1c..."}}
```

| What happened | Status | Code | What the client is told |
|---|---|---|---|
| The service refused something on purpose | 401, 403, 404, 422, 503 | per error type | The fixed message for that type, never which document, user or rule |
| A body that doesn't match the schema, or isn't JSON | 422 | `invalid_input` | Which fields are wrong, never what was in them |
| A route that doesn't exist | 404 | `not_found` | The same as a document that doesn't exist |
| The wrong method | 405 | `method_not_allowed` | |
| Anything unexpected | 500 | `internal_error` | "Something went wrong." and the request ID |

FastAPI's own answer to a bad body includes the input, which here could be a password or a question. This one lists the fields only.

**One log line per request**: method, route pattern (`/documents/{document_id}`, not the ID), status and milliseconds. Not the query string, which can hold a question, and not the client's address. A path that matches no route isn't logged either: it is whatever the client typed, and could be made to look like a log entry. uvicorn's own access log is switched off for the same reason; it writes the full URL.

An unexpected error is logged with its type and where it happened, not its message: a library's error message can hold document text (NFR-9).

The libraries that log one line per HTTP request they make (`httpx`, `huggingface_hub` and others) keep only their warnings. Running the service showed them logging every check of the embedding model's files at startup, and they would log every call to the language model.

## Health

`GET /health` → `{"status": "ok"}`, or 503 if the database doesn't answer. It needs no token, so it says nothing else: no version, no model, no counts.
