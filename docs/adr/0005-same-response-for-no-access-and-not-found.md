# 0005. Same response for "no access" and "not found"

Status: Accepted
Date: October 2026

## Context

When a user asks about something in a document they can't read, the system has to say something. If it says "you don't have access to that", it has confirmed the document exists. For some documents, such as an acquisition plan, existence is the secret.

## Decision

The two cases share one code path. Both end with no usable chunks, and both return the same fixed message without calling the LLM. There is no "forbidden" branch.

Titles, counts, scores and citations of unauthorized documents are never returned, and error messages don't refer to them.

## Alternatives

- Tell the user they lack access and whom to ask. More helpful, and what many document systems do. Rejected because it leaks existence.
- Reveal existence only for some departments or classifications. Reasonable in a real deployment, since many documents are not secret in themselves. Not built, because it adds a second policy to maintain.

## Consequences

- An unauthorized user can't use the assistant to map which restricted documents exist.
- Because it is one path, the two responses can't drift apart over time.
- An employee with a legitimate need gets a dead end and isn't told where to go.
- No LLM call for refused questions, which also means no cost and no chance of the model guessing.

## Revisit when

A customer wants "request access" behaviour. That would be a per-department or per-classification setting, with the current behaviour as the default.
