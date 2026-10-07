# 0006. Fail the request when the audit record can't be written

Status: Accepted
Date: October 2026

## Context

Every query and admin action is audited. The audit write can fail, for example when the database is unavailable or full. The service then has to choose between answering without a record and not answering.

## Decision

The request fails. A query that can't be audited isn't answered.

## Alternatives

- Answer anyway and log the failure. Keeps the service available. Rejected because an attacker who can cause audit failures gets unrecorded access, and gaps in the log undermine the rest of it.
- Write audit records to a queue and persist them later. Better availability and still complete in normal operation. Not chosen now because it adds a component, and records can still be lost if the queue is.

## Consequences

- The audit log is complete for every answered request.
- Q&A availability is tied to the availability of the audit store.
- The audit write is on the request path and adds to latency.

## Revisit when

Audit writes become a measurable share of latency or a cause of outages. The next step would be a durable queue.
