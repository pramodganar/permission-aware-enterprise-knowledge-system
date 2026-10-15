# Requirements: Permission-Aware Enterprise Knowledge System

Status: Draft
Date: October 2026

## Business problem

Employees lose time looking for answers that already exist in internal documents, and the first attempt to fix this with an AI assistant exposed confidential content.

### Background

AcmeCorp is a company of about 1,200 employees across seven departments: Engineering, Finance, HR, Sales, Legal, Operations and Executive. Its internal knowledge lives in several places: policies on a shared drive, engineering documentation in a wiki, SOPs and contracts in department folders, and strategy papers in a restricted executive folder. Each of these has its own access rules, managed by the owning department.

### Current workflow

This is what happens today when an employee needs an answer, for example a sales rep checking how much discount they can offer without approval:

1. Search. The employee searches the shared drive and the wiki separately, by keyword. They need to know where the document lives and roughly what it is called.
2. Guess. They find several versions of the policy and can't tell which one is current.
3. Ask. They message a colleague or raise a request with the owning department. HR, Finance and senior engineers answer the same questions again and again.
4. Wait and act. The answer arrives later, with no source attached, and the employee acts on it.

Steps 1 to 3 are manual, and the time they take is the cost. Step 4 is the risk: a decision made on an answer nobody can check.

### Why the first AI pilot was stopped

IT ran a pilot of an AI assistant over these documents to remove that friction. The pilot indexed everything into a single vector store. During testing, a Sales employee asked a general question about bonuses and the answer quoted a draft executive compensation paper. Security stopped the pilot.

The cause was not the model. The assistant had no concept of who was asking: once documents were embedded into one index, the access rules of the source systems were gone, and any employee could reach any content by asking the right question. Adding "do not reveal confidential information" to the prompt was proposed and rejected, because the confidential text would still be retrieved and sent to the model on every such query.

### What is needed

AcmeCorp needs a way for employees to ask questions in plain language and get sourced answers, where:

- a user only ever gets answers built from documents they are already allowed to read,
- that guarantee is enforced by the system and can be tested, without relying on the model's behaviour,
- every query leaves an audit record that Security can review.

The first two points are the condition for Security to approve a rollout. Answer quality matters, but it is secondary to them.

### Who is affected

- Employees in every department, who lose time searching or work from outdated copies.
- HR, Finance and senior engineers, who absorb the repeated questions.
- Security and Legal, who carry the risk if confidential material is exposed.
- IT, who own the assistant and need something they can operate and defend in a review.

*AcmeCorp is a fictional company and the scenario above is written for this project. It is not based on a real customer.*

## Users

Each employee can read their own department's documents plus general company documents. Executives can read those of every department. Restricted documents are the exception: they need a named grant, for executives too. The full personas, with the questions each one should and should not get answered, are in [personas.md](personas.md).

| User | Department | Example question |
|---|---|---|
| Alice | Engineering | How does our deployment pipeline handle rollbacks? |
| Bob | Finance | What is the approval limit for vendor payments? |
| Carol | HR | What is the process for a performance improvement plan? |
| David | Sales | What discount can I offer without approval? |
| Emma | Legal | What is our data retention obligation for contracts? |
| Frank | Operations | What is the SOP for a warehouse incident? |
| Grace | Executive | What is the executive compensation strategy? |

There are also people who never ask a question but still matter:

- Security / compliance: wants proof that nothing leaks, and that the proof doesn't rely on the LLM behaving.
- Document owners: need to set who can see their documents when they upload them.
- Whoever runs the system: needs health checks, logs and basic metrics.

In a real company the security team can block the whole thing, so I've tried to write the security requirements in a way that can be tested.

## Approach

Permissions are applied at retrieval time, before anything is sent to the LLM.

```
User -> Auth -> Identity -> Authorization -> Filtered retrieval -> Context -> LLM -> Answer + citations
```

The LLM only gets chunks the user is already allowed to read. That means I don't have to trust the prompt to keep secrets, and a prompt injection can't leak something the model was never given.

Answers come with citations, and every query goes into an audit log.

## Functional requirements

### Identity

- FR-1: Users log in and get an identity (user, department, roles).

### Documents

- FR-2: Admins can ingest PDF, DOCX and TXT documents. A document with no owning department or classification is rejected at ingestion.
- FR-3: Each document is stored with department, classification, permissions, source, version and timestamps.
- FR-4: Admins can change a document's permissions, replace it with a new version, or delete it. Removed or replaced content stops showing up in results.
- FR-5: Users can list the documents they are allowed to read. The list goes through the same permission check as search.

### Indexing

- FR-6: Documents are split into chunks, and each chunk points to the document it came from, which is where access is decided.
- FR-7: Chunks are embedded and stored in a vector index that can be saved, reloaded and versioned.

### Search and answers

- FR-8: A user asks a question and gets back the most relevant chunks they are allowed to read (search).
- FR-9: Retrieved chunks can be reranked before use. Whether they are is decided by measuring it on the corpus (decision record 0012).
- FR-10: The answer is generated only from the retrieved context (Q&A).
- FR-11: Every answer lists the sources it used.
- FR-12: If nothing the user can access answers the question, the system says so and doesn't guess.

### Audit

- FR-13: Every query, the access decision and the chunks used are written to an audit log.
- FR-14: Security reviewers can read the audit log. Other users can't.

### Interfaces

- FR-15: Everything is available through an HTTP API (auth, documents, search, Q&A, health).
- FR-16: A small web UI for login, asking questions and viewing answers with citations.
- FR-17: An evaluation script for retrieval quality, answer quality, citations, latency and permission leakage.

## Non-functional requirements

### Performance and cost

- NFR-1 Latency: aiming for p95 under 5 s end to end and under 500 ms for retrieval, on the AcmeCorp data on my own machine. End to end mostly depends on the LLM provider, so the retrieval number is the one I control. First guess, not measured yet.
- NFR-2 Filter overhead: the time added by the permission filter is measured separately, so I can say what the security costs in latency.
- NFR-3 Cost per query: the context sent to the LLM is capped (number of chunks and total tokens), so one question can't run up an unbounded bill.

### Reliability

- NFR-4 Fail closed: if authorization can't be evaluated for any reason, return nothing. Never fall back to unfiltered results.
- NFR-5 Degrade, don't break: if the LLM provider is down or slow, search still works and Q&A returns a clear error. LLM calls have a timeout and a limited number of retries.
- NFR-6 Index consistency: the index records which embedding model and chunking settings built it. A query embedded with a different model is refused instead of quietly returning bad matches.
- NFR-7 Freshness: a document is searchable once its ingestion run finishes. There is no live sync.

### Operability

- NFR-8 Structured logs and per-stage timings for every request, tied together by a request ID.
- NFR-9 Application logs don't contain document text or full questions. That content goes only to the audit log, which has its own access control.
- NFR-10 Same env-variable config for local and Docker.

### Maintainability

- NFR-11 Swappable parts: LLM provider, embedding model, vector store and database are chosen through config.
- NFR-12 Authorization starts as RBAC but shouldn't block a move to attribute-based rules later.
- NFR-13 Unit, integration and security tests with pytest, run in CI. A failing security test blocks the merge.
- NFR-14 Type hints, small modules, no hard-coded secrets or paths.

### Scale

- NFR-15 The AcmeCorp corpus is small. I'll note what would have to change at larger scale but I'm not claiming to have tested it.

## Security requirements

This is the main point of the project.

### Enforcement

- SR-1: Unauthorized chunks are dropped during retrieval. They never reach the reranker, the context builder or the LLM.
- SR-2: Nothing in this list can be met by adding an instruction to the prompt.
- SR-3: One place decides access. Search, Q&A, the document list and citations all go through the same authorization check, and no debug, admin or evaluation endpoint returns content without it.
- SR-4: Deny by default. A chunk with missing or broken permission metadata is not returned to anyone.
- SR-5: A chunk inherits its document's permissions and can't be more open than the document.

### Identity

- SR-6: Identity comes from the verified token only, never from the request body or the question text. Tokens are signed, expire, and are rejected if either check fails.
- SR-7: Passwords are stored hashed, never in plain text, and never logged.
- SR-8: Changing a user's role, deactivating a user, or changing a document's permissions takes effect without re-embedding everything. A deactivated user's existing token must stop working. All of these take effect on the next request (see [architecture.md](architecture.md)).
- SR-9: Admin rights and read rights are separate. Being able to upload or manage documents doesn't grant access to their content through search.

### Leakage paths

- SR-10: Don't leak that a document exists. No access and no such information look the same to the user. No titles, counts, scores or citations from documents they can't see, and error messages don't mention them either.
- SR-11: Document text is untrusted. Instructions hidden in a document must not widen anyone's access. This should follow from SR-1, and I'll test it.
- SR-12: No caching of results or answers across users with different permissions.
- SR-13: With a hosted LLM, authorized content still leaves the system. The provider is configurable so that can be controlled.
- SR-14: The database and the vector index hold everything, so only the service reads them. Embeddings are treated as being as sensitive as the text they came from.

### Audit

- SR-15: Audit log records who asked, when, what, which chunks came back and the decision. It's append-only, and access to it is restricted because queries can be sensitive too.
- SR-16: Admin actions (ingest, permission change, delete) and failed logins are audited as well.

### Platform

- SR-17: Keys come from the environment, never from code or the repository.
- SR-18: API inputs are validated. Uploads are limited by file type and size, and an uploaded file is treated as untrusted when it is parsed.
- SR-19: Rate limiting is at least designed, per user.

Not covered: encryption at rest and network-level controls. They matter in production but are infrastructure concerns outside this project.

### The test I keep coming back to

Alice (Engineering) asks "What is the executive compensation strategy?"

- Retriever returns zero chunks from Executive documents.
- The LLM gets none of that content.
- She's told there's no information available to answer the question.
- The query is in the audit log.

Grace (Executive) asks the same thing and gets an answer with sources.

## Success metrics

Nothing is measured yet. These are targets.

### Security (pass/fail)

These are measured on probe queries: a question paired with a user who is not allowed to read the documents that answer it. The "must be refused" lines in [personas.md](personas.md) are the starting set, plus prompt-injection variants of them.

| Metric | Definition | Target |
|---|---|---|
| Permission leakage rate | probe queries where any unauthorized content is exposed, at retrieval or in the answer / all probe queries | 0% |
| Unauthorized retrieval rate | probe queries where the retriever returns at least one unauthorized chunk / all probe queries | 0% |
| Unauthorized answer rate | probe queries where the answer or its citations contain unauthorized content / all probe queries | 0% |
| Existence leakage | probe queries whose response differs from the response to a question no document answers | 0 |
| Audit coverage | queries with a matching audit record / all queries | 100% |

Anything short of these is a bug. Results are reported as counts ("0 of N"), because 0% over twenty probes says much less than 0% over two thousand.

### Over-blocking

A system that returns nothing to anyone has a perfect leakage score, so the security numbers mean nothing alone. They are always reported next to these:

| Metric | Definition | Target |
|---|---|---|
| Wrongful refusal rate | questions the user is allowed to have answered, and the documents do answer, that get refused / all such questions | set after baseline |
| Control pass rate | probe questions that the authorized user (for example Grace) gets answered / all probe questions | 100% |

The control pass rate is what shows a refusal came from the permission filter and not from a missing document.

### Retrieval quality

Measured against a labelled set of questions with known relevant chunks.

| Metric | Definition |
|---|---|
| Recall@K | share of the relevant chunks that appear in the top K |
| Precision@K | share of the top K that are relevant |
| MRR | average of 1 / rank of the first relevant chunk |

### Answer quality

| Metric | Definition |
|---|---|
| Faithfulness | share of claims in the answer that are supported by the retrieved context |
| Answer relevance | whether the answer addresses the question asked |
| Citation correctness | share of citations that point to a chunk which supports the claim it is attached to |
| Correct abstention | questions with no answer in the user's accessible documents where the system declines instead of guessing |

How faithfulness and relevance are judged (by hand, by an LLM judge, or both) is decided in the evaluation phase.

### Latency

p50 and p95 for the whole request and for each stage, plus the permission filter overhead on its own.

I'll set targets for retrieval quality, answer quality and wrongful refusals once I have a first baseline, since I don't want to pick numbers out of the air.

Quality is measured per user against the documents that user can read. Otherwise Alice's recall would drop every time the system correctly hides an Executive document from her.

In a real rollout you'd also track time saved and how many repeat questions stop reaching HR/Finance/IT. I can't measure that with synthetic data and no users, so I won't report it.

## Assumptions

- All data is synthetic.
- Users, departments and roles are seeded locally. In production they'd come from the identity provider.
- Permissions are set at ingestion. In production they'd be synced from the source systems' ACLs.
- Access depends on department, classification and individual grants. The rules are in [authorization.md](authorization.md).
- Executives read general and departmental documents of every department. Restricted documents, such as an individual HR case file, need a named grant even for executives.
- Small corpus (tens of documents), English, text only.
- Ingestion is a batch job run by an admin.
- Single company, no multi-tenancy.

## Out of scope

- SSO / directory sync with a real identity provider
- Connectors that sync permissions from SharePoint, Confluence etc.
- OCR, images, complex tables
- Redaction inside a chunk (access is per chunk)
- Multi-turn chat memory. History carries earlier content forward, and that needs its own security thinking.
- Multi-tenancy
- Fine-tuning
- Non-English documents
- HA deployment and load testing

## Failure handling

| What goes wrong | What should happen |
|---|---|
| Authorization can't be evaluated | Return nothing |
| Chunk has no usable permission metadata | Nobody gets it |
| No authorized chunk is relevant | Say there's no information available |
| LLM provider is down | Clear error, no fallback to unfiltered or another user's cached content |
| Document contains injected instructions | Access doesn't change |
| Token expired, missing or tampered | Reject before any retrieval |
