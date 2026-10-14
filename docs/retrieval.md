# Retrieval

How a question becomes a handful of chunks, and how the relevance threshold was chosen.

## The path

1. The question is checked, cleaned and embedded (`app/retrieval/query.py`).
2. The person's access scope is worked out from the database (`scope.py`).
3. The index is searched only inside that scope (`search.py`).
4. The chunks found are loaded and each one's document is checked again with `can_read` (`retriever.py`).
5. If the best of the remaining chunks scores below the threshold, nothing is returned.

The person gets up to `APP_RETRIEVAL_TOP_K` chunks (default 5), best first. An empty result, for whatever reason, leads to the same "no information available" answer, and the LLM is not called.

## The relevance threshold

Scores are cosine similarities between the question and a chunk, from -1 to 1. Without a threshold, every question gets five chunks, however unrelated. When David asks how executive bonuses are calculated, he would get five chunks of the discount policy and the leave policy, and the LLM would be asked to answer from them.

### What I measured

The real model (all-MiniLM-L6-v2), the 17 AcmeCorp documents, and the default chunking. Each answerable question is asked by someone who may read the document that answers it, the narrowest reader where there is a choice. Off-topic questions are asked by Grace, who can read 16 of the 17 documents, so almost the whole company had a chance to match.

| Group | Questions | Best score: min | median | max |
|---|---|---|---|---|
| Answerable: 17 worded like the documents, 5 worded differently ("What happened when payments went down?") | 22 | **0.424** | 0.656 | 0.796 |
| Off-topic ("What is the capital of France?", "Tell me a joke.") | 11 | 0.080 | 0.150 | **0.306** |

The highest off-topic score is "What is the share price today?" at 0.306, matching the acquisition paper. The lowest answerable one is "Can I accept a gift from a supplier?" at 0.424, matching the code of conduct.

I chose **0.35**, roughly in the middle of the gap. The margins are small: 0.044 above the worst off-topic question, 0.074 below the worst answerable one. Twenty-two and eleven questions are not many, so this is a starting value to be checked against real questions. The best score is logged with every retrieval for that reason.

David's bonus question scores 0.330 in his own documents, so he now gets nothing, and the LLM is not called.

### Why the gate is on the best chunk, not on each chunk

The usual approach is to drop every chunk below the threshold. On this data, that removes real answers. Carol asks "Which employees are on a performance improvement plan?" Her top five are:

| Rank | Score | Document |
|---|---|---|
| 1 | 0.643 | Performance Improvement Plan Process |
| 2 | 0.577 | Performance Improvement Plan Process |
| 3 | 0.522 | Performance Improvement Plan Process |
| 4 | 0.362 | Salary Bands 2026 |
| 5 | 0.347 | HR Case File 2026-014 |

The case file is the document that actually names an employee on a plan, and it scores below 0.35. The embedding model matches the process document's wording better. A per-chunk cut-off would drop it. So the threshold answers one question, "is anything here about this at all?", and when the answer is yes, the top five are returned as they are. Putting the right chunk first is the reranker's job.

The gate is applied after the second authorization check. A forbidden chunk that slipped through with a high score must not be what decides that the person gets an answer.

### What the threshold can't do

"What is our customer data retention design?" scores 0.588 against the contract retention obligations. No document describes a design, but the obligations are close in meaning. The threshold can tell "about nothing here" from "about something here". It can't tell "about something here" from "answered here". That falls to the generation step: the model is told to answer only from the context and to say when the context does not answer the question.

The same is true when the person can't read the answer. David asking about vendor payment approval limits scores 0.532 against his discount policy, which is also about approval limits. He gets his own document, never the finance one, but the LLM will have to say it does not cover vendor payments.

### Keeping it honest

`tests/integration/test_relevance_gate.py` runs the measurements above with the real model (`pytest -m model`). It fails if any of the 22 answerable questions is stopped, if any of the 11 off-topic questions gets through, if David's bonus question gets an answer, or if Carol's case-file chunk is lost. It also keeps the retention-design question as a test, so the known limit stays known.

The threshold only means something for the model it was measured with. Changing `APP_EMBEDDING_MODEL` means measuring again.
