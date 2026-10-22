# 0012. Build reranking, but leave it off by default

Status: Accepted
Date: October 2026

## Context

The plan, and requirement FR-9, said retrieved chunks would be reranked: a cross-encoder reads the question and each candidate chunk together and reorders them. That is common advice for RAG systems, because a bi-encoder search compares vectors made separately and can miss which chunk actually answers the question.

Before building it into the default path I measured it on the AcmeCorp documents, with cross-encoder/ms-marco-MiniLM-L6-v2 running locally on the CPU, reranking the 20 closest chunks for each of the 22 answerable questions used to set the relevance threshold.

| | Search only | Search and reranking |
|---|---|---|
| Answer document ranked first | 21 of 22 | 21 of 22 |
| Carol's case file, "which employees are on a plan?" | 5th | 4th |
| Gap between worst answerable and best off-topic question, as a share of the score range | 16.5% | 1.6% |
| Retrieval time per question, median of 22 | 34 ms | 971 ms |

With 17 documents the search already finds the right document first almost every time. The reranker added nearly a second per question for one place of improvement on one question. Its scores would also make a much weaker relevance threshold than the search's cosine scores.

## Decision

Reranking is built and tested, and switched off by default (`APP_RERANKER_MODEL` unset).

When it is on, it reorders the chunks that passed both access checks and the relevance gate. It never adds or removes a chunk, and it never sees a chunk the person may not read. The relevance gate stays on the search score whether reranking is on or not.

If a question and a chunk together are longer than the reranker reads, that question is not reranked and the search order is kept, with a warning. A cross-encoder would otherwise cut the chunk without saying so.

FR-9 is reworded to match: chunks can be reranked, and whether they are is decided by measurement.

## Alternatives

- Rerank by default, as planned. Rejected for now: nearly a second per question for no measured gain.
- Don't build it. Rejected: on a larger corpus, with many similar documents, the order of the results is where a reranker usually helps, and the measurement should be easy to repeat. The real-model tests do that.
- Use the reranker's score for the relevance gate. Rejected on the measurement above: the gap between answerable and off-topic questions almost disappears on its scale.
- An LLM as reranker. More expensive again, and it would send chunk text to a provider before the answer step, which the design avoids (SR-13).

## Consequences

- Default retrieval stays fast and needs one model, not two.
- Turning reranking on is a setting, plus a model download the first time. Turning it on in production should come with the same measurement on that corpus.
- Twenty-two questions on 17 documents is a small test. The decision is about this corpus, not about reranking in general.

## Revisit when

The corpus grows enough that the right document is often not first, the evaluation set in phase 11 shows ranking problems, or a GPU is available.

## Revisited in phase 11

The evaluation set measured both sides again, on 34 answerable questions by chunk rather than by document, and on all 73 questions for time ([evaluation](../evaluation.md#latency)):

| | Search only | Search and reranking |
|---|---|---|
| Chunk with the answer ranked first | 23 of 34 | 25 of 34 |
| Chunk with the answer in the five given to the model | 33 of 34 | 34 of 34 |
| Answer document ranked first | 33 of 34 | 33 of 34 |
| Retrieval, p50 / p95 | 6.7 / 7.4 ms | 573 / 818 ms |

The gain is real and small: two questions get their answer chunk first, and one, Carol's case-file question, gets it into the model's sources at all, which might let the model answer it (not measured). Four questions move down as well. The cost is the reranker's 711 ms median on this laptop's CPU, on every question that passes the gate, which takes retrieval's p95 over the 500 ms target in NFR-1 by itself.

**The decision stands: off by default.** One question in 34 doesn't pay for missing the retrieval target on all of them. The case-file question is better served where it fails, in which chunks are retrieved, than by reranking everything.

Measuring this also found that timings taken while the machine does other work are not to be trusted: a first run, with type checking going on at the same time, put the reranker at 1,436 ms, twice the clean figure.
