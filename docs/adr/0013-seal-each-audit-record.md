# 0013. Seal each audit record with a keyed hash, rather than chaining them

Status: Accepted
Date: October 2026

## Context

The audit trail is append-only from the service: the ORM refuses to change or delete a record, and database triggers refuse it from raw SQL. Neither stops someone who controls the database itself. They can drop the triggers and edit, delete or add records, and nothing would show it (threat T22).

The usual answer is a hash chain: each record holds the hash of the one before it, so changing any record breaks every link after it. But a chain needs each new record to read the newest one first. The service writes records from many requests at once, and two writers that read the same newest record fork the chain. Keeping it straight means serialising every audit write, across processes, for the life of the service.

## Decision

Each record is sealed on its own. The seal is an HMAC-SHA256 of every field of the record, its ID included, under a key derived from the token signing key. It is written in the same transaction as the record, in a table of its own: a record can't be updated once written, and its ID isn't known until it is. The seal table is insert-only, like the trail.

`python -m scripts.audit check` checks every record against its seal and the IDs for gaps. Each record's ID is written to the application log once it is committed.

What that finds, against someone with the database but not the key:

- a record changed in any field: its seal no longer matches;
- a record added: they can't make a seal that matches;
- a record deleted: a gap in the IDs, since SQLite gives each new record the next ID after the highest;
- a record moved to another ID, seal and all, to close a gap: the ID is inside the seal;
- a seal removed, or one left without its record.

## Alternatives

- A hash chain. Finds the same things and the newest records deleted too, if the latest hash is kept somewhere else. Rejected for the forking described above: a chain that is only right when writes don't overlap is wrong in production.
- A chain sealed later by a single background job. No forking, but records are unprotected until the job reaches them, and it is another moving part.
- Write-once storage outside the database. The real answer for production, and it doesn't need any of this. Not available on a laptop.
- A key of its own for the seals. One more secret to manage; deriving it from the signing key means anyone who has it can already be anyone in the service.

## Consequences

- The newest records deleted, with their seals, leave nothing behind in the database to show it. The log does: its highest `audit_id` against the trail's highest ID. That only works while the log is kept apart from the database, which is the operator's job.
- Records written before sealing existed can't be checked. They can only be the oldest ones; an unsealed record after a sealed one is reported.
- Changing the signing key makes every existing seal fail to match. Rotating it would need the old key kept for checking, or the trail given its own key first.
- PostgreSQL sequences can skip a number when a transaction rolls back, so a gap there wouldn't always mean a deletion.
- One more insert per record, in the same transaction.

## Revisit when

The service is deployed with somewhere to send records that can't be changed afterwards. Then the trail goes there, and the seals are a second check rather than the only one.
