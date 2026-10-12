# 0011. Pack lines into size-bounded chunks with overlap

Status: Accepted
Date: October 2026

## Context

Documents have to be split before they are embedded. The split decides what can be retrieved and cited, and changing it later means re-chunking and re-embedding every document.

The stored text comes from three file formats and has no common structure. Text files keep blank lines between paragraphs. Word files give one paragraph per line with no blank lines. PDF files give lines cut where the page was, with the page header and footer among them. Headings can't be told from numbered steps: a pattern for them was wrong in 2 of the 17 AcmeCorp documents.

The embedding model reads a limited number of tokens and ignores the rest without an error.

## Decision

Split the stored text into lines and pack consecutive lines into chunks of at most 800 characters. Close a chunk at the best nearby boundary: a blank line, then the end of a sentence, then any line. Start each chunk with up to 120 characters of whole lines from the one before.

Page headers and footers are removed from PDF text at extraction, before chunking.

Chunking is done per document version. A chunk never holds text from two documents. A chunk stores its text, its position and its character offsets, and no permissions.

The sizes are settings and are recorded in the index manifest. The real token count is checked against the model's limit when a chunk is embedded.

The measurements and the full reasoning are in [chunking.md](../chunking.md).

## Alternatives

- One chunk per document. Works for short documents only. A long one would be truncated by the embedding model silently.
- Fixed character windows. Simple, but cuts words and numbers in half.
- Split on blank lines. Only the text files have them.
- Split at headings. Best when headings are known. They aren't, in the stored text.
- Semantic splitting with embeddings. Slow, tied to one model, and hard to reproduce in a test.
- Measure size in tokens. Exact, but it ties the chunker to one model's tokenizer. Characters with a check at embedding time gives the same protection.

## Consequences

- One method handles all three formats, and it can be tested without a model.
- The same text and settings always give the same chunks, so chunks can be rebuilt from the stored documents.
- Chunks follow size and not meaning, so one can span two sections.
- About 15 percent of the text is stored and embedded twice because of the overlap.
- Near-identical neighbouring chunks can both be retrieved for one question.
- Header and footer removal could remove real content in an unusual layout.
- 800 and 120 are starting values. Nothing has been measured that says they are the best ones.

## Revisit when

The evaluation set exists and shows how retrieval changes with chunk size, or extraction is changed to keep headings and other structure.
