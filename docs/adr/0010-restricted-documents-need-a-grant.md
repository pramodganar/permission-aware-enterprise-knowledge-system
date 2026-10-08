# 0010. Restricted documents need a named grant, even for executives

Status: Accepted
Date: October 2026

## Context

The starting assumption was simple: employees read general documents and their own department's, and executives read everything.

Two things don't fit that. Some documents shouldn't be open to a whole department: an individual's HR case file is an HR document, but not every HR employee should read it. And "executives read everything" is rarely true in a real company. A chief operating officer does not browse disciplinary files or a colleague's medical accommodation, and legal privilege can depend on keeping a document to a named group.

The personas document listed both as open questions.

## Decision

Documents have one of three classifications: `general`, `departmental` or `restricted`.

A `restricted` document can only be read by users who hold a grant for it. No role reads restricted documents, including `executive`. The owning department doesn't either.

Executives still read every `general` and `departmental` document in every department.

A grant names one user and one document, records who gave it, and can expire.

## Alternatives

- Two classifications and executives read everything. Simplest, and it matches the original assumption. Rejected because it has no way to keep a document to named people, which is the case that matters most when something leaks.
- A `restricted` level that executives can still read. Solves the "not the whole department" problem but keeps the unrealistic rule, and makes the executive role a target: compromising one account exposes every sensitive document.
- More roles, such as `hr_manager`. Works for a few cases and then multiplies. Access to a case file follows the people involved in the case, not a job title.
- A list of allowed roles and users on every document. Flexible, but two ways of saying who may read (classification and lists) can disagree, and long lists on every document are hard to review.

## Consequences

- There is a clear place for the most sensitive documents, and a clear answer to "who can read this one": the names on its grants.
- Compromising an executive account no longer exposes everything.
- The access rule has a fourth case, and the scope query in retrieval has to include grants.
- Someone has to maintain grants. A restricted document with no grants is readable by nobody, which is safe but can look like a missing document.
- "Executives can read everything" is no longer true as stated. The requirements and personas documents are updated to say so.
- Grants without an expiry accumulate. Reviewing them is not built.

## Revisit when

A customer needs group-based grants (a project team, a committee) or an approval step. Both would extend grants without changing the classification rule.
