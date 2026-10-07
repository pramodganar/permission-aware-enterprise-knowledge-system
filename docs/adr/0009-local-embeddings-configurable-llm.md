# 0009. Local embeddings, configurable LLM provider

Status: Accepted
Date: October 2026

## Context

Two models are involved: one to embed documents and questions, one to write answers. Each could be a hosted API or run locally. Anything sent to a hosted API leaves the company's systems.

Embedding touches every document at ingestion. Generation touches only the authorized context for one question.

## Decision

Embeddings are computed locally with a sentence-transformers model. The LLM is called through a provider interface, and the provider and model are set by configuration. Tests use a fake provider.

## Alternatives

- Hosted embedding API. Often better quality and no model to load. Rejected because it would send the whole corpus, including the most restricted documents, to a third party during ingestion.
- A local LLM only. Nothing leaves the service at all. Not made the default because answer quality on modest hardware is lower. It remains possible through the provider interface.
- Hard-coding one LLM provider's SDK. Less code. Rejected because the choice of provider is usually the customer's, driven by their data agreements.

## Consequences

- Document text leaves the service at one point only: the LLM call, with authorized context for one question.
- A customer can choose a provider that meets its data rules, or a self-hosted model, without code changes.
- The embedding model is loaded in the service, which adds memory use and startup time.
- Changing the embedding model means re-embedding the corpus. The index manifest records which model built it, and a mismatch stops the service from answering.
- The provider interface has to stay small, or it turns into a framework of its own.

## Revisit when

Retrieval quality measurements show the local embedding model is the limiting factor.
