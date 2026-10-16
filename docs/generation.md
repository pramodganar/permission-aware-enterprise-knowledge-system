# Generation

How the chunks a person may read become the answer they get. Being built in phase 8; this page grows with it.

## The context

The context builder (`app/rag/context.py`) turns a retrieval into the sources the LLM will read. Its only input is the retrieval: chunks that passed both access checks and the relevance gate. It has no database session and no other source of text, so it can leave chunks out but can't add any.

**Sources.** Chunks become numbered sources, S1 being the most relevant. The answer will cite these labels, and a citation can only point to a source that was in the context.

**Neighbours are joined.** Chunks overlap so that a sentence cut at a chunk boundary is whole in one of them. When two neighbouring chunks of the same document are both retrieved, the model would otherwise read the shared lines twice. They become one source, with the shared text kept once. In the company documents, 29 of the 40 pairs of neighbouring chunks share text (40 to 119 characters, 76 on average). The other 11 share none: the lines at the end of the first chunk were longer than the overlap allows. Those stay as two sources, because the blank line between them is in neither chunk, and joining them would mean making up the text in between. So every source is exactly a piece of a stored document, which a test checks against the stored text.

A joined source is as relevant as its best chunk. Two separate passages of one document are ranked separately, so a weak passage isn't pulled up next to a strong one.

**The cap (NFR-3).** At most `APP_CONTEXT_MAX_CHARACTERS` characters of document text (default 6,000) per question. It is counted in characters because every LLM provider counts tokens differently, and the limit has to hold before any provider is asked. A chunk that doesn't fit is left out whole, never cut, and the next smaller one may still fit. A chunk next to one already chosen only costs the text it adds. The setting can't be smaller than one chunk, so the best chunk always fits. With the defaults, five chunks of at most 800 characters always fit.

**Document text is data.** Each source goes to the model in its own block:

```
<source id="S1" document="Leave Policy" version="4">
...the passage...
</source>
```

The text and the title are escaped, so a document containing `</source>` can't close its block and present what follows as something else (threat T14). That doesn't stop a model from being swayed by what a document says. It does let the prompt say exactly where documents begin and end, and anything the model does with them stays inside what the person may read anyway.
