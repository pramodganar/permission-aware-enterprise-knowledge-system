# 0001. Enforce permissions before the LLM, never in the prompt

Status: Accepted
Date: October 2026

## Context

The system answers questions from internal documents with different audiences. The requirement is that a user never receives information they are not allowed to read. Somewhere in the pipeline that has to be enforced.

## Decision

Access is decided by code in the retrieval path. Only chunks the user may read are retrieved, reranked and placed in the context. The LLM is called after that and has no way to obtain anything else.

No security requirement may be satisfied by an instruction in the prompt.

## Alternatives

- Retrieve everything and instruct the model not to reveal restricted content. Rejected. The restricted text is sent to the model on every such query, and a prompt is a request, not a control. Prompt injection, rephrasing or a model error can defeat it, and it can't be tested to a pass/fail standard.
- Retrieve everything and filter the model's output. Rejected. Detecting restricted content in free text is unreliable, and paraphrases get through.
- A separate model deployment per department. Rejected. It doesn't handle general documents or executives well, and it multiplies cost and operations.

## Consequences

- Leakage becomes a property of deterministic code, which can be tested exactly.
- Prompt injection can affect what an answer says but not what the user can reach.
- The retriever becomes security-critical and needs its own tests.
- The assistant can't hint that restricted information exists, because it never sees it.

## Revisit when

Never for the principle. The mechanism behind it is in 0002 and 0003.
