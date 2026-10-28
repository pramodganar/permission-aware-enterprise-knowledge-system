# 0014. Name documents outside the service by a random public ID

Status: Accepted
Date: October 2026

## Context

A document's ID is the database's: 1, 2, 3, in the order documents were stored. Every route, the UI and the audit trail used it. So a reader who could open documents 3 and 7 knew three others had been stored between them (threat T36). Not their titles, departments or text, but a count of documents they may not read, which SR-10 rules out. It was the one thing about hidden documents a reader could learn, and the non-interference test couldn't see it: that test compares two companies, and any document stored before one the person may read changes that document's ID.

## Decision

Each document has a second ID, its public one: a random UUID (version 4, 122 bits from the operating system), made when the document is made. It is the only ID that leaves the service. Responses, paths, search filters and the audit trail's `document_id` all use it. The database's own ID stays the key inside the service, so the access rules, the scope, the index and every foreign key are unchanged.

Public IDs live in a table of their own, `document_public_ids`, rather than a column on `documents`. The service creates tables that are missing at startup and never changes one, so a database from before this version gains the table, and the service gives every document stored before it a public ID when it starts. Going back a version still works: the older one ignores the table.

The admin routes gained a list, `GET /admin/documents`, because an admin can no longer find documents by counting from 1.

## Alternatives

- Encrypt the number with a key, for example a small block cipher over 64 bits. No table and no lookup, and the same number always gives the same ID. But it is cryptography of my own to get right, and it ties every document's ID to a key: replacing the key would change every link and filter, and anyone with the key reads the numbering back.
- A column on `documents`. Simpler queries, but every existing database would need its table changed, and there are no migrations.
- A hash of the number, or of the title. Not random: a hash of a number can be found by trying every number, and a title's hash gives two companies with the same document the same ID.
- Sequential IDs with a random start, or random gaps. Still in order, and the gaps still say something.

## Consequences

- The non-interference test now renumbers every document in its second company, with gaps, and the responses are still byte for byte the same.
- A document's ID is 36 characters, not one or two. An admin picks documents from a list instead of typing an ID.
- Each new version is a new document with its own public ID, as it already had its own number.
- One more lookup to turn a public ID into the number, on each request that names a document.
- Records written to the audit trail before this version keep the numbers in their details, such as a refused lookup's `requested_id`; their `document_id` is shown as the public ID like any other.
- Chunk IDs and user IDs are still numbers. Chunk IDs are shown only in the audit trail, to the security reviewer, who reads every record anyway. A person sees only their own user ID.

## Revisit when

The service moves to PostgreSQL with migrations. Then the public ID could be a column on `documents`, with the same values.
