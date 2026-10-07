# 0002. Keep permissions in the database, not in the vector index

Status: Accepted
Date: October 2026

## Context

Retrieval needs to know which chunks a user may read. That information could sit next to each vector as metadata, or in the relational database with the documents.

Permissions change: roles are reassigned, documents are reclassified, people leave. Those changes have to take effect quickly and without rebuilding anything.

## Decision

The database is the only place permissions are stored. The vector index holds vectors and chunk IDs. A chunk has no permissions of its own and takes them from its document.

## Alternatives

- Store department, classification and allowed roles as metadata on each vector. This is the usual approach with vector databases that filter natively. Rejected for now because FAISS has no metadata store, so I would be maintaining a second copy of the permissions that can drift from the first.
- Store permissions per chunk in the database. Rejected. A document with fifty chunks would need fifty updates per change, and a missed one leaves a chunk more open than its document.

## Consequences

- A permission change is one row update and applies on the next query. No re-embedding, no index rebuild.
- One source of truth, so there is nothing to keep in sync.
- Every query needs a database lookup to work out the allowed chunks before the vector search.
- The index file alone says nothing about who may read what. It is still sensitive because of the vectors.

## Revisit when

The vector store is replaced by one that filters on metadata natively (see 0003). Even then the database stays the source of truth and the store holds a synced copy.
