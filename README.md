# Permission-Aware Enterprise Knowledge System

A question-answering system over internal company documents where a user can only get answers built from documents they are allowed to read.

Most RAG examples put every document into one vector index, which means anyone can retrieve anything by asking the right question. This project is about not doing that. Permissions are applied during retrieval, before anything is sent to the LLM, so the model never sees content the user shouldn't.

```
User -> Auth -> Identity -> Authorization -> Filtered retrieval -> Context -> LLM -> Answer + citations
```

The setting is AcmeCorp, a fictional company with seven departments. All documents and users are synthetic.

## Status

Work in progress. Identity, authorization and document ingestion are built and tested, and the AcmeCorp company data can be loaded with one command. There is no chunking, retrieval or API yet, so documents can be stored with their permissions but nothing is searchable.

- [x] Requirements, personas, architecture, threat model, decision records
- [x] Project structure, settings, logging, error types, lint and type checks
- [x] Users, departments, roles and authentication
- [x] Authorization: roles, document classification, grants, and tests for the full access matrix
- [x] Document ingestion: text, PDF and Word files, cleaning, file details, versions and deletion
- [x] Company data: 17 documents in PDF, Word and text, 9 users, and a script that loads them
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

## Company data

`data/acmecorp` holds the company: 7 departments, 9 users and 17 documents. The documents are kept the way a company would have them: published policies and procedures as PDF, working papers as Word files, and engineering notes and a case system export as plain text. `manifest.json` lists them with each document's department, classification and named readers.

AcmeCorp is fictional. I wrote every document for this project, and none of the people or figures are real.

```
python -m scripts.seed
```

This creates the tables and loads everything. Documents go through the normal ingestion pipeline, uploaded by the knowledge admin, so labels and grants are checked by the same code as any other upload. Running it again adds nothing and does not reset passwords.

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
