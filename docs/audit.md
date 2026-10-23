# Audit trail

Every question, search, document opened, login, refusal and change to a document is recorded: who did it, when, what, and what came of it (FR-13, SR-15, SR-16). Only a security reviewer can read the records (FR-14), and reading them is recorded too.

The application log leaves the question and the document text out (NFR-9). The audit trail is where the question goes, with the chunks the person was given, because a reviewer needs both to say what someone saw. That is also why it has its own permission.

## What is recorded

| Action | When | Holds |
|---|---|---|
| `question.sent` | Just before the model is called | The question and the chunks it is about to be sent, and the provider. If this can't be stored, the model isn't called |
| `question.asked` | Every question that reaches retrieval, whatever came of it | The question, the chunks given to the model in order, the outcome (`answered`, `no_information`, `failed`) and its reason, the cited documents, the model, prompt version and token counts, any filters |
| `search` | Every search | The question, the chunks returned, the filters |
| `document.read` | Every attempt to open a document | `allowed`, `denied` or `not_found`. The response for the last two is identical; the record tells them apart |
| `login.succeeded` | Every login that gets a token | The account. Where looking into a stolen password starts |
| `login.failed` | Every failed login, and every attempt refused for too many failures before its password is looked at | Why it failed, and the account if the email belongs to one. Not what was typed: people type passwords into the email field |
| `action.denied` | Every request refused with 403: an action the person's roles don't allow, or an admin giving themselves access | The route's pattern, the permission it needed, why it was refused, and the document if it was one |
| `document.ingested`, `document.replaced`, `document.deleted`, `document.relabelled`, `documents.rechunked` | Every change to a document | The document, its labels before and after, the file's checksum |
| `grant.given`, `grant.revoked` | Every change to who may read a document | The document, who was granted, until when |
| `audit.read` | Every read of the trail | Who read it, with what filters, and how many records it returned |

A question that is refused before retrieval (empty, too long) isn't recorded, because nothing was looked up. Nor are the health check, the caller's own account, lists of documents (opening one is recorded), or an admin's view of a document's labels (every change to them is). Nor is a request without a valid token: there is nobody to record, and anyone could fill the trail with them. They are in the application log.

Which route records what is written down once, as a table in [the completeness test](../tests/security/test_audit_completeness.py), with a reason for each route that records nothing. A route that isn't in it fails the test, so a new one can't be added without deciding. Every route is called by someone allowed to, by someone refused and with no token, and the records each call left are found by its request ID: exactly what the table says, by whoever made the call, and sealed.

A question that goes to the model leaves two records: `question.sent` before the call, and `question.asked` with the outcome after it. Records are never changed, so "sent" and "answered" are two facts recorded when each became true. A failed question has both, the second with outcome `failed`: its chunks were sent all the same, and with a hosted model that is company text that left the building.

The record before the call is there because of what the record after it can't cover. Running the service with the audit table removed showed the model being called, answering, and only then the record failing: the answer was withheld, but the question and its sources had already gone to the provider with no record. Now the model is never called unless what it will be sent is on record first.

Each record has the request ID of the request that made it, so the application log lines of the same request can be found.

## Written with what it records

Changes are recorded in the same transaction as the change, by the code that makes it: the ingestion pipeline and the document access module. A change is stored with its record or not at all. There is never a change without a record, nor a record of a change that didn't happen.

Questions, searches, document reads and failed logins change nothing, so they are recorded just before the response goes out.

## If a record can't be stored, the request fails

[Decision record 0006](adr/0006-fail-request-when-audit-fails.md). Tested by removing the audit table from under the service:

| Request | Response |
|---|---|
| A question | 503. The model is never called |
| A search | 503, no results |
| Opening a document | 503, no content |
| A failed login | 503 instead of 401 |
| A login | 503, no token |
| A refusal | 503 instead of 403 |
| A change | refused, and the document is as it was |

## Append-only

The service only ever adds records, and two things hold it to that:

- The ORM refuses to flush a changed or deleted record.
- In SQLite, triggers refuse `UPDATE` and `DELETE` on the table, for anything that bypasses the ORM. PostgreSQL would do the same with a trigger, or by not granting the service's role `UPDATE` and `DELETE` on the table.

Neither stops someone who controls the database itself (T22). The seals below find what they do. Production would send records to separate write-once storage.

A database seeded before the audit trail existed gets the table, and its triggers, when the service starts. Existing tables are never changed.

## Sealed

Every record is sealed when it is written: an HMAC-SHA256 of all its fields, its ID included, under a key derived from the token signing key. The seal is stored in the same transaction, in `audit_seals`, which is insert-only like the trail ([decision record 0013](adr/0013-seal-each-audit-record.md)).

```
python -m scripts.audit check
```

checks every record against its seal and the IDs for gaps, prints the highest ID, and exits 2 if anything doesn't match. It never prints what a record says. Against someone with the database but not the key, it finds:

| What they did | What the check says |
|---|---|
| Changed any field of a record | `record N does not match its seal` |
| Added a record, with a seal made up without the key | `record N does not match its seal` |
| Deleted records from the middle | `records N to M are missing` |
| Deleted a record and moved the next one down, seal and all, to close the gap | `record N does not match its seal`: the ID is inside the seal |
| Swapped two records | both don't match |
| Removed a seal, or left one without its record | `record N has no seal`, `seal N has no record` |

Each is a test, run with the triggers and foreign keys dropped first, as someone who owns the database would.

**What it can't find** is the newest records deleted, seals and all: nothing is left in the database to say they were there. Each record's ID is written to the application log once it is committed (`audit record sealed`, with `audit_id`), and never for one that was rolled back. The log is kept apart from the database, so its highest `audit_id` against the check's highest ID shows it. That is only as good as keeping the log somewhere else.

Records written before sealing existed can't be checked, and are counted separately. They can only be the oldest: an unsealed record after a sealed one is a problem. The key comes from the signing key, so there is one secret to keep; changing that key makes every existing seal fail to match, which is one reason the service has no key rotation yet.

## Reading it

```
GET /audit   ?actor=david@acmecorp.example&action=question.asked&document_id=16
             &since=2026-10-01T00:00:00Z&until=...&limit=100&offset=0
```

Newest first, with `total` across every page. Times must include a timezone. Anyone without `audit:read` gets 403. In the company that is everyone but Irene.

## Not covered

- Retention. Records are kept for ever; questions are personal data, and a real deployment would need a retention period and a way to honour it.
- Tampering by whoever runs the database and also has the service's key, and the newest records deleted, if the application log isn't kept apart from the database (T22).
