# Demo scenario

How I show the system to someone, in about ten minutes: what to do, what will appear, and what to say. Part A runs on the public demo page and needs nothing installed. Part B runs on the full stack, with real sign-in, the admin and the audit trail.

Nothing here is a hoped-for result. Every outcome in Part A is the one the evaluation measured with Groq ([evaluation](evaluation.md)), and a test checks each row against the committed results. Every step of Part B is done through the API by a test named next to it ([test_demo_scenario.py](../tests/integration/test_demo_scenario.py)). AcmeCorp is fictional.

## Before you start

- **Wake the public page** two minutes before: [https://permission-aware-enterprise-knowledge-system-ij67laxbihrkmrqfw.streamlit.app/](https://permission-aware-enterprise-knowledge-system-ij67laxbihrkmrqfw.streamlit.app/). A free app sleeps when nobody has used it for a while, and takes about a minute to load the company again.
- **Start the full stack** for Part B:

  ```
  python -m scripts.make_secrets
  docker compose up --build --wait
  ```

  The UI is at http://127.0.0.1:8501, the API at http://127.0.0.1:8000, and every person's password is in `secrets/seed_password`. No model is needed for Part B.
- **Open two browser windows**, one of them private, so two people can be signed in side by side. And a terminal, for two `curl` commands.

## Part A: same question, different people (about 4 minutes)

Say first: *"Most RAG demos put every document into one index and ask the model not to tell. Here the system works out what each person may read before it searches, so the model never sees anything else. Watch the same question asked by two people."*

On the page, use the **Compare two people** tab for each pair.

| Step | Person | Question | What you'll see |
|---|---|---|---|
| A1 | David | How are executive bonuses calculated? | No information |
| A2 | Grace | How are executive bonuses calculated? | An answer citing Executive Compensation Strategy 2027 |
| A3 | Frank | What is the vendor payment approval limit? | No information |
| A4 | Bob | What is the vendor payment approval limit? | An answer citing Vendor Payment Approval Limits |
| A5 | Bob | What acquisitions is the company considering? | No information |
| A6 | Emma | What acquisitions is the company considering? | An answer citing Project Falcon Acquisition Paper |

What to say at each pair:

- **A1 and A2, executive bonuses.** David is in Sales. *"He gets 'No information available', the same words as for a question no document covers. So he can't tell whether the document exists. The model wasn't even called: nothing David may read is relevant, so it stopped at the relevance threshold. Grace, an executive, gets the formula and its source. Same question, same code, same model."*
- **A3 and A4, a departmental document.** *"Vendor approval limits belong to Finance. Frank, in Operations, gets nothing. Bob, in Finance, gets the tiers. That's the department rule, and it's checked twice: once in the database query that decides what can be searched, and again on every result."*
- **A5 and A6, a restricted document.** *"The acquisition paper is restricted: only people named on it may read it. Emma, from Legal, has a grant that expires in 60 days. Bob doesn't. Being senior doesn't help here: even an executive needs a grant for a restricted document."*

The sidebar shows how many documents each person may read: point at it when switching person.

## Part B: who decides, and what is recorded (about 5 minutes)

Sign in at http://127.0.0.1:8501. Each step names the test that does the same through the API.

**B1. Henry manages documents without reading them.** Sign in as `henry@acmecorp.example`. Open **Manage documents**: all 17 documents, by ID, with their labels and grants, and no titles. He has no **Documents** or **Ask and search** page at all: pages are offered by the permissions the service reports. *"Henry is the knowledge admin. He decides who may read a document, but he can't read it. Nothing on his pages shows a title or a line of text."* Choose the restricted document from the Executive department, the one with grants to Grace and Emma, and copy its ID. That's the acquisition paper.
Shown by `tests/integration/test_demo_scenario.py::test_b1_henry_manages_every_document_without_reading_one`.

**B2. A hidden document looks like a missing one.** In the private window, sign in as `david@acmecorp.example`. **Documents** lists the 4 he may read. In the terminal, sign in as David and ask for the copied ID, then for an ID that doesn't exist:

```
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login -H 'Content-Type: application/json' \
  -d "{\"email\": \"david@acmecorp.example\", \"password\": \"$(cat secrets/seed_password)\"}" \
  | python -c "import json, sys; print(json.load(sys.stdin)['access_token'])")
curl -s http://127.0.0.1:8000/documents/<the copied ID> -H "Authorization: Bearer $TOKEN"
curl -s http://127.0.0.1:8000/documents/0badc0de-0000-4000-8000-000000000000 -H "Authorization: Bearer $TOKEN"
```

Both are a 404 with the same body, apart from the request ID. *"Forbidden and missing are one response, so trying IDs maps nothing. And the IDs are random, so they don't count what's stored."*
Shown by `tests/integration/test_demo_scenario.py::test_b2_a_hidden_document_is_the_same_404_as_a_missing_one`.

**B3. A grant applies at once, and so does taking it away.** As Henry, on the acquisition paper, open **Give someone access** and grant `david@acmecorp.example`. In David's window, reload **Documents**: the paper is there, and he can open it. Back as Henry, **Revoke** it. David's next reload doesn't have it, and opening it is the 404 again. *"No re-indexing, no waiting for a token to expire. Permissions live in the database and are read on every request."*
Shown by `tests/integration/test_demo_scenario.py::test_b3_a_grant_applies_at_once_and_so_does_revoking_it`.

**B4. The security reviewer sees all of it.** Sign out Henry and sign in as `irene@acmecorp.example`. Open **Audit trail** and filter by `david@acmecorp.example`. His refused lookup is there as `denied`, and the made-up ID as `not_found`: the responses were identical, but the trail tells them apart. Filter by `henry@acmecorp.example` for the grant and the revoke. *"Every question, search, document opened, refusal and change is recorded in the same transaction. If the record can't be written, the request fails. Each record is sealed, so a changed one is found by `python -m scripts.audit check`."*
Shown by `tests/integration/test_demo_scenario.py::test_b4_the_reviewer_sees_every_step_and_what_the_responses_hid`.

**B5. Signing out ends the token everywhere.** In David's window, **Sign out**. In the terminal, run the same `curl` with `$TOKEN` again: 401. *"Signing out withdraws every token David holds, on every device, not just the one in this browser."*
Shown by `tests/integration/test_demo_scenario.py::test_b5_signing_out_withdraws_the_token`.

## If something goes wrong

- **The public page is still loading.** Start with Part B, and come back to it.
- **Every answer fails, or says the page's budget is spent.** The page shares one budget for the model, 60 questions an hour, between every visitor. Answers already given are still shown. The point of Part A survives without answers: run the full stack with no model and sign in as each person and press **Search only** on **Ask and search**, which shows the passages each person gets, or nothing.
- **No network.** Part B needs none: the containers fetch nothing. Asking says answers are switched off, and search still works.
- **Don't use Carol's case-file question** ("Which employees are on a performance improvement plan?"). Carol may read the case file, but in the evaluation the passage that answers it ranks below the threshold, so she gets no information. That is the one wrongful refusal in the results. If someone asks, say so: it's a retrieval miss, not an access decision, and the report shows it.

## What not to claim while showing it

- That answers are always right. They were measured on my own questions, over documents I wrote.
- That the person picker on the public page is sign-in. It's for comparing people over fictional data, and is the reason the page may only ever hold AcmeCorp.
- That prompt injection is solved. A planted instruction can't widen access or carry text out through a link, but it can still make an answer wrong.

## Questions people ask

- **"What if the model is tricked into leaking?"** It has nothing to leak: it's only given the asker's own passages. The evaluation ran a model that repeats everything it's given, with the threshold off, and nothing leaked.
- **"Can the admin give himself access?"** No: a grant can't name the admin making it, and a label change that would let them read the document is refused. Henry holds no reading role, so it matters for an admin who is also a reader. It closed a hole I found by attacking the system myself.
- **"Why not filter the results after searching?"** Hidden documents would take places in the top results, so a person would get fewer or worse answers exactly when something hidden matches. That's a signal in itself.
- **"How fast is it?"** Retrieval is 7.4 ms and an answered question 1.6 s at p95; the permission filter costs 2.6 ms ([evaluation report](evaluation-report.md)).
- **"Would it work at a real company?"** The access model would. The next steps are users from the company's identity provider, documents synced with their source permissions, PostgreSQL, and a vector store that filters by itself ([deployment](deployment.md)).
