# 0003. Pre-filter the vector search, then check results again

Status: Accepted
Date: October 2026

## Context

Given the user's access scope (0002), there are two points at which the retriever can apply it: before the similarity search or after it.

## Decision

Before. The retriever gets the IDs of the chunks inside the user's scope and restricts the similarity search to those IDs.

After the search, each returned chunk is checked once more against the authorization module before it leaves the retriever. This second check is expected to remove nothing. If it removes a chunk, the chunk is dropped and an error is logged, because the first filter has a bug.

## Alternatives

- Post-filter only: search the whole index, then drop what the user can't read. Simpler, and the usual first attempt. Rejected for two reasons. Unauthorized chunks occupy places in the top K, so a user with narrow access gets fewer or worse results, and asking for a larger K to compensate is guesswork. And unauthorized content sits in memory one bug away from the context.
- One index per department. The search then physically can't touch another department's vectors. Rejected for now because access is not only by department (general documents, executives, possible per-document grants), so queries would fan out over several indexes and still need a filter inside each.
- Pre-filter only, without the second check. Rejected. The second check is cheap, since it runs on a handful of chunks, and it turns a silent leak into a logged error.

## Consequences

- Result quality doesn't depend on how much of the corpus the user can see.
- A leak needs two independent checks to fail.
- Passing a list of allowed IDs per query is fine for a small corpus and will not scale to large ones.
- Needs to be proven against the FAISS index type actually used, as the first task when retrieval is built.

## Revisit when

The allowed-ID list per query becomes large enough to hurt latency. The replacement is a store that filters inside the search: pgvector, or a vector database with metadata filtering.
