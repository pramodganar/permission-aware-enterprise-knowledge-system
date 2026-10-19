# Permission-Aware Enterprise Knowledge System

A question-answering system over internal company documents where a user can only get answers built from documents they are allowed to read.

Most RAG examples put every document into one vector index, which means anyone can retrieve anything by asking the right question. This project is about not doing that. Permissions are applied during retrieval, before anything is sent to the LLM, so the model never sees content the user shouldn't.

```
User -> Auth -> Identity -> Authorization -> Filtered retrieval -> Context -> LLM -> Answer + citations
```

The setting is AcmeCorp, a fictional company with seven departments. All documents and users are synthetic.

**Live demo:** [https://permission-aware-enterprise-knowledge-system-ij67laxbihrkmrqfw.streamlit.app/](https://permission-aware-enterprise-knowledge-system-ij67laxbihrkmrqfw.streamlit.app/). Pick who you are, ask a question, or ask the same question as two people side by side ([how it works](docs/demo.md)).

## Status

Work in progress. Identity, authorization, document ingestion, permission-aware retrieval and answer generation are built and tested, and the AcmeCorp company data can be loaded with one command. The HTTP API is being built: logging in, listing and opening documents, searching, asking, managing documents and the audit trail work so far.

- [x] Requirements, personas, architecture, threat model, decision records
- [x] Project structure, settings, logging, error types, lint and type checks
- [x] Users, departments, roles and authentication
- [x] Authorization: roles, document classification, grants, and tests for the full access matrix
- [x] Document ingestion: text, PDF and Word files, cleaning, file details, versions and deletion
- [x] Company data: 17 documents in PDF, Word and text, 9 users, and a script that loads them
- [x] Chunking, embeddings and the vector index, with versions and rollback
- [x] Permission-aware retrieval: access scope, filters, relevance threshold, optional reranking, and end-to-end boundary tests
- [x] Answer generation with citations, measured on two real models, and end-to-end answer boundary tests
- [ ] API
- [ ] UI (a demo page is done early: [docs/demo.md](docs/demo.md))
- [ ] Evaluation, including permission leakage tests
- [ ] Docker and CI

Measurements so far are in [retrieval](docs/retrieval.md) and [generation](docs/generation.md). End-to-end quality and latency numbers come with the evaluation.

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
- [Demo page](docs/demo.md): pick a person, ask, compare two people; how to run and deploy it
- [Threat model](docs/threat-model.md): what could go wrong and what is left uncovered
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

## Running the API

```
uvicorn app.main:serve --factory --no-access-log
```

It needs the company data and an index (above), and refuses to start without a usable index. `GET /health` answers `{"status": "ok"}`; the interactive documentation is at `/docs`. See [docs/api.md](docs/api.md).

## Checks

```
ruff check .
ruff format --check .
mypy
pytest --cov
```

To run only the security tests:

```
pytest tests/security
```

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

In use now: Python 3.11, FastAPI (with python-multipart for uploads), SQLAlchemy (SQLite in tests), Argon2 password hashing, PyJWT, pypdf, python-docx, sentence-transformers with the `all-MiniLM-L6-v2` embedding model (and the `ms-marco-MiniLM-L6-v2` cross-encoder for optional reranking), FAISS, NumPy, pydantic-settings, pytest, ruff, mypy.

Planned: PostgreSQL as the target database, Streamlit, Docker. Libraries are added to the requirements files when the part that needs them is built.
