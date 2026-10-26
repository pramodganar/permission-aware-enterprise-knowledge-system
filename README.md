# Permission-Aware Enterprise Knowledge System

A question-answering system over internal company documents where a user can only get answers built from documents they are allowed to read.

Most RAG examples put every document into one vector index, which means anyone can retrieve anything by asking the right question. This project is about not doing that. Permissions are applied during retrieval, before anything is sent to the LLM, so the model never sees content the user shouldn't.

```
User -> Auth -> Identity -> Authorization -> Filtered retrieval -> Context -> LLM -> Answer + citations
```

The setting is AcmeCorp, a fictional company with seven departments. All documents and users are synthetic.

**Live demo:** [https://permission-aware-enterprise-knowledge-system-ij67laxbihrkmrqfw.streamlit.app/](https://permission-aware-enterprise-knowledge-system-ij67laxbihrkmrqfw.streamlit.app/). Pick who you are, ask a question, or ask the same question as two people side by side ([how it works](docs/demo.md)).

## Status

Work in progress. Identity, authorization, document ingestion, permission-aware retrieval and answer generation are built and tested, and the AcmeCorp company data can be loaded with one command. The HTTP API is complete: logging in, listing and opening documents, searching, asking, managing documents and the audit trail, with a security test that walks every route. The web UI covers all of it, talks only to the API, and starts with the API in one command.

- [x] Requirements, personas, architecture, threat model, decision records
- [x] Project structure, settings, logging, error types, lint and type checks
- [x] Users, departments, roles and authentication
- [x] Authorization: roles, document classification, grants, and tests for the full access matrix
- [x] Document ingestion: text, PDF and Word files, cleaning, file details, versions and deletion
- [x] Company data: 17 documents in PDF, Word and text, 9 users, and a script that loads them
- [x] Chunking, embeddings and the vector index, with versions and rollback
- [x] Permission-aware retrieval: access scope, filters, relevance threshold, optional reranking, and end-to-end boundary tests
- [x] Answer generation with citations, measured on two real models, and end-to-end answer boundary tests
- [x] API: login, documents, search and ask, admin routes, audit trail, and a walk of every route for its authorization
- [x] UI: signing in, asking and searching, documents, managing documents and the audit trail, each offered by permission (and a public demo page: [docs/demo.md](docs/demo.md))
- [x] Evaluation: a labelled set of 73 questions, retrieval, leakage through the API, answer quality checked by hand, latency, and one command that checks every target
- [ ] Docker and CI

Measured on the AcmeCorp data ([evaluation report](docs/evaluation-report.md)): no leak in 18 forbidden and injection probes, in any configuration, including a model that repeats everything it was given; 1 wrongful refusal in 34 answerable questions with Groq; the answer chunk in the top five for 33 of 34; 33 answers of which, by hand, all are correct and 32 faithful; retrieval 7.4 ms and an answered question 1.6 s at p95, and the permission filter 2.6 ms at the median. How each is measured, and what it doesn't cover, is in [evaluation](docs/evaluation.md).

## The idea in one example

Alice works in Engineering and asks "What is the executive compensation strategy?"

- The retriever returns no chunks from Executive documents.
- The LLM is not given that content. With nothing to answer from, it isn't called at all.
- Alice gets the same response she would get for a question no document covers.
- The query is recorded in the audit log.

Grace, an executive, asks the same question and gets an answer with sources.

## Documentation

- [Requirements](docs/requirements.md): the problem, and the functional, non-functional and security requirements
- [Personas](docs/personas.md): who uses the system and what each person can and cannot see
- [Architecture](docs/architecture.md): components, request flow and repository layout
- [Authorization](docs/authorization.md): roles, permissions and the rules that combine them
- [Chunking](docs/chunking.md): how documents are split, with the measurements behind it
- [Retrieval](docs/retrieval.md): from question to chunks, and how the relevance threshold was measured
- [Generation](docs/generation.md): from chunks to an answer with sources
- [API](docs/api.md): the HTTP service, its errors, request IDs and logs
- [Audit trail](docs/audit.md): what is recorded, who can read it, and why a request fails without it
- [UI](docs/ui.md): the web UI, which only talks to the API
- [Demo page](docs/demo.md): pick a person, ask, compare two people; how to run and deploy it
- [Evaluation](docs/evaluation.md): the labelled question set and how the system is measured
- [Evaluation report](docs/evaluation-report.md): every target, what was measured, and whether it's met
- [Security](docs/security.md): the promise, where it is kept, what shows it, and how to run it safely; start here for a review
- [Threat model](docs/threat-model.md): what could go wrong and what is left uncovered
- [Configuration](docs/configuration.md): every setting, where settings come from, what production changes, and secrets as files
- [Docker](docs/docker.md): the API and UI images, what is in them, and how to run them
- [Logging](docs/logging.md): the log's format, following one request, where the time goes, what is never written, and every event
- [Secrets](docs/secrets.md): what is secret, where it lives, how it is checked and how to replace it
- [Decision records](docs/adr/README.md): why the main design choices were made

## Setup

Requires Python 3.11.

Windows (PowerShell):

```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env
```

macOS / Linux:

```
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Then set `APP_JWT_SECRET_KEY` in `.env`. It has no default and must be at least 32 characters:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Company data

`data/acmecorp` holds the company: 7 departments, 9 users and 17 documents. The documents are kept the way a company would have them: published policies and procedures as PDF, working papers as Word files, and engineering notes and a case system export as plain text. `manifest.json` lists them with each document's department, classification and named readers.

AcmeCorp is fictional. I wrote every document for this project, and none of the people or figures are real.

```
python -m scripts.seed
```

This creates the tables and loads everything, 57 chunks included. Documents go through the normal ingestion pipeline, uploaded by the knowledge admin, so labels and grants are checked by the same code as any other upload. Running it again adds nothing and does not reset passwords.

All seeded users share one password. Pass it with `--password`, or set `SEED_PASSWORD`; otherwise one is generated and printed once. The script refuses to run when `APP_ENVIRONMENT` is `production`.

| User | Department | Role |
|---|---|---|
| alice@acmecorp.example | Engineering | employee |
| bob@acmecorp.example | Finance | employee |
| carol@acmecorp.example | HR | employee |
| david@acmecorp.example | Sales | employee |
| emma@acmecorp.example | Legal | employee |
| frank@acmecorp.example | Operations | employee |
| grace@acmecorp.example | Executive | executive |
| henry@acmecorp.example | Engineering | knowledge admin |
| irene@acmecorp.example | Legal | security reviewer |

Three documents are company-wide, twelve belong to a department, and two are restricted: an HR case file that only Carol can read, and an acquisition paper readable by Grace and, for 60 days, by Emma.

## Vector index

```
python -m scripts.index build              # build a new version and make it current
python -m scripts.index status             # list versions; exit code 2 if out of date
python -m scripts.index rollback [VERSION] # go back to an earlier version
```

`build` embeds the chunks of every active document and writes a new version under `data/index/versions/`. The new version is loaded back and checked before the `current` pointer is moved to it, so a failed build leaves the previous one in use. The last three versions are kept (`APP_INDEX_KEEP_VERSIONS`). For the company data a version is 57 chunks and an 88 KB file. The first run downloads the embedding model.

Each version has a manifest with the embedding model, the chunk settings, a checksum of the index file and a fingerprint of the text that was embedded. Loading refuses a version built with a different model or chunk settings, or whose files don't match. `status` compares the fingerprint with the database and reports the index as out of date when documents have been added, replaced or deleted since it was built. A label or grant change doesn't make it out of date, because permissions are not in the index.

The index holds vectors of every document, restricted ones included. It is not committed, and it needs the same protection as the database.

## Audit trail

```
python -m scripts.audit check   # every record against its seal; exit code 2 if anything doesn't match
```

Every record is sealed with a keyed hash when it is written, so a record changed, added, deleted or moved by someone with the database but not the service's key is found. The check prints the highest record ID; the application log has each one too, which is what shows the newest records deleted. See [docs/audit.md](docs/audit.md) and [decision record 0013](docs/adr/0013-seal-each-audit-record.md).

## Running the API

```
uvicorn app.main:serve --factory --no-access-log
```

It needs the company data and an index (above), and refuses to start without a usable index. `GET /health` answers `{"status": "ok"}`; the interactive documentation is at `/docs`. Each person may ask 10 questions a minute and 200 a day, and search 30 times a minute ([how it counts](docs/api.md#how-often)). See [docs/api.md](docs/api.md).

## Running the API and the UI

One command starts both:

```
python -m scripts.run
```

It starts the API, waits until it answers its health check, then starts the UI pointed at it, and prints where to go: http://127.0.0.1:8501 by default. Ctrl+C stops both, and if either stops the other is stopped too. `--api-port`, `--ui-port` and `--host` change where they listen; both listen on this machine only unless told otherwise.

Sign in as any of the AcmeCorp people with the password given to `python -m scripts.seed`. Each person sees the pages their role allows: David asks and reads, Henry manages documents by ID without reading them, Irene reviews the audit trail. The UI only talks to the API; see [docs/ui.md](docs/ui.md).

To run them separately, start the API as above and, in a second terminal, `streamlit run ui/app.py`; the UI finds the API at `UI_API_URL`, `http://127.0.0.1:8000` by default.

## Docker

```
docker build --target api -t knowledge-api .
docker build --target ui  -t knowledge-ui  .
```

Two images: the API, with the embedding model built in so a container fetches nothing, and the UI, which holds none of the service because it only talks to the API. Both run as an unprivileged user and hold no secret; the database and index live on a volume.

To run both, with the company seeded and indexed on the first start:

```
python -m scripts.make_secrets
docker compose up --build
```

The first line makes the signing key, the fictional company's password and an empty model key as files in `secrets/`, which the containers read instead of their environment. The UI is then at http://127.0.0.1:8501, and the password is in `secrets/seed_password`. Each image has a health check: the UI is started once the API has loaded and answers, and `docker compose ps` shows whether both still do. What each container is given, and how they are hardened, is in [docs/docker.md](docs/docker.md); every setting is in [docs/configuration.md](docs/configuration.md).

## Checks

```
ruff check .
ruff format --check .
mypy
pytest --cov
```

To look for secrets in every tracked file and every commit, the local `.env`'s own values included (it prints where, never what):

```
python -m scripts.scan_secrets
```

Some tests generate their inputs with [Hypothesis](https://hypothesis.readthedocs.io): files damaged at random, questions, answers, titles, filters and requests to every route. The examples are the same on every run, so a failure can be run again.

To run only the security tests:

```
pytest tests/security
```

The tests that build and run the Docker images need Docker, and are left out of a normal run too: `pytest -m docker`.

The tests that load the real models are left out of a normal run, because loading them takes about half a minute. The first run downloads the embedding model and the reranker, about 90 MB each.

```
pytest -m model
```

Answers come from a language model chosen with `APP_LLM_PROVIDER` (see `.env.example`). The default, `none`, sends nothing anywhere. For a free model that runs on your own machine, install [Ollama](https://ollama.com), run `ollama pull llama3.1`, and set `APP_LLM_PROVIDER=ollama` and `APP_LLM_TIMEOUT_SECONDS=300`. The tests that ask the real local model are left out of a normal run too, because each answer takes about a minute on a laptop CPU:

```
pytest -m llm
```

For fast answers, Groq has a free tier: create a key at [console.groq.com](https://console.groq.com), put it in your `.env` as `APP_LLM_API_KEY`, and set `APP_LLM_PROVIDER=groq` and `APP_LLM_MAX_OUTPUT_TOKENS=1000`. The prompt, which is document text, then goes to Groq.

## Stack

In use now: Python 3.11, FastAPI (with python-multipart for uploads), SQLAlchemy (SQLite in tests), Argon2 password hashing, PyJWT, pypdf, python-docx, sentence-transformers with the `all-MiniLM-L6-v2` embedding model (and the `ms-marco-MiniLM-L6-v2` cross-encoder for optional reranking), FAISS, NumPy, pydantic-settings, Streamlit and httpx for the UI, pytest, ruff, mypy.

Docker for the images. Planned: PostgreSQL as the target database. Libraries are added to the requirements files when the part that needs them is built.
