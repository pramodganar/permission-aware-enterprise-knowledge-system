# Architecture decision records

One file per decision that would be expensive to change later. Each has the context, what I decided, what else I considered, what it costs, and when I'd revisit it.

A decision is not edited after it is accepted. If it changes, a new record replaces it and the old one is marked superseded.

| No. | Decision | Status |
|---|---|---|
| [0001](0001-enforce-permissions-before-the-llm.md) | Enforce permissions before the LLM, never in the prompt | Accepted |
| [0002](0002-permissions-in-the-database.md) | Keep permissions in the database, not in the vector index | Accepted |
| [0003](0003-pre-filter-vector-search.md) | Pre-filter the vector search, then check results again | Accepted |
| [0004](0004-identity-only-token.md) | Token carries identity only; user is loaded on every request | Accepted |
| [0005](0005-same-response-for-no-access-and-not-found.md) | Same response for "no access" and "not found" | Accepted |
| [0006](0006-fail-request-when-audit-fails.md) | Fail the request when the audit record can't be written | Accepted |
| [0007](0007-faiss-and-sqlite-locally.md) | FAISS and SQLite locally, behind interfaces | Accepted |
| [0008](0008-single-service.md) | One service with separate modules | Accepted |
| [0009](0009-local-embeddings-configurable-llm.md) | Local embeddings, configurable LLM provider | Accepted |
| [0010](0010-restricted-documents-need-a-grant.md) | Restricted documents need a named grant, even for executives | Accepted |
| [0011](0011-pack-lines-into-bounded-chunks.md) | Pack lines into size-bounded chunks with overlap | Accepted |
| [0012](0012-reranking-off-by-default.md) | Build reranking, but leave it off by default | Accepted |
| [0013](0013-seal-each-audit-record.md) | Seal each audit record with a keyed hash, rather than chaining them | Accepted |
