# Personas

Seven employees, one per department, plus three roles that run or review the system. The same seven are used as seed users, in the security tests and in the evaluation set, so what each one can and cannot see is written down here once.

## Access at a glance

Every document has one owning department. "General" means company-wide documents such as the code of conduct, the leave policy or the travel policy.

| Persona | General | Eng | Finance | HR | Sales | Legal | Ops | Executive |
|---|---|---|---|---|---|---|---|---|
| Alice (Engineering) | yes | yes | no | no | no | no | no | no |
| Bob (Finance) | yes | no | yes | no | no | no | no | no |
| Carol (HR) | yes | no | no | yes | no | no | no | no |
| David (Sales) | yes | no | no | no | yes | no | no | no |
| Emma (Legal) | yes | no | no | no | no | yes | no | no |
| Frank (Operations) | yes | no | no | no | no | no | yes | no |
| Grace (Executive) | yes | yes | yes | yes | yes | yes | yes | yes |

This is the expected behaviour, not the implementation. How it is modelled (roles, classifications, per-document grants) is decided in the authorization design.

## Employees

### Alice, backend engineer (Engineering)

Uses it for architecture decisions, runbooks and on-call procedures, mostly when she is in the middle of something and doesn't want to dig through the wiki.

- Should get an answer: "How does our deployment pipeline handle rollbacks?"
- Should get an answer: "How many days of annual leave do I get?" (general)
- Must be refused: "What is the executive compensation strategy?" (Executive)
- Must be refused: "What are the salary bands for senior engineers?" (HR)

The salary band question is the interesting one. It is about engineers, but HR owns the document. Access follows who owns the document, not what the question is about.

### Bob, financial analyst (Finance)

Uses it for approval limits, expense rules, and month-end close procedures.

- Should get an answer: "What is the approval limit for vendor payments?"
- Must be refused: "What was the root cause of the last production outage?" (Engineering)
- Must be refused: "What acquisitions is the company considering?" (Executive)

### Carol, HR business partner (HR)

Uses it for policy wording when advising managers, and for the steps of formal HR processes.

- Should get an answer: "What is the process for a performance improvement plan?"
- Must be refused: "What is next year's headcount budget?" (Finance)
- Must be refused: "What does the customer data retention design look like?" (Engineering)

### David, account executive (Sales)

Uses it during deals: discount rules, contract approval steps, what he is allowed to promise.

- Should get an answer: "What discount can I offer without approval?"
- Must be refused: "What is the status of the dispute with our supplier?" (Legal)
- Must be refused: "How are executive bonuses calculated?" (Executive)

David is the persona from the pilot incident in the problem statement, so his bonus question is the first regression test I want to pass.

### Emma, legal counsel (Legal)

Uses it for contract standards, retention obligations and regulatory policies.

- Should get an answer: "What is our data retention obligation for contracts?"
- Must be refused: "What discount floor was approved for enterprise deals?" (Sales)
- Must be refused: "Which employees are on a performance improvement plan?" (HR)

### Frank, operations supervisor (Operations)

Uses it on shift for SOPs and incident procedures. He needs short, exact answers with the SOP section they came from.

- Should get an answer: "What is the SOP for a warehouse incident?"
- Must be refused: "What is the vendor payment approval limit?" (Finance)
- Must be refused: "What is the company's three-year strategy?" (Executive)

### Grace, chief operating officer (Executive)

Uses it to pull facts from across departments before a decision.

- Should get an answer: "What is the executive compensation strategy?"
- Should get an answer: any of the questions above.

Grace is not refused any general or departmental document, but her queries are cited and audited like everyone else's. Restricted documents are different: she needs a grant for those like anyone else. She is also the control case: if Grace gets an answer and Alice does not, for the same question, the filter is doing the work and not a gap in the documents.

## A refusal looks the same as "not found"

When Alice asks about executive compensation, she gets the same response as she would for a question no document covers. She is not told that a document exists and that she lacks access to it. The trade-off is that the system can't point her to the right department to ask.

## System roles

These are roles, not extra people. Any employee could hold one in addition to their department.

- Knowledge admin: ingests documents and sets their owning department and permissions. Being able to upload a document does not give the admin the right to read every document through search.
- Security reviewer: reads the audit log to check who asked what and what came back. Does not get document content through this role.
- Operator: keeps the service running. Needs health checks, logs and timings, and should not need document content to debug.

## How these are used later

- Seed data: the seven employees are the seeded users.
- Security tests: every "must be refused" line becomes a test that checks the retriever returns zero chunks from the forbidden department.
- Evaluation: retrieval quality is scored per persona, against only the documents that persona can read.

## Questions answered in the authorization design

These were open when the personas were written. The answers are in [authorization.md](authorization.md).

- Does every member of a department see every document in it? No. A document can be classified as restricted, and then only people named in a grant can read it.
- Can someone get temporary access for a cross-team project? Yes, through a grant with an expiry date. A user still belongs to one department.
- Should executives see everything, including individual HR cases? No. Executives read general and departmental documents of every department, but restricted documents need a grant for them too.
