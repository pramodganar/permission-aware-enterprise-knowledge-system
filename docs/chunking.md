# Chunking

Status: Draft. The chunker and the removal of page headers and footers are implemented, and ingestion stores chunks with each document. Nothing is embedded yet.
Date: October 2026

A document is too long to embed or to hand to the LLM as one piece, so it is split into chunks. This document says how, and why. The decision itself is recorded in [decision record 0011](adr/0011-pack-lines-into-bounded-chunks.md).

## What a chunk is for

A chunk is the unit that is embedded, searched, put into the LLM context and cited. So it has to be:

- small enough for the embedding model to read in full,
- large enough to make sense without the rest of the document,
- traceable to an exact place in one document.

It also has to be safe, which here means one thing: a chunk never contains text from more than one document.

## What the stored text looks like

I measured the text the ingestion pipeline stored for the 17 AcmeCorp documents before designing anything. The three file formats give three different shapes of text. The figures in this section are from that first measurement, when page headers and footers were still in the PDF text.

| | Text files (3) | Word files (8) | PDF files (6) |
|---|---|---|---|
| A line is | A paragraph, a heading or a list item | A paragraph, a heading, a list item or a table row | A piece of a paragraph, cut where the page was |
| Blank lines between paragraphs | Yes | No | No. One blank line per page break |
| Longest line | 458 characters | 296 | 88 |
| Page header and footer in the text | No | No | Yes, on every page |

Across the corpus:

- A document is 1,555 to 2,625 characters, or 261 to 453 words. The whole corpus is 37,226 characters.
- A numbered section is 36 words at the median and 127 at most.
- Only one line in the corpus is longer than 400 characters.

Three things follow from this.

**Blank lines can't be the rule.** Splitting on blank lines is the usual first approach. It would work for 3 of the 17 documents.

**Headings can't be found reliably.** A heading looks like `3. Supervisor actions`. So does a numbered step, `3. Tell the shift supervisor at once.` A pattern for headings gave a wrong sequence in 2 of the 17 documents (the warehouse SOP and the HR case file). The reliable signal, the heading style in a Word file, is dropped at extraction.

**PDF text carries page furniture.** In the middle of the warehouse SOP the stored text reads:

```
the supervisor has spoken to them and recorded it.
Confidential - Operations Page 1 of 2
Uncontrolled when printed. The current version is on the intranet.

ACMECORP Operations
7. Damaged customer goods
```

Those lines say nothing about the content, they repeat on every page, and when a sentence runs over a page break they land in the middle of it.

## The design

### 1. Remove page headers and footers from PDF text first

This happens at extraction, because that is the only place where the pages are still separate.

A line is removed when it is among the first two or the last two lines of a page and the same line, with digits ignored, is in the same position on every page of the document. Ignoring digits makes "Page 1 of 2" and "Page 2 of 2" the same line.

It only applies to documents of two pages or more. On a one-page document nothing repeats, so nothing is removed.

The risk is removing real content, for example a table whose first row is repeated on each page. Ignoring digits adds to it: a real line at the edge of every page that differs only in its numbers is taken for a header. Keeping the rule to the outer two lines, and requiring a match on every page, keeps that unlikely. It is not zero.

If the rule would remove every line of the document, nothing is removed.

### 2. Split the text into lines, and pack lines into chunks

The unit is the line, because a line means something in all three formats, even if not the same thing.

Lines are added to a chunk in order until the next line would take it over the size limit. Then the chunk is closed and the next one starts.

A chunk should not end in a bad place. Naive packing at 800 characters ended 27 of 43 chunks on a line that did not finish a sentence: a heading cut off from its section, or half a sentence from a PDF. So before closing a chunk, the packer looks back over its last few lines for a better place to stop, in this order:

1. after a line that ends a sentence (`.`, `?`, `!`) and is followed by a blank line,
2. after any line that ends a sentence,
3. before a blank line,
4. after any line.

It looks back over at most the last quarter of the chunk, so a chunk can't shrink to almost nothing.

My first version of this list put "before a blank line" first. That was wrong, and the tests on the company documents showed it: in a text file a heading is followed by a blank line too, so the rule chose to end a chunk on `5. Database changes`. A blank line only counts as a good place when the line before it finishes a sentence.

A single line longer than the limit is split at sentence ends, and if a sentence is still too long, between words, and if a word is still too long, inside it. On this corpus that never happens: the longest line is 458 characters.

### 3. Overlap

Each chunk starts with the last lines of the one before it, up to the overlap size. A fact that sits on a boundary is then whole in at least one chunk.

The overlap is made of whole lines and never reaches into another document.

### 4. Sizes

| Setting | Value | Reason |
|---|---|---|
| Maximum chunk size | 800 characters | See below |
| Overlap | 120 characters | About one paragraph line, or one and a half PDF lines |

These are settings, not constants in the code, and the index manifest records the values that built an index (NFR-6).

**Why 800.** The limit that matters is the embedding model's. Small sentence-transformers models read 256 to 512 tokens and silently ignore the rest, so text beyond the limit is in the chunk but not in the vector. At roughly four characters per token for English, 800 characters is about 200 tokens, which leaves room for the document title (see below) under a 256-token limit.

Four characters per token is a rule of thumb, and I have not measured it on this text. Tables and numbers tokenize worse than prose. So the size is checked again where it matters: when a chunk is embedded, its real token count is compared with the model's limit, and a chunk over the limit is an error, not a silent truncation.

**Why characters and not tokens.** Counting tokens needs the embedding model's tokenizer, which would tie the chunker to one model and make it slow to test. Characters are exact, fast and model-independent. The price is the approximation above, covered by the check at embedding time.

**What these values give on the corpus.** Measured with the implemented chunker on the 17 documents, 35,844 characters once page headers and footers are gone:

| Maximum size / overlap | Chunks | Per document | Median chunk | Chunks that end mid-sentence |
|---|---|---|---|---|
| 400 / 60 | 118 | 5 to 9 | 334 characters | 13 of 101 |
| 800 / 120 | 57 | 3 to 4 | 724 characters | 2 of 40 |
| 1,200 / 180 | 41 | 2 to 3 | 1,027 characters | 1 of 24 |

The last column counts chunks, other than the last of a document, whose final line does not end with `.`, `?` or `!`. With the look-back switched off, the figure at 800 / 120 is 23 of 40.

The two that remain at 800 / 120 are a PDF sentence spread over more wrapped lines than the look-back reaches, and a list item ending in a semicolon.

I have no measurement yet that says 800 retrieves better than 400 or 1,200. That needs the evaluation set. 800 is the starting point because it fits the model limit and holds about three sections of a typical document.

### 5. What is embedded is not exactly what is stored

A chunk such as "Up to 5,000 EUR: Finance Analyst" does not say what the limit is for. The document title does.

So the text given to the embedding model is the document title, a line break, and the chunk text. The stored chunk text is the document's own words and nothing else, so a citation shows exactly what the document says.

The title comes from the same document as the chunk, so it has the same audience.

### 6. What is stored for each chunk

| Field | Meaning |
|---|---|
| `document_id` | The document version this chunk came from |
| `position` | 0, 1, 2 ... in reading order. Used once per document. |
| `text` | The chunk's text |
| `start_offset`, `end_offset` | Character offsets into the document's stored text |
| `created_at` | When the chunk was written |

The offsets make a chunk checkable: the document text from start to end must equal the chunk text. They also let a citation point at the exact passage.

There is no department, classification or grant on a chunk. Those are read from the document ([decision record 0002](adr/0002-permissions-in-the-database.md)). A test lists the columns of the table, so adding one is a deliberate act.

Chunks are written in the same transaction as their document. A document is never stored without them, and a refused upload stores neither.

## Rules the chunker must keep

These become tests.

1. **One document per chunk.** A chunk's text, including its overlap, comes from a single document version.
2. **Nothing lost.** Every non-blank line of the document is in at least one chunk.
3. **Nothing added.** A chunk's text is a slice of the document's stored text.
4. **Bounded.** No chunk is longer than the maximum size.
5. **Deterministic.** The same text and settings always give the same chunks.
6. **Terminates.** The overlap is always smaller than the chunk size, so each chunk moves forward.
7. **Every chunk adds something.** A chunk ends later than the one before it. Looking back for a better ending may never give up the first new line of a chunk, or the chunk would be nothing but overlap.

Rule 1 is the security rule. If overlap or batching ever joined the end of one document to the start of the next, a chunk could hold text from an HR case file inside a chunk that belongs to the leave policy, and the permission check on the leave policy would let it through.

## Versions and deletion

Chunks belong to one document version and are never edited.

- A new version of a document gets its own chunks. The old version's chunks stay, pointing at the old row.
- The old chunks stop being searchable at once, because their document is no longer active and the access query only returns active documents. Nothing has to be cleaned up for that to be true.
- Their vectors are removed at the next index rebuild.

Chunking is deterministic, so chunks can be rebuilt from the stored document text at any time, for example after changing the size. `rechunk_documents` does this for every active document. It compares the stored chunks with what the current settings give and only rewrites a document where they differ, so the chunk IDs of everything else stay the same. The seed script runs it, which also covers a database filled before chunking existed.

A re-chunked document gets new chunk IDs. Once there is a vector index, its vectors for that document are stale and the index has to be rebuilt.

## What I considered and didn't choose

| Approach | Why not |
|---|---|
| One chunk per document | Fits these documents, which are short. A real policy of twenty pages would be cut off by the embedding model with no warning, and a citation to a whole document is not much of a citation. |
| Fixed windows of N characters | Simplest. Cuts through words and sentences, and a number cut in half is a wrong number. |
| Split on blank lines | Works for 3 of 17 documents. |
| Split at headings | The best result when it works. Headings can't be told from numbered steps in the stored text. Worth returning to if extraction keeps the structure of Word files. |
| Split where the topic changes, using embeddings | Needs the model at chunking time, is slow, and the result changes with the model, so it is hard to test and to reproduce. |
| Split by sentence only | Loses the line structure that lists and table rows depend on. A table row is not a sentence. |

## What this costs

- Chunks follow size, not meaning. A chunk can hold the end of one section and the start of the next.
- Overlap stores some text twice. At 120 of 800 characters that is about 15 percent.
- Two chunks of the same document can both be retrieved for one question and say nearly the same thing. The context builder will have to handle that.
- Removing page headers and footers can, rarely, remove real content.
- A marking printed in a footer, such as "Confidential - Finance", is removed from the text. That is intended: it is not what protects the document, and it should not be something a search can match on.

## Not covered

- Scanned PDFs. They have no text layer, so there is nothing to chunk.
- Tables that are longer than one chunk. The rows are kept whole, but the header row is only in the first chunk.
- Languages without spaces between words, where "split between words" does not apply.
- Removing text that is duplicated across documents.

## Open until measured

- Whether 800 and 120 are good values.
- Whether putting the title in front of the embedded text helps.
- The real characters-per-token ratio for this corpus and the chosen model.
