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

FastAPI's own answer to a bad body includes the input, which here could be a password or a question. This one lists the fields only. The name of a field the client added is the client's own text too, so it is shown only if it looks like a field name (letters, digits and underscores, up to 64): `body.role` is shown, a key like `<script>…</script>` becomes `body.(unrecognised)`.

**One log line per request**: method, route pattern (`/documents/{document_id}`, not the ID), status and milliseconds. Not the query string, which can hold a question, and not the client's address. A path that matches no route isn't logged either: it is whatever the client typed, and could be made to look like a log entry. uvicorn's own access log is switched off and silenced for the same reason; it writes the full URL.

An unexpected error is logged with its type and where it happened, not its message: a library's error message can hold document text (NFR-9). The PDF reader's own logger is switched off: its warnings quote the bytes of the file it is reading, which fuzzing showed put document text in the log. A test reads every line written at DEBUG, from every logger, over a session of use in which a model and a reranker fail with text in their errors, and finds no piece of any document, no question, password or token.

The libraries that log one line per HTTP request they make (`httpx`, `huggingface_hub` and others) keep only their warnings. Running the service showed them logging every check of the embedding model's files at startup, and they would log every call to the language model.

Every event the service logs, its level and its fields, how to follow one request through the log, and where a request's time goes are in [logging](logging.md).

**Headers on every response**, errors and unknown paths included:

| Header | Why |
|---|---|
| `Cache-Control: no-store` | Unless a route sets its own. Nearly every response differs per person, and a shared cache must not keep one |
| `X-Content-Type-Options: nosniff` | A browser treats JSON as JSON, even when document text inside it looks like HTML |
| `X-Frame-Options: DENY` | There are no pages to frame |
| `Referrer-Policy: no-referrer` | No URL of the service travels in a Referer |

There are no CORS headers: a page on another site can't call the service from a browser.

## Every route, and who it is for

| Route | For |
|---|---|
| `GET /health`, `POST /auth/login` | anyone |
| `GET /auth/me`, `GET /documents`, `GET /documents/{id}`, `POST /search`, `POST /ask` | anyone logged in; what they get depends on what they may read |
| `/admin/documents...` (seven routes) | `documents:manage` |
| `GET /audit` | `audit:read` |

The security tests read the routes off the app and fail for one that isn't in this table, so a new route can't be added without deciding who it is for. They also check each route depends on the check its row says, and call every route with no token, five kinds of bad token, a deactivated account and each of the nine people ([tests/security/test_api_routes.py](../tests/security/test_api_routes.py)).

## Health

`GET /health` → `{"status": "ok"}`, or 503 if the database doesn't answer. It needs no token, so it says nothing else: no version, no model, no counts.

It answers only once the service is ready: the port opens after the embedding model and the index are loaded. That is what the Docker images' health checks ask, and what Compose waits for before starting the UI ([Docker](docker.md#health-checks)). Docker asks every 30 seconds, so a 200 is logged at DEBUG, below the default level; a 503 is logged like any other request.

## Logging in and who you are

```
POST /auth/login   {"email": "david@acmecorp.example", "password": "..."}
                -> {"access_token": "...", "token_type": "bearer", "expires_in": 1800}
GET  /auth/me      Authorization: Bearer <token>
                -> id, email, full_name, department, roles, permissions
```

**The token holds only the user's ID** and when it expires ([decision record 0004](adr/0004-identity-only-token.md)). Every request loads the user from the database, so a role change, a move to another department or a deactivated account takes effect on the next request with the same token. `/auth/me` shows exactly what the next request will be decided on: the department and roles as they are now, and the permissions the authorization service gives them.

**Every failed login is the same 401**, with the same body: an unknown email, a wrong password and a deactivated account can't be told apart by the response. Nor, as far as measured, by its timing: an unknown email is checked against a dummy password hash, so it costs the same work. Over 30 attempts each on the development laptop, the median time was 72.9 ms for the right password, 72.0 ms for a wrong one and 73.3 ms for an unknown email. That is a check on one machine, not a proof against a patient attacker measuring thousands of attempts. The reason a login failed is logged, never the email or password typed. Every login is in the audit trail, the ones that succeed included, and so is every attempt refused for too many failures.

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

**A document the caller may not read gets exactly the 404 of one that doesn't exist**: the same body and the same headers. Trying IDs one by one tells nobody which documents exist. A replaced or deleted document, or one whose grant has expired, is gone from the list and gives the same 404 on the next request. Each refused lookup leaves one log line with the ID and whether a document with it exists, so a security reviewer can see someone trying; no title is ever logged.

Nor does the time taken tell the two apart, as far as measured. Both do the same work: the scope query, a lookup of the ID alone, one log line and one audit record. The lookup used to load the whole document to see that it exists, so a hidden document's 404 took longer the bigger it was: for one of 2 MB, the median was 12.5 ms against 9.2 ms for a missing ID, over 1,000 requests each on the development laptop. Reading the ID alone, it was 9.4 ms, against 8.9 ms for a hidden document of 4 bytes and 9.3 ms for a missing ID, within the noise. A test holds the cause rather than the timing: a refused lookup runs the same queries either way and never loads a document.

**Nor do the IDs themselves.** Every document is known outside the service by its public ID, a random UUID such as `3f0c9a52-8d1e-4b7a-9c61-2e5d8f4a7b10`, made with the document and taken from nothing else ([decision record 0014](adr/0014-random-public-document-ids.md)). The database numbers documents in the order they were stored, and if readers saw those numbers, one who could open 3 and 7 would know three others were stored between them (threat T36). The numbers stay inside the service: every response, path and filter here, and the admin and audit routes, use the public ID. Each new version of a document has a public ID of its own, as it is a document of its own.

Anything that isn't a UUID is invalid input (422), not a database error.

**Filters only narrow.** `department` and `classification` are added to the caller's query, so they can only remove documents from it. Asking for HR's documents as David gives the one HR document everyone may read, the Leave Policy, and nothing else. A filter for something the caller can't read gives an empty list, the same as a filter that matches nothing. An unknown department isn't an error, for the same reason.

**What is never shown**, even to someone who may read the document: who else holds a grant, who uploaded it, and which earlier version it replaced. A reader needs none of it, and each says something about people or documents outside their own access. The response models list their fields one by one, so a field added to a document doesn't reach a client unless it's added to the model too.

**Both responses carry `Cache-Control: no-store`.** Each person's list is different, and a cache shared between people would otherwise hand one person's list to another (threat T6).

## Searching and asking

```
POST /search  {"question": "How many days of annual leave do I get?",
               "filters": {"departments": ["hr"], "classifications": ["general"],
                           "document_ids": ["3f0c9a52-8d1e-4b7a-9c61-2e5d8f4a7b10"]}}
           -> {"results": [{document {id, title, version}, start_offset, end_offset, text, score}, ...],
               "message": null}

POST /ask     the same body
           -> {"answered": true, "answer": "... [S1]", "citations": [{label, document, text}, ...],
               "cut_off": false, "uncited": false}
```

`filters` is optional, and so is each field in it.

**Both are POST**, so the question travels in the body. In the URL it would end up in proxy logs, browser history and the service's own request log. The question isn't logged here either, and neither is the answer (NFR-9).

**No rules of their own.** `/search` calls `retrieve` and `/ask` calls `answer_question`, the one path to the model, with the user from the token. The body allows exactly `question` and `filters`; a `user_id` or `role` next to them is refused (422). Filters narrow within what the caller may read, as in [retrieval](retrieval.md): filtering on a document the caller can't read gives exactly the response of filtering on one that doesn't exist.

**"No information" looks the same whatever the reason.** Nothing the caller may read is relevant, the model found nothing in its sources, or the provider refused: each gets `"answered": false` and "No information available to answer this question.", and an empty search gets the same message. Which reason it was is logged, not returned, and the relevance score it was decided on isn't returned either.

**Citations are the sources the answer cites**, not everything the model was given, each with the passage it points to. They come from the answer's own context, so every one is to something the caller may read. A label the model made up is removed from the text. An answer that reached the length cap is marked `cut_off`; one that cites nothing is marked `uncited`.

**When the model can't answer**, `/ask` gets 503 and `/search` keeps working (NFR-5):

| Situation | Response |
|---|---|
| `APP_LLM_PROVIDER=none` | 503, "Answers are switched off on this service. Search still works." |
| The provider is down, too slow or rate-limiting, after its retries | 503, "The service is temporarily unavailable." |
| The model's reply is unusable: empty, or nothing but made-up citations | 503, the same |
| The provider refuses, with or without text | 200, the usual "no information" answer |

A question that can't be searched with (empty, no words, over `APP_MAX_QUESTION_CHARACTERS`) is a 422 that says which, and nothing reaches the model. A body over 10,000 characters is refused before any of that.

If `APP_RERANKER_MODEL` names a cross-encoder, it is loaded at startup and both routes use it.

Both responses carry `Cache-Control: no-store`.

### How often

Each person may ask **10 questions in any minute and 200 in any 24 hours**, and search **30 times in any minute** (`APP_ASK_LIMIT_PER_MINUTE`, `APP_ASK_LIMIT_PER_DAY`, `APP_SEARCH_LIMIT_PER_MINUTE`). A call over a limit gets **429** with a `Retry-After` header, rounded up, so waiting that long is always enough. The windows slide: a call is taken if there are fewer than the limit in the last minute, or day, however they were spread.

NFR-3 caps the size of each model call, and the daily limit caps how many there are. Together they bound what one person can spend: at most 200 questions a day, each at most three calls (the first and `APP_LLM_MAX_RETRIES` retries, 2 by default), each with at most `APP_CONTEXT_MAX_CHARACTERS` of document text and `APP_LLM_MAX_OUTPUT_TOKENS` of answer. The per-minute limits are against a script, not a person: someone reading the answers asks a few a minute.

How it counts:

- **Per person, by the user ID in the token.** Logging in again doesn't start a new count, and one person's limit is theirs alone. A call without a valid token is refused with 401 first and counts for nobody.
- **Every call counts, whatever it asks and however it ends.** It is checked before the body is looked at, so an invalid body counts too. A question about a document the person may not read gets no information without the model being asked, and it counts exactly like one that is answered. If only model calls counted, where the limit fell would depend on what the person may read.
- **A refused call isn't counted**, so retrying while refused doesn't push the wait further away. A call refused by one window uses nothing in the other.
- **Asking and searching have separate budgets**, so running out of questions leaves search working.
- **A limited call is logged, not audited**: one `request limited` line with the user ID and the route, never the question. It was given nothing, and recording each one would turn a flood of calls into a flood of records.

What it doesn't cover (threat T24):

- Like the login limit, it is in memory, per process. Two copies of the service count separately, and a restart forgets. A shared store such as Redis would fix both.
- It counts calls, not cost: a question with a long answer counts the same as one with no answer.
- There is no limit for the service as a whole. The total is the number of people times their limits. A company large enough for that to exceed the provider's quota needs a shared budget in front of the provider as well.
- The other routes aren't limited. They read the database and nothing else, and the managing routes are for admins.

## Managing documents

Every route here needs the `documents:manage` permission, which in the company only Henry, the knowledge admin, holds. Anyone else gets 403 on every one of them, before the body is looked at, and the refusal is recorded in the audit trail; without a token it is 401, and only the application log has it.

```
POST   /admin/documents                       multipart: file, title, department, classification, readers (repeated)
GET    /admin/documents                       ?limit=100&offset=0  -> {"total": 17, "documents": [...]}
GET    /admin/documents/{id}                  how one document is managed
POST   /admin/documents/{id}/versions         multipart: file, and optionally a new title
PATCH  /admin/documents/{id}                  {"classification": ..., "department": ...}
DELETE /admin/documents/{id}                  204
PUT    /admin/documents/{id}/grants           {"email": ..., "expires_at": "2026-12-31T00:00:00+00:00"}
DELETE /admin/documents/{id}/grants/{email}   204
```

The work is done by the ingestion pipeline and the document access module ([authorization](authorization.md)), which check the permission a second time. So each rule there applies here: no document without a department and a classification, no restricted document without a named reader, no grant to oneself or to someone who can't read, an expiry must have a timezone and be in the future, and the same file can't be stored twice. A refusal says which rule, and nothing is stored.

**Managing a document is not reading it.** Every response here describes how a document is managed: ID, version, status, department, classification, file type and size, how many chunks, which version it replaced, and who holds a grant. Never its title, file name or text. Henry works with documents by ID, which he finds in the list: every document, newest first, replaced and deleted versions included, each as it is managed. Uploading one doesn't let him open it, and his own document list stays empty.

**Changes apply on the next request**, because access is decided from the database. A deleted or replaced document stops appearing in anyone's list, lookups and search at once. A relabelled one moves to its new audience at once.

**Nobody can give themselves access** through these routes. A grant, or an upload's list of readers, that names the admin is refused with 403, and so is a label change that would let the admin read a document they can't read now: relabelling a restricted document `general`, or moving one into their own department. Henry holds no reading role, so this only matters for an admin who is also a reader; for them it was a way round the rule on grants, until a test that tries every label change on every document found it. A change that leaves the document closed to them is made as before.

**Search is the one thing that waits.** A new document or version is readable at once, through the list and by ID, but search only finds it once the index has been rebuilt (`python -m scripts.index build`) and the service restarted, since the index is loaded at startup. Every response says `in_index`, so the admin can see whether search can find it yet.

### The size of a request

An uploaded form is read before the route's permission check can run, so without a limit anyone could make the service take in a body of any size. The middleware refuses, before anything reads the body:

- a body declared larger than `APP_MAX_UPLOAD_BYTES` plus 64 KB for the form around the file: 413;
- a body that doesn't declare its length (chunked): 411. Its size can only be known by reading it, and every ordinary client declares the length.

The server holds a client to the length it declared, so the declared length is a real bound. A file within that but over `APP_MAX_UPLOAD_BYTES` itself is refused by the pipeline with a 422 that gives the limit.

## Audit trail

```
GET /audit   ?actor=...&action=...&document_id=...&since=...&until=...&limit=100&offset=0
```

Needs `audit:read`, which only the security reviewer holds. Every question, search, document opened, failed login and change is recorded, and if a record can't be stored the request fails with 503 rather than going unrecorded. What is recorded, and how it's kept append-only: [audit trail](audit.md).
