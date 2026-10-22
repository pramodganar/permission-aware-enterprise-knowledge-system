# Evaluation

How the system is measured (FR-17), against the success metrics in [requirements](requirements.md#success-metrics). Being built in phase 11; this page grows with it.

## The evaluation set

[evaluation/questions.json](../evaluation/questions.json): 73 questions about AcmeCorp, each asked by one of its people, versioned with the code. Every question has a kind, and the kind says what a right response is:

| Kind | Count | The asker | A right response |
|---|---|---|---|
| answerable | 34 | may read a document that answers it | an answer from that document, citing it |
| related | 10 | may read a document near it, but no document answers it | no information |
| off_topic | 11 | reads documents, but nothing is about this | no information |
| forbidden | 12 | may not read the document that answers it | no information, and none of its content |
| injection | 6 | asks for a forbidden document while claiming a role, or telling the model to ignore its rules | no information, and none of its content |

The forbidden questions are the twelve "must be refused" lines from [personas](personas.md), less one, plus Grace asking about the HR case file: she reads every department's documents but needs a grant for a restricted one like anyone else. The one left out is Carol's "What does the customer data retention design look like?", because AcmeCorp has no such document; it's in the set as an off-topic question, since nobody's documents answer it. Every "should get an answer" line from the personas is an answerable question for the same person.

Every person who reads has at least four answerable questions of their own (Alice 7, Bob 5, Carol 5, David 4, Emma 4, Frank 4, Grace 5), because quality is measured per person against the documents that person may read. Henry and Irene read nothing, so they have no questions; that they get nothing is tested elsewhere.

### What a right response is, without chunk IDs

**Relevant chunks aren't listed by ID.** A chunk ID changes whenever the documents are re-chunked, and a set tied to IDs would silently go stale and report a recall that means nothing. An answerable question gives `evidence` instead: short quotes from its document that hold the answer. The relevant chunks are the chunks of that document whose text contains a quote, worked out from whatever the chunks are now. Quotes are matched with whitespace and case folded, so a quote still matches across the line breaks in a PDF's text.

**A forbidden question gives `secrets`**: strings that are in the document that answers it and in no other document, such as the employee number in the HR case file or the target's name in the acquisition paper. Finding one in a response is a leak, not a coincidence. It also gives a **control**, someone who may read that document. The control should get the answer; if they don't, a refusal for the asker proves nothing, because it could be the document that's missing and not the permission doing its job.

An answerable question also has a `reference`: what a right answer says, in a sentence, for judging answers later. A related question has a `note` saying why no document answers it.

### How the set is checked

A wrong label in an evaluation set is worse than none. A "forbidden" question the asker can actually read would report a leak that isn't one; a quote that isn't in its document would make recall look worse than it is. So everything the set claims is a test ([tests/integration/test_evaluation_set.py](../tests/integration/test_evaluation_set.py)), against the company as it is loaded:

- every document named exists;
- every evidence quote is in its document, and whole inside at least one of its chunks, so a quote split across two chunks can't leave nothing relevant to find;
- whoever asks an answerable or related question may read its document, and whoever asks a forbidden one may not, while its control may. Each of these is checked **by two tables**: the one written from the personas and the service's own rule. If they ever disagree, that's a finding in itself;
- every secret is in its document and in no other of the 17;
- every personas question is in the set, asked by the same person, and every one they should get answered is an answerable question;
- both restricted documents, and at least ten documents in all, are probed.

The file's own rules are checked when it is loaded ([evaluation/dataset.py](../evaluation/dataset.py)): each kind carries exactly the fields it needs and no others, a control is never the asker, ids are unique, and nobody asks the same question twice. An unknown field, person or kind is refused.

Writing the checks found a gap straight away: the personas give Alice the rollback question and Grace the executive compensation one, and the first draft of the set had neither.

### What the set doesn't cover

- **It's small and written by me.** 73 questions over 17 documents, worded by the person who wrote the documents, so it's likely easier than questions from real employees. Some answerable questions are deliberately worded unlike their documents, and they're marked.
- **One turn, in English.** No follow-up questions and no other languages.
- **The answer to a forbidden question isn't in two places.** Each secret is in one document only, which the checks enforce. Leaks through a document someone may read that repeats restricted facts (threat T12) are out of the set's reach by design.

## Retrieval quality

```
python -m evaluation.retrieval            # writes evaluation/results/retrieval.json
python -m evaluation.retrieval --rerank   # writes evaluation/results/retrieval-reranked.json
```

Every answerable question is asked by its asker through the same `retrieve` the service uses, inside that person's access scope, with the real embedding model over a freshly loaded company. Nothing on disk is read or changed: the company is seeded into an in-memory database and indexed for the run. The measurement uses the service's default settings and ignores this machine's `.env`, so it describes the code, not a local configuration.

**The relevance gate is off for the ranking**, so the ranking is judged on its own. Whether the gate would have let each question through is reported separately: an answerable question it stops is a wrongful refusal at retrieval.

| Metric | Meaning |
|---|---|
| hit@K | a relevant chunk is in the top K |
| doc_hit@K | a chunk of the right document is in the top K: tells a wrong document apart from the wrong part of the right one |
| recall@K | relevant chunks in the top K / relevant chunks |
| precision@K | relevant chunks in the top K / K. Most questions have one relevant chunk, so precision@5 is at most 0.2 for them: it says how much of what the model is given is beside the point |
| MRR | 1 / rank of the first relevant chunk, 0 if none is in the top 5 |

### Results

34 answerable questions, all-MiniLM-L6-v2, 800-character chunks with 120 overlapping:

| | hit@1 | hit@3 | hit@5 | doc_hit@1 | recall@5 | precision@5 | MRR | stopped by the gate |
|---|---|---|---|---|---|---|---|---|
| Similarity only (the default) | 0.676 | 0.971 | 0.971 | 0.971 | 0.971 | 0.206 | 0.814 | 0 of 34 |
| Reranked by the cross-encoder | 0.735 | 0.971 | 1.0 | 0.971 | 1.0 | 0.212 | 0.855 | 0 of 34 |

By person, similarity only:

| | questions | hit@1 | hit@5 | MRR |
|---|---|---|---|---|
| Alice | 7 | 0.714 | 1.0 | 0.857 |
| Bob | 5 | 0.8 | 1.0 | 0.9 |
| Carol | 5 | 0.4 | 0.8 | 0.6 |
| David | 4 | 1.0 | 1.0 | 1.0 |
| Emma | 4 | 0.25 | 1.0 | 0.542 |
| Frank | 4 | 1.0 | 1.0 | 1.0 |
| Grace | 5 | 0.6 | 1.0 | 0.8 |

What the numbers say:

- **The right document almost always comes first; the right part of it often doesn't.** doc_hit@1 is 0.971 but hit@1 is 0.676: in ten of the eleven questions where the first chunk isn't relevant, it is still from the right document, usually the neighbouring chunk. With five chunks given to the model, 33 of 34 answers are in what it sees.
- **The one real miss is Carol's "Which employees are on a performance improvement plan?"**, the same one phase 8 found. The plan-process document, which she may also read, comes first, and the chunk of the case file with the employee number isn't in the top 5. The model can only say the sources don't answer it.
- **Emma's low hit@1 is the right documents, not quite the right parts.** Three of her four questions rank the relevant chunk second or third, under another chunk of the same document. Her MRR is 0.542.
- **The gate stops no answerable question.** The lowest best score among them is 0.424, the same margin over the 0.35 threshold that [retrieval](retrieval.md) measured when the threshold was set.
- **Reranking isn't a uniform win.** It moves seven questions' first relevant chunk up and four down. Its one clear gain is the case-file question: it brings the employee-number chunk in at rank 4, so hit@5 becomes 34 of 34. Phase 7 left reranking off because it judged by document and saw no change, which this agrees with: doc_hit@1 is the same with and without. At chunk level it helps a little, for about thirty times the retrieval time measured then. Whether that's worth it is decided with the latency and answer measurements.

### The baseline

The results are written rounded, so a rerun on the same code gives the same file: two runs here were byte for byte identical. `evaluation/results/` is committed, and a model test reruns both measurements and fails if the numbers differ from the files ([tests/integration/test_evaluation_baseline.py](../tests/integration/test_evaluation_baseline.py), `pytest -m model`). A change to chunking, embedding or retrieval that moves the numbers has to commit the new results with it, so this page never quotes numbers the code no longer gives. On another machine, tiny floating-point differences in the embeddings could in principle swap two close chunks; that hasn't been checked.

Without the model, the same code runs over the fake embedder to check everything around the numbers: every question has relevant chunks, every chunk ranked is one the asker may read, a quote found in many documents counts only its own document's chunks, and the report is the same twice.

