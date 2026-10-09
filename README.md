# Permission-Aware Enterprise Knowledge System

A question-answering system over internal company documents where a user can only get answers built from documents they are allowed to read.

Most RAG examples put every document into one vector index, which means anyone can retrieve anything by asking the right question. This project is about not doing that. Permissions are applied during retrieval, before anything is sent to the LLM, so the model never sees content the user shouldn't.

```
User -> Auth -> Identity -> Authorization -> Filtered retrieval -> Context -> LLM -> Answer + citations
```

The setting is AcmeCorp, a fictional company with seven departments. All documents and users are synthetic.

## Status

Work in progress. Identity, authorization and document ingestion are built and tested. There is no chunking, retrieval or API yet, so documents can be stored with their permissions but nothing is searchable.

- [x] Requirements, personas, architecture, threat model, decision records
- [x] Project structure, settings, logging, error types, lint and type checks
- [x] Users, departments, roles and authentication
- [x] Authorization: roles, document classification, grants, and tests for the full access matrix
- [x] Document ingestion: text, PDF and Word files, cleaning, file details, versions and deletion
- [ ] Chunking, embeddings and the vector index
- [ ] Permission-aware retrieval
- [ ] Answer generation with citations
- [ ] API
- [ ] UI
- [ ] Evaluation, including permission leakage tests
- [ ] Docker and CI

Nothing has been measured yet, so there are no quality or latency numbers here.

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

## Stack

In use now: Python 3.11, FastAPI, SQLAlchemy (SQLite in tests), Argon2 password hashing, PyJWT, pypdf, python-docx, pydantic-settings, pytest, ruff, mypy.

Planned: PostgreSQL as the target database, FAISS, sentence-transformers, Streamlit, Docker. Libraries are added to the requirements files when the part that needs them is built.
