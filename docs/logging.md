# Logging

What the service writes to its log, for whoever runs it: the format, how to follow one request, where the time goes, what is never written, and every event with its fields. A test reads every log call in [app/](../app/) and compares it with this page, so an event can't be added, renamed or given a new field without the page saying so.

The log is for running the service. Who asked what, and which documents it used, is the audit trail's job ([audit](audit.md)), which has its own access control.

## The format

One JSON object per line, on stdout:

```json
{"timestamp": "2026-10-26T11:26:21.015+00:00", "level": "INFO", "logger": "app.retrieval.search", "message": "search done", "request_id": "0ecbc91f4fdf40ddb256867469529c69", "user_id": 4, "in_scope": 13, "not_indexed": 0, "hits": 5, "milliseconds": 6}
```

| Field | On | What |
|---|---|---|
| `timestamp` | every line | UTC, to the millisecond |
| `level` | every line | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `logger` | every line | The module that wrote it, such as `app.retrieval.search` |
| `message` | every line | Fixed text naming the event: never a value, so it can be searched for |
| `request_id` | lines written during a request | The same on every line of one request ([below](#following-one-request)) |
| `exception` | a few errors | The traceback |

Then the event's own fields, listed [below](#every-event): IDs, counts, scores and timings.

**Everything is in this format,** so whatever collects the log reads one format from one stream:

- **The server's own lines,** uvicorn's: it gives its loggers handlers of its own, writing plain text to stderr, and the service takes them away at startup, so they go through the same formatter. They follow `APP_LOG_LEVEL` like the rest.
- **uvicorn's access log is silenced,** not only switched off with `--no-access-log`: each of its lines holds the request's full URL, query string included, and the client's address. The service writes its own line for every request without either.
- **Warnings** a library raises with Python's `warnings` are logged as `py.warnings`, at WARNING.
- **Progress bars** the model libraries draw while a model loads are switched off: in a container's log they were text in among the JSON on every start.
- **Libraries that log every HTTP request they make** at INFO (`httpx`, `httpcore`, `huggingface_hub`, `urllib3`) keep only their warnings and errors. The service logs what it sends, as counts.
- **The PDF reader (`pypdf`) writes nothing:** its warnings quote the bytes it tripped over, a document's own text. Found by fuzzing it ([tests/unit/test_fuzzing.py](../tests/unit/test_fuzzing.py)).

A Docker test reads every line the API's container writes, both streams, and fails on one that isn't JSON.

## Levels

`APP_LOG_LEVEL` sets it, `INFO` by default, for the service and its scripts alike ([configuration](configuration.md)). Under Docker Compose it is passed to `setup` and `api`.

| Level | Adds |
|---|---|
| `ERROR` | Something is wrong and needs looking at: an unexpected error, an audit record that couldn't be written, the two access checks disagreeing |
| `WARNING` | Expected, but worth knowing: a failed login, a refused action, a request over its limit, a model call retried, an index behind the database |
| `INFO` | Every request, every stage of every search and question, every change to a document or its access, startup |
| `DEBUG` | A passing health check, which Docker makes every 30 seconds ([Docker](docker.md#health-checks)) |

No level writes document text or a question ([below](#what-is-never-written)).

## Following one request

Every request gets an ID: a new one, or the client's own in `X-Request-ID` if it is made of letters, digits and dashes, at most 64 long ([API](api.md)). It is in the `X-Request-ID` response header, in every error body, and on every line the request writes. A person reporting a problem quotes it; an operator finds every line with it:

```
docker compose logs --no-log-prefix api | grep 0ecbc91f4fdf40ddb256867469529c69
```

One search, as the stack wrote it (the timestamps, logger and request ID left out):

```json
{"level": "INFO", "message": "question processed", "characters": 31, "tokens": 9, "milliseconds": 8}
{"level": "INFO", "message": "access scope decided", "user_id": 4, "documents": 4, "chunks": 13, "milliseconds": 1}
{"level": "INFO", "message": "search done", "user_id": 4, "in_scope": 13, "not_indexed": 0, "hits": 5, "milliseconds": 4}
{"level": "INFO", "message": "chunks retrieved", "user_id": 4, "chunks": 5, "dropped": 0, "best_score": 0.375, "below_threshold": false, "reranked": false, "milliseconds": 7, "rerank_milliseconds": null}
{"level": "INFO", "message": "audit record sealed", "audit_id": 29, "action": "search"}
{"level": "INFO", "message": "request handled", "method": "POST", "path": "/search", "status": 200, "milliseconds": 44}
```

## Where the time goes

Each stage says how long it took, in `milliseconds`, so a slow answer can be traced to the step that was slow:

| Line | Times |
|---|---|
| `question processed` | Checking the question and embedding it |
| `access scope decided` | Working out what the person may read |
| `search done` | The vector search inside that scope |
| `chunks retrieved` | The whole retrieval: scope, search, the second check and reranking. `rerank_milliseconds` is the reranker alone, or `null` without one |
| `model answered` | The model call, retries included |
| `question answered` | `retrieval_milliseconds` from the question to the context, `generation_milliseconds` the model and checking its answer |
| `request handled` | The whole request, as the service saw it |

Ten searches through the Docker stack on the development laptop, David's five questions twice, the real embedding model on the CPU and no reranker:

| Line | Median | Range |
|---|---|---|
| `question processed` | 8 ms | 8–13 ms |
| `access scope decided` | 1 ms | 1–5 ms |
| `search done` | 6 ms | 4–10 ms |
| `chunks retrieved` | 10 ms | 7–21 ms |
| `request handled` | 45 ms | 38–66 ms |

So about 18 ms of a 45 ms search is the stages above. The rest, checking the token, writing and committing the audit record and sending the response, isn't timed separately. A question adds the model, which takes seconds, not milliseconds.

A test makes the reranker and the model slow on purpose and checks the time shows up in their lines and in no line before them ([tests/integration/test_request_log.py](../tests/integration/test_request_log.py)).

## What is never written

Document text, questions, answers, prompts, passwords and tokens (NFR-9, SR-7). What a person asked and which documents answered it are in the audit trail, which only security reviewers can read.

- **A message is fixed text,** and the values are named fields: a test fails on a log call whose message isn't a plain string, such as an f-string, or that passes values to be formatted into it.
- **No field is named for content.** The formatter replaces the value of a field named `password`, `token`, `authorization`, `secret`, `question`, `query`, `answer`, `prompt`, `context`, `content` or `text` with `[redacted]`, as a safety net; a test fails on any log call that uses one of those names, so the safety net is never what stands between a document and the log.
- **Errors are logged by type,** never by message: a library's message can quote what it was reading. The client gets a fixed message and the request ID.
- **Which documents a person may read** is logged as counts, never as a list: that is itself sensitive.
- **A path that matched no route** is logged as `(no matching route)`: it is whatever the client typed.

Tested by using the service the way people do, failures included, at DEBUG, and searching every line written, by every logger, for any piece of any document, question, password or token ([tests/security/test_logs.py](../tests/security/test_logs.py)).

## Keeping the log

Under Docker Compose each container's log is rotated: at most five files of 10 MB. Docker's default keeps one file that grows until the disk is full. `docker compose logs` reads them.

## Every event

The service's own, by area. A field that is `null` wasn't known or doesn't apply.

**Requests**

| Event | Level | Logger | Fields |
|---|---|---|---|
| `request handled` | INFO; DEBUG for a passing health check | `app.api.middleware` | `method`, `path`, `status`, `milliseconds` |
| `unexpected error` | ERROR | `app.api.middleware` | `error`, `where` |
| `request limited` | WARNING | `app.api.questions` | `user_id`, `route` |

`path` is the route's pattern, such as `/documents/{document_id}`, never the IDs in it. `where` is the traceback without the error's message.

**Signing in and access**

| Event | Level | Logger | Fields |
|---|---|---|---|
| `login succeeded` | INFO | `app.auth.service` | `user_id` |
| `login failed` | WARNING | `app.auth.service` | `reason`, `user_id` |
| `action denied` | WARNING | `app.authorization.checks` | `user_id`, `permission` |
| `document not returned` | WARNING | `app.authorization.checks` | `user_id`, `document_id`, `exists` |
| `document access granted` | INFO | `app.authorization.document_access` | `document_id`, `user_id`, `grantee_id`, `expires` |
| `document access revoked` | INFO | `app.authorization.document_access` | `document_id`, `user_id`, `grantee_id` |
| `document labels changed` | INFO | `app.authorization.document_access` | `document_id`, `user_id`, `old_classification`, `new_classification`, `old_department`, `new_department` |

`login failed` has a `user_id` only when the email belongs to someone. `exists` says whether a document that wasn't returned exists at all; the client is told the same thing either way.

**Searching and asking**

| Event | Level | Logger | Fields |
|---|---|---|---|
| `question processed` | INFO | `app.retrieval.query` | `characters`, `tokens`, `milliseconds` |
| `access scope decided` | INFO | `app.retrieval.scope` | `user_id`, `documents`, `chunks`, `milliseconds` |
| `scope narrowed` | INFO | `app.retrieval.filters` | `user_id`, `filters`, `chunks_before`, `chunks_after` |
| `search done` | INFO | `app.retrieval.search` | `user_id`, `in_scope`, `not_indexed`, `hits`, `milliseconds` |
| `search skipped, empty scope` | INFO | `app.retrieval.search` | `user_id` |
| `search returned chunks outside the scope` | ERROR | `app.retrieval.search` | `user_id` |
| `vector search returned chunks outside the allowed list` | ERROR | `app.retrieval.index` |  |
| `chunks retrieved` | INFO | `app.retrieval.retriever` | `user_id`, `chunks`, `dropped`, `best_score`, `below_threshold`, `reranked`, `milliseconds`, `rerank_milliseconds` |
| `second check removed chunks the scope allowed` | ERROR | `app.retrieval.retriever` | `user_id`, `dropped` |
| `not reranked, a chunk and the question are too long for the reranker` | WARNING | `app.retrieval.retriever` | `model` |
| `context built` | INFO | `app.rag.context` | `user_id`, `sources`, `chunks`, `left_out`, `characters` |
| `model answered` | INFO | `app.rag.llm` | `provider`, `model`, `attempts`, `milliseconds`, `input_tokens`, `output_tokens`, `stop_reason`, `prompt_version` |
| `model call failed` | WARNING | `app.rag.llm` | `provider`, `attempt`, `error` |
| `question answered` | INFO | `app.rag.answer` | `user_id`, `outcome`, `reason`, `sources`, `citations`, `unknown_citations`, `removed_links`, `cut_off`, `retrieval_milliseconds`, `generation_milliseconds` |

The three ERRORs about chunks outside a scope or an allowed list mean an access check is wrong: the request is refused or the chunk dropped, and it needs looking at the same day ([threat model](threat-model.md)). `filters` names which kinds of filter were given, not their values. `best_score` is there to check the relevance threshold against real questions.

**Documents**

| Event | Level | Logger | Fields |
|---|---|---|---|
| `document ingested` | INFO | `app.ingestion.pipeline` | `document_id`, `user_id`, `department`, `classification`, `characters`, `chunks`, `grants` |
| `document replaced` | INFO | `app.ingestion.pipeline` | `document_id`, `replaced_document_id`, `version`, `user_id`, `characters`, `chunks` |
| `document deleted` | INFO | `app.ingestion.pipeline` | `document_id`, `user_id` |
| `documents re-chunked` | INFO | `app.ingestion.pipeline` | `documents`, `user_id` |
| `text file was not UTF-8, read as Windows-1252` | WARNING | `app.ingestion.extractors` | `bytes` |
| `removed repeated page lines from PDF text` | INFO | `app.ingestion.extractors` | `pages`, `lines_per_page` |

**The index and the models**

| Event | Level | Logger | Fields |
|---|---|---|---|
| `embedding model loaded` | INFO | `app.retrieval.embeddings` | `model`, `dimension`, `max_tokens` |
| `chunks embedded` | INFO | `app.retrieval.embeddings` | `chunks`, `model` |
| `reranker loaded` | INFO | `app.retrieval.reranking` | `model`, `max_tokens` |
| `language model provider chosen` | INFO | `app.rag.providers` | `provider`, `model` |
| `vector index built` | INFO | `app.retrieval.index` | `chunks`, `dimension`, `model` |
| `vector index saved` | INFO | `app.retrieval.index_store` | `chunks`, `model` |
| `vector index loaded` | INFO | `app.retrieval.index_store` | `chunks`, `model`, `built_at` |
| `index version published` | INFO | `app.retrieval.index_versions` | `version`, `chunks` |
| `index rolled back` | WARNING | `app.retrieval.index_versions` | `from_version`, `to_version` |
| `old index versions removed` | INFO | `app.retrieval.index_versions` | `versions` |
| `the vector index is behind the database; rebuild it with: python -m scripts.index build` | WARNING | `app.api.components` | `missing`, `retired` |

**The audit trail**

| Event | Level | Logger | Fields |
|---|---|---|---|
| `audit record sealed` | INFO | `app.models.audit` | `audit_id`, `action` |
| `audit record not written` | ERROR | `app.audit.trail` | `action`, `error` |

`audit record not written` means the request it belonged to was refused: nothing is answered that can't be audited ([audit](audit.md)).

## Not covered

- **Shipping the log anywhere.** It stays in Docker's files on the machine. What is watched in it, and when someone is called, is in [monitoring](monitoring.md); collecting it centrally is not set up.
- **The token check, the audit write and the commit aren't timed** separately; together they are most of a fast search's time.
- **The scripts** (`seed`, `index`, `audit`) write the same JSON lines, but their own output, what they made and what they found, is printed for a person to read.
