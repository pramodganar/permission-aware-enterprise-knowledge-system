# 0015. Withdraw tokens with a per-user version

Status: Accepted
Date: October 2026

## Context

A token holds the user's ID and an expiry, and the user is loaded on every request (decision record 0004). That made deactivation and role changes apply at once, but signing out didn't: the UI forgot the token, and the service went on accepting it until it expired, up to 30 minutes later. A token copied from a shared computer, a browser's memory or a log kept working after its owner had signed out.

## Decision

Each user has a token version, 0 until they first sign out. A token carries the version that was current when it was issued, in a `ver` claim. `POST /auth/logout` adds one to the caller's version, so every token issued before stops matching and is refused on its next request, on every device. Signing in again gives a token with the new version.

The version lives in a table of its own, `token_versions`, loaded with the user in the query every request already makes, so checking it costs no query of its own. A database from before this version gains the table at startup, like the public document IDs (decision record 0014). A token issued before versions existed has no `ver` and counts as version 0, so nobody is signed out by the upgrade.

Signing out is audited (`signed.out`), in the same transaction as the change: if the record can't be stored, nothing is withdrawn and the request fails (decision record 0006).

## Alternatives

- A list of withdrawn tokens, each by an ID in the token, kept until it would have expired. Withdraws one device at a time. But it grows with every sign-out, needs cleaning, and needs a lookup the user query can't carry.
- A "tokens issued before" time per user. The same idea, but `iat` is in whole seconds, so a sign-in in the same second as a sign-out is either refused or lets the old token through. A counter has no clock to get wrong.
- Shorter tokens with refresh tokens. Narrows the window without closing it, and adds a refresh flow to the API and the UI.
- Server-side sessions. Would work too; it would replace the tokens rather than add one number to them.

## Consequences

- Signing out ends every session of that account, not only the one it was sent from. There is no way to end one device and keep another.
- A token taken while the person is still signed in works until they sign out or it expires. Signing out is how they end it.
- The UI signs out at the service before forgetting the token. If the service can't be reached it still signs out locally, and says the service's session lasts until it expires.
- One small table, and one more column in the user query.

## Revisit when

Identity moves to an external provider with its own sessions, or people need to end one device's session and keep the others.
