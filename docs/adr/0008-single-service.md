# 0008. One service with separate modules

Status: Accepted
Date: October 2026

## Context

The system has distinct responsibilities: authentication, authorization, ingestion, retrieval, generation and audit. They could be separate services or modules in one.

## Decision

One FastAPI service. Each responsibility is a module with a defined interface, and modules depend on each other through those interfaces. The UI is a separate process that only calls the API.

## Alternatives

- Separate services, with authorization as its own service. Common in larger organisations, where one policy service serves many applications. Rejected here because a network call between authorization and retrieval adds latency and a new failure mode on the most security-critical path, and there is only one application.
- An external policy engine such as OPA. Good for complex, changing policies owned by a security team. The current rules are simple enough to express in code. The authorization interface is kept narrow so an engine could sit behind it later.

## Consequences

- One thing to deploy, run and debug.
- Authorization can't be unreachable while retrieval is up, so there is no partial-failure case to design for.
- Parts can't be scaled independently. Ingestion and queries share the same process.
- The module boundaries are only convention. Tests should check that content-returning code goes through the authorization module.

## Revisit when

Ingestion load starts to affect query latency (move ingestion to a worker first), or a second application needs the same authorization rules.
