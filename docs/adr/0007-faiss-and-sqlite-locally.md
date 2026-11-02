# 0007. FAISS and SQLite locally, behind interfaces

Status: Accepted
Date: October 2026

## Context

The project runs on one machine with a corpus of tens of documents. It needs a vector index and a relational store. The architecture targets PostgreSQL, and the vector store is expected to change at larger scale.

## Decision

Locally, vectors go in a FAISS index saved to disk and relational data goes in SQLite through SQLAlchemy. Both sit behind interfaces, and which one is used is a configuration setting.

The FAISS index is a flat (exact) index. With this corpus size there is no reason to accept approximate results.

## Alternatives

- pgvector in PostgreSQL from the start. Attractive because the permission join and the vector search become one SQL query. Not chosen first because it needs a running Postgres for every test run, and because doing the filter by hand with FAISS makes the mechanism explicit. It is the most likely replacement.
- A dedicated vector database (Qdrant, Weaviate, Chroma). Native metadata filtering, but another service to run, and filtering would rely on a copy of the permissions (see 0002).
- PostgreSQL only, no SQLite. Closer to production. Deferred so that the tests need no external service.

## Consequences

- Runs with no external services, so tests are fast and self-contained.
- The permission filter is my code, not a vendor feature, so I have to test it thoroughly.
- SQLite and PostgreSQL differ in places (types, concurrency). The schema has to stay within what both support, and the integration tests should also run against PostgreSQL.
- FAISS has no built-in way to update or delete in place for every index type, so removed chunks are excluded by the scope query and dropped at the next rebuild.

## Revisit when

The corpus outgrows exact search, or the allowed-ID list becomes too large (see 0003).

## Corrections

November 2026. The decision stands, but three things this record said about the system aren't true of it as built:

- Under Alternatives it said the Docker Compose setup uses PostgreSQL, and that sentence is taken out. Compose doesn't run PostgreSQL: it runs SQLite on the `data` volume, like everything else here. Nothing has run against PostgreSQL.
- So the integration tests don't run against PostgreSQL either, and some of the database code is written for SQLite only: the triggers that keep the audit trail append-only (`app/models/audit.py`) and the connection settings (`app/core/database.py`). Moving to PostgreSQL means writing those again and running the tests there.
- The vector index is behind an interface, `VectorIndex` in `app/retrieval/index.py`, but FAISS is its only implementation and no setting chooses it. Only the database is chosen by a setting, `APP_DATABASE_URL`.
