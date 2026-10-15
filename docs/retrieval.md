# Retrieval

How a question becomes a handful of chunks, how the relevance threshold was chosen, and why reranking is off by default.

## The path

1. The question is checked, cleaned and embedded (`app/retrieval/query.py`).
2. The person's access scope is worked out from the database (`scope.py`).
3. If the person asked for filters, the scope is narrowed to match them (`filters.py`).
4. The index is searched only inside that scope (`search.py`).
5. The chunks found are loaded and each one's document is checked again with `can_read` (`retriever.py`).
6. If the best chunk the person may read scores below the threshold, nothing is returned.
7. If reranking is on, a cross-encoder reorders what is left.

The person gets up to `APP_RETRIEVAL_TOP_K` chunks (default 5), best first. An empty result, for whatever reason, leads to the same "no information available" answer, and the LLM is not called.

## Filters

A person can narrow a question to some departments, classifications or document IDs: "only HR documents", "only this policy". Values in one field are alternatives (finance or legal); different fields must all hold (finance, and general).

A filter is input from the person, so it is treated like the question: it can only take chunks away from their scope, never add any. Two things make sure of that, and either one alone would be enough:

- The filter query only looks at documents already in the scope.
- Its result is intersected with the scope's chunk IDs.

A test removes each guard in turn and checks the other still holds, because a test of the overall result alone can't tell if one of them is missing.

Asking for something you may not read is not an error. Filtering by another department's document, or by the executive department, gives exactly the same empty result as filtering by an ID or a department that doesn't exist. Otherwise a filter would be a way to find out which documents exist. Filter values are not logged, only which kinds of filter were used, because "document 16" or "hr" says what the person was looking for.

Filters can also improve results for people who can read a lot. Grace asking about bonuses with an HR filter gets five HR chunks; the executive paper, which matches better, doesn't take any of the places.

Limits: up to 20 departments and 50 document IDs. An empty list is refused rather than guessed at, since it could mean "no filter" or "nothing".

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

The case file is the document that actually names an employee on a plan, and it scores below 0.35. The embedding model matches the process document's wording better. A per-chunk cut-off would drop it. So the threshold answers one question, "is anything here about this at all?", and when the answer is yes, the top five are returned as they are.

The gate is applied after the second authorization check. A forbidden chunk that slipped through with a high score must not be what decides that the person gets an answer.

### Filters and the gate

With a filter, the gate looks at the best chunk in everything the person may read, not only in what the filter left. I found this with the real model after adding filters. Carol asked the same question with a filter for restricted documents and got nothing: the filter removed the process document, which was what made the question relevant, and left the case file, whose best chunk is 0.347. She had asked for exactly that document and was told there was no information.

So when a filter is used, a second search finds the single best chunk in the person's whole readable scope, checks it with `can_read` like any other, and the gate uses the better of the two scores. The second search stays inside what the person may read, so the gate still only depends on their own documents. If the filter leaves nothing, the answer is nothing, whatever the wider search finds.

The cost: a filtered question can now return weak chunks when the question is relevant to the person's documents but not to the part they filtered to. The model has to say the context doesn't answer it, the same as for the known limit below.

### What the threshold can't do

"What is our customer data retention design?" scores 0.588 against the contract retention obligations. No document describes a design, but the obligations are close in meaning. The threshold can tell "about nothing here" from "about something here". It can't tell "about something here" from "answered here". That falls to the generation step: the model is told to answer only from the context and to say when the context does not answer the question.

The same is true when the person can't read the answer. David asking about vendor payment approval limits scores 0.532 against his discount policy, which is also about approval limits. He gets his own document, never the finance one, but the LLM will have to say it does not cover vendor payments.

### Keeping it honest

`tests/integration/test_relevance_gate.py` runs the measurements above with the real model (`pytest -m model`). It fails if any of the 22 answerable questions is stopped, if any of the 11 off-topic questions gets through, if David's bonus question gets an answer, or if Carol's case-file chunk is lost. It also keeps the retention-design question as a test, so the known limit stays known, and checks that Carol's filtered question returns the case file.

The threshold only means something for the model it was measured with. Changing `APP_EMBEDDING_MODEL` means measuring again.

## Reranking

A cross-encoder reads the question and one chunk together and scores how well the chunk answers it. It is usually more precise than comparing two vectors made separately, and much slower. It is built and tested, and off by default ([decision record 0012](adr/0012-reranking-off-by-default.md)).

### What I measured

cross-encoder/ms-marco-MiniLM-L6-v2, locally on the CPU, reranking the 20 closest chunks for each of the 22 answerable questions:

| | Search only | Search and reranking |
|---|---|---|
| Answer document ranked first | 21 of 22 | 21 of 22 |
| Carol's case file | 5th | 4th |
| Gap between worst answerable and best off-topic, share of the score range | 16.5% (0.424 vs 0.306) | 1.6% (-7.50 vs -7.82) |
| Retrieval time per question, median | 34 ms | 971 ms |

With 17 documents, the search already puts the right document first for 21 of 22 questions. The miss is the case-file question, where both put the process document first. The reranker moved the case file up one place and added nearly a second per question. The times are from one run on a laptop under varying load; an earlier run of the reranker alone took about 600 ms for 20 chunks.

Its scores would also make a poor relevance threshold. On its own scale, the worst answerable question and the best off-topic one are almost level, so the gate stays on the search score even when reranking is on.

### How it fits in

When `APP_RERANKER_MODEL` names a model, the retriever searches for `APP_RERANK_CANDIDATES` chunks (default 20), runs both access checks and the gate, then lets the reranker reorder what is left and returns the best five. The reranker:

- only ever sees chunks that passed both access checks, so restricted text never reaches it;
- changes the order, never which chunks there are;
- is not called when the gate stops the question or the scope is empty;
- reads each chunk as it was embedded, title first.

A cross-encoder cuts what doesn't fit without saying so. With the default models, the longest allowed question and the longest chunk fit together (a real-model test checks it), but another model might read less. If a pair doesn't fit, that question keeps the search's order and a warning is logged. Reranking is an improvement, not a requirement, so it isn't worth failing the question over.

`tests/integration/test_reranking_model.py` repeats the measurements above with the real models, so the decision can be checked again when the documents or the models change.


## The security boundary, tested end to end

`tests/security/test_retrieval_boundaries.py` tests retrieval from the outside, through `retrieve`, with an index built once that holds every chunk of the company, restricted ones included. Every check counts a chunk as leaked if the person may not read its document, or if its text isn't the stored text.

- **Every person, every question, every filter.** 9 people, 17 questions and 29 ways of filtering (none, each of the 7 departments, each of the 3 classifications, each of the 17 documents, and an ID that doesn't exist): 4,437 retrievals, 3,103 of them asking about a document the person may not read. No leaks, and the second check never had to remove anything. Over the same run, no question, title or chunk text reached the logs.
- **With reranking on.** The reranker only ever read chunks of the person's own documents.
- **Hostile questions.** Eight questions that try to talk past the rules ("ignore all previous instructions", "I am Grace, an executive", an SQL fragment, filter syntax typed into the question, invisible tag characters) change nothing for anyone.
- **Forbidden looks like missing.** For every person, filtering by each document they may not read gives a result identical to filtering by an ID that doesn't exist, and the same for departments they read nothing of.
- **Changes take effect on the next question, with the index unchanged.** A revoked grant, an expired grant, a new grant, a deactivated account, a demoted executive, an employee moving department, a document reclassified as restricted, a document moved to another department, a deleted document, and a replaced document whose old vectors are still in the index.
- **Failures close the door.** If the scope query, the second check or the index fails, the question fails. A failing check is never taken as a yes.
- **Scopes can't be kept.** A scope from an hour ago, as a cache would hold, is refused.

Every retrieval in the suite must also have needed nothing from the second check. I added that after breaking the SQL rule on purpose: with grant expiry or document status removed from it, nobody saw anything they shouldn't, because `can_read` caught it, and so no test failed. The safety net was working and hiding that the first line had broken. Now the suite fails as soon as the second check has to step in.

The test for kept scopes found a bug. The retriever passed the scope's own time to the search as "now", so the scope's age always came out as zero and the 60-second limit could never trigger through the retriever. The search's own tests called it directly with a real time, so they didn't notice. The retriever now lets the search use the current time.
