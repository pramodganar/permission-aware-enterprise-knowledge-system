# Authorization

Status: Draft. This covers the role model. The rules for individual documents (classification and per-document grants) are added to this document next.
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
| `documents:manage` | Ingesting documents, changing their permissions, replacing and deleting them |
| `audit:read` | Reading the audit log |

## Which role grants what

| | `employee` | `executive` | `knowledge_admin` | `security_reviewer` |
|---|---|---|---|---|
| `documents:read:general` | yes | yes | | |
| `documents:read:own_department` | yes | yes | | |
| `documents:read:all_departments` | | yes | | |
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
| Grace | Executive | `executive` | Everything |
| An IT administrator | any | `knowledge_admin` | Nothing through search |

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

## Not covered yet

- How a document's classification and owning department combine with these permissions into a yes or no for one document.
- Per-document grants for individuals.
- Temporary access.
