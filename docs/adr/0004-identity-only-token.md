# 0004. Token carries identity only; user is loaded on every request

Status: Accepted
Date: October 2026

## Context

Authentication uses JWTs. A token could carry the user's department and roles, which avoids a database lookup. But a token can't be changed after it is issued, so whatever it says stays true until it expires.

## Decision

The token holds the user ID and an expiry time. On every request the service loads the user from the database and reads department, roles and whether the account is active from there.

## Alternatives

- Put department and roles in the token. Faster and stateless. Rejected because a deactivated user or a changed role stays valid until the token expires.
- Roles in the token with a very short lifetime and refresh tokens. Narrows the window but doesn't close it, and adds a refresh flow.
- Roles in the token plus a revocation list. Works, but the list has to be checked on every request, which is the same lookup with more moving parts.
- Server-side sessions. Would also work. JWT was kept because the API is called by a separate UI process and by scripts.

## Consequences

- Deactivation and role changes apply on the next request.
- One database lookup per request. Small compared with an embedding and an LLM call.
- The service isn't stateless with respect to identity. It needs the database to authenticate.
- A leaked signing key still allows forging a token for any real user ID. This decision doesn't help with that.

## Revisit when

The identity lookup shows up in latency measurements, or identity moves to an external provider with its own session handling.
