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
