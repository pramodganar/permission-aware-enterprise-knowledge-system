# Authorization

Status: Draft. The role model and the document rules are designed and implemented in the authorization service. Nothing calls the service yet; retrieval and the API come later.
Date: October 2026

Authentication answers "who is this?". This document is about the next question: "what may they do?". The requirements it implements are in [requirements.md](requirements.md), and the reasons for the wider design are in [architecture.md](architecture.md).

## The model

```
User --has--> Roles --grant--> Permissions
User --belongs to--> one Department
```

Two things about a user decide their access:

- Their roles, which say what kinds of things they may do.
- Their department, which says whose documents "own department" means for them.

Both are read from the database on every request. Neither is stored in the token.

## Roles

| Role | Meant for |
|---|---|
| `employee` | Anyone who uses the assistant to ask questions |
| `executive` | Senior leadership, who need to read across departments |
| `knowledge_admin` | Whoever ingests documents and sets their permissions |
| `security_reviewer` | Whoever reviews the audit log |

A user can hold several roles. A new user holds none.

## Permissions

| Permission | Allows |
|---|---|
| `documents:read:general` | Reading company-wide documents |
| `documents:read:own_department` | Reading documents owned by the user's own department |
| `documents:read:all_departments` | Reading documents owned by any department |
| `documents:read:granted` | Reading documents the user has been individually granted |
| `documents:manage` | Ingesting documents, changing their permissions, replacing and deleting them |
| `audit:read` | Reading the audit log |

## Which role grants what

| | `employee` | `executive` | `knowledge_admin` | `security_reviewer` |
|---|---|---|---|---|
| `documents:read:general` | yes | yes | | |
| `documents:read:own_department` | yes | yes | | |
| `documents:read:all_departments` | | yes | | |
| `documents:read:granted` | yes | yes | | |
| `documents:manage` | | | yes | |
| `audit:read` | | | | yes |

This table is the policy. In the code it is one mapping in `app/authorization/permissions.py`, and a test holds a second copy of it, so the policy can't change without a test changing too.

## Rules

1. Deny by default. A user with no roles has no permissions and can read nothing.
2. Permissions add up. A user's permissions are the union of what their roles grant.
3. There are no deny rules. Access is only ever added. This keeps the outcome easy to reason about: to know what someone can do, list their roles and take the union.
4. Unknown roles grant nothing. If a role exists in the database that the code doesn't know, it is ignored.
5. Inactive users have no permissions, whatever roles they still hold.
6. Changes apply on the next request, because roles are read from the database each time.

## Separation of duties

Reading documents, managing documents and reviewing the audit log are three separate powers, and none of the non-reading roles includes reading.

- A knowledge admin can upload a document and decide who sees it, but can't read other departments' content through search.
- A security reviewer can see who asked what, but doesn't get document content through that role.
- Someone who holds both of those roles still can't read documents.

A person can be given a reading role as well, for example an HR employee who is also a security reviewer. That is a visible, deliberate assignment, not something a role brings with it.

## How the personas map

| Person | Department | Roles | Can read |
|---|---|---|---|
| Alice | Engineering | `employee` | General + Engineering |
| Bob | Finance | `employee` | General + Finance |
| Carol | HR | `employee` | General + HR |
| David | Sales | `employee` | General + Sales |
| Emma | Legal | `employee` | General + Legal |
| Frank | Operations | `employee` | General + Operations |
| Grace | Executive | `executive` | General + every department |
| An IT administrator | any | `knowledge_admin` | Nothing through search |

This table is about `general` and `departmental` documents. `restricted` documents are a separate case, described under Documents below.

Grace's access comes from her role, not from being in the Executive department. Someone else in that department with only the `employee` role would read General and Executive documents and nothing more.

## Why this is not pure RBAC

Pure role-based access control can't say "your own department's documents" without a role per department (`engineering_reader`, `finance_reader`, and so on). That works but multiplies roles and makes moving someone between departments a role change.

So the design uses roles for the kind of access and one attribute, the department, for its scope. This is the smallest step towards attribute-based access control, and it is where further attributes would go later: document classification, project membership, location.

## Decisions

### The policy is in code, not in database tables

A classic RBAC schema has `permissions` and `role_permissions` tables. I didn't build them. With four roles and five permissions the tables would hold a dozen rows that nobody edits, at the cost of joins on every request and a policy that can be changed with one SQL statement and no review.

In code, a policy change is a commit: it is reviewed, tested and has a history. The cost is that a customer can't define their own roles without a code change. If that were needed, the mapping would move behind the same function to tables or a policy engine.

### No deny rules

Deny rules ("everyone in Finance except contractors") are expressive but make outcomes depend on evaluation order, and that is where authorization bugs come from. Leaving them out means a missing grant can only ever cause too little access, never too much.

### Executive is a role

Tying "reads everything" to the department name would hide a powerful rule inside a string comparison. As a role it can be granted, removed and audited like any other.

## Documents

The permissions above say what a user may do in general. Whether a user can read one particular document also depends on two things stored on the document.

### What a document carries

| Field | Meaning |
|---|---|
| Owning department | The department responsible for the document. Required. |
| Classification | Who the document is for. Required, with no default. |

A document can't be stored without both. There is deliberately no default classification: a default of "general" would make every unlabelled document public, and a default of "restricted" would hide mistakes. An unlabelled document is rejected instead.

### Classifications

| Classification | Who it is for | Example |
|---|---|---|
| `general` | Everyone who can use the assistant | Leave policy, code of conduct |
| `departmental` | Members of the owning department, and executives | Deployment runbook, vendor payment limits |
| `restricted` | Only people named in a grant | An individual HR case file, an acquisition paper |

### Grants

A grant gives one named user access to one document. It records who gave it and can have an expiry date.

Grants do two jobs:

- They are the only way anyone reads a `restricted` document.
- They let someone from outside a department read one of its documents, for example for a cross-team project, without changing their department or roles.

A grant only adds access. There is no "deny" grant.

### The decision

A user can read a document if any one of these is true:

| # | The document is | and the user has | and |
|---|---|---|---|
| 1 | `general` | `documents:read:general` | |
| 2 | `departmental` | `documents:read:own_department` | the document belongs to the user's department |
| 3 | `departmental` | `documents:read:all_departments` | |
| 4 | any classification | `documents:read:granted` | the user holds a grant for it that hasn't expired |

If none is true, the answer is no. That includes every case the table doesn't mention: a user with no roles, an inactive user.

One check comes before all four rows: the document's classification must be one of the three known values. If it isn't, the label can't be trusted and nobody can read the document, even with a grant.

### Where this is enforced

`app/authorization/service.py` is the only place that decides read access. It answers in two forms:

- `can_read(user, document)` for one document that is already loaded.
- `readable_documents(user)`, a database query for every document the user may read. Retrieval and the document list filter by this, so unauthorized documents are never fetched.

Having the same rule in two forms is a risk, because they could drift apart. A test runs both over every user and document and requires the same answer.

### What follows from this

- `restricted` documents are not covered by rows 1 to 3, so no role reads them. Not the owning department, and not executives. Only row 4 applies.
- A grant is not enough without a reading role. A knowledge admin who is given a grant still can't read the document, because the admin role doesn't include `documents:read:granted`.
- An expired grant is the same as no grant. Nothing has to be cleaned up for access to end.
- Moving a document between classifications changes who can read it on the next request. Nothing is re-indexed.

### Examples

| Document | Alice (Engineering, employee) | Carol (HR, employee) | Grace (Executive, executive) |
|---|---|---|---|
| Leave policy: HR, `general` | yes (1) | yes (1) | yes (1) |
| Deployment runbook: Engineering, `departmental` | yes (2) | no | yes (3) |
| Salary bands: HR, `departmental` | no | yes (2) | yes (3) |
| Executive compensation strategy: Executive, `departmental` | no | no | yes (2) |
| HR case file: HR, `restricted`, granted to Carol | no | yes (4) | no |
| Runbook, with a 30-day grant to Carol | yes (2) | yes (4), then no | yes (3) |

### Setting and changing access

Access is set when a document is ingested and can be changed afterwards. All of it needs the `documents:manage` permission, and all of it takes effect on the next request.

| Action | Rules |
|---|---|
| Ingest with labels | Department and classification are required. A `restricted` document must name at least one reader, otherwise it would be stored and readable by nobody. |
| Grant access | The user must be active and hold a role that can read documents. An expiry must be in the future. Granting again replaces the expiry. |
| Revoke access | Removes the grant. |
| Change labels | Classification, owning department, or both. |

Two rules are worth calling out:

- Nobody can grant access to themselves. Someone who is both a knowledge admin and an employee could otherwise open any restricted document alone. It now takes a second person. Two admins can still do it for each other, which only the audit trail shows.
- A grant to someone who can't read documents is refused. It would be stored and never honoured, and the admin would believe access had been given.

Managing a document does not require being able to read it. An admin looks a document up by its ID to change its audience, without that giving them its content.

### Chunks

A chunk has no classification, department or grants of its own. It points to its document, and the decision is always made on the document. A chunk therefore can't be more open than the document it came from, and a change to the document applies to all its chunks at once.

## More decisions

### Restricted documents need a named grant, even for executives

The original assumption was that executives read everything. I narrowed it: executives read every `general` and `departmental` document in every department, but a `restricted` document is for the people named on it and nobody else. The reasoning and the alternatives are in [decision record 0010](adr/0010-restricted-documents-need-a-grant.md).

### No per-document role lists

A document could carry a list of allowed roles as well as a classification. I left that out. Classification already expresses "which kind of user", and a second list that says the same thing in a different way is a place for the two to disagree. Exceptions go through grants, which name a person and can expire.

### No default classification

Covered above. A missing label is an error at ingestion, not a silent choice.

## Not covered yet

- Granting on behalf of a group or a project team. Grants are per person.
- An approval step before a grant takes effect.
- Automatic review of grants that never expire.
