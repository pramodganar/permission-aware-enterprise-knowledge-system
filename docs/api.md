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

## Logging in and who you are

```
POST /auth/login   {"email": "david@acmecorp.example", "password": "..."}
                -> {"access_token": "...", "token_type": "bearer", "expires_in": 1800}
GET  /auth/me      Authorization: Bearer <token>
                -> id, email, full_name, department, roles, permissions
```

**The token holds only the user's ID** and when it expires ([decision record 0004](adr/0004-identity-only-token.md)). Every request loads the user from the database, so a role change, a move to another department or a deactivated account takes effect on the next request with the same token. `/auth/me` shows exactly what the next request will be decided on: the department and roles as they are now, and the permissions the authorization service gives them.

**Every failed login is the same 401**, with the same body: an unknown email, a wrong password and a deactivated account can't be told apart by the response. Nor, as far as measured, by its timing: an unknown email is checked against a dummy password hash, so it costs the same work. Over 30 attempts each on the development laptop, the median time was 72.9 ms for the right password, 72.0 ms for a wrong one and 73.3 ms for an unknown email. That is a check on one machine, not a proof against a patient attacker measuring thousands of attempts. The reason a login failed is logged, never the email or password typed.

**The body is checked strictly.** Unknown fields are refused, so a client can't send `"role": "executive"` alongside its password and hope something reads it. A password over 1,024 characters is refused before hashing, so a huge one can't keep the server busy. As everywhere, a refused body names the fields and never echoes what was in them.

The token response carries `Cache-Control: no-store`.

### Too many failed logins

After `APP_LOGIN_MAX_FAILURES` failed logins (5) for one email within `APP_LOGIN_FAILURE_WINDOW_SECONDS` (15 minutes), further attempts get **429** with a `Retry-After` header, and the password isn't checked at all, so even the right one is refused until the oldest failure leaves the window. A successful login clears the count.

Attempts are counted **per email as typed**, normalised, whether or not an account has it. An unknown email is limited exactly like a real one, so the limit doesn't reveal which emails exist.

What it doesn't cover (threat T10):

- It is in memory, per process. Two copies of the service count separately, and a restart forgets. A shared store such as Redis would fix both.
- It counts per email, not per client, so one client trying one common password on many emails isn't slowed. A per-client limit needs the client's real address, which behind a proxy is a trust question of its own.
- Someone who knows a colleague's email can lock them out for the window by failing on purpose. The window is short for that reason.
- At most 10,000 emails are remembered; past that the one not seen for longest is forgotten, so a flood of made-up emails can't use up memory.

## Documents

```
GET /documents          ?department=hr&classification=departmental&limit=100&offset=0
                     -> {"total": 3, "documents": [{id, title, department, classification,
                                                    file_type, version, updated_at}, ...]}
GET /documents/{id}  -> the same fields, plus source_filename, size_bytes, created_at, content
```

**The list is the documents the caller may read, and nothing else** (FR-5). It is the same `readable_documents` query that retrieval filters by, so the list and search can't disagree. Someone with no reading role, such as a knowledge admin or a security reviewer, gets an empty list, not an error. Documents come in title order; `total` counts every page.

**A document the caller may not read gets exactly the 404 of one that doesn't exist**: the same body and the same headers. Trying IDs one by one tells nobody which documents exist. A replaced or deleted document, or one whose grant has expired, is gone from the list and gives the same 404 on the next request. The log records a denied attempt, with the document's ID, so a security reviewer can see someone trying; a missing ID isn't logged, and no title is ever logged.

An ID that can't be a document ID (not a whole number, below 1, or too big for the database) is invalid input (422), not a database error.

**Filters only narrow.** `department` and `classification` are added to the caller's query, so they can only remove documents from it. Asking for HR's documents as David gives the one HR document everyone may read, the Leave Policy, and nothing else. A filter for something the caller can't read gives an empty list, the same as a filter that matches nothing. An unknown department isn't an error, for the same reason.

**What is never shown**, even to someone who may read the document: who else holds a grant, who uploaded it, and which earlier version it replaced. A reader needs none of it, and each says something about people or documents outside their own access. The response models list their fields one by one, so a field added to a document doesn't reach a client unless it's added to the model too.

**Both responses carry `Cache-Control: no-store`.** Each person's list is different, and a cache shared between people would otherwise hand one person's list to another (threat T6).
