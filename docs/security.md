# Security

Status: Written at the end of phase 12, against the built system.
Date: October 2026

This is the page to start from when reviewing the system's security. It says what the system promises, where in the code each promise is kept, what shows it is kept, how to run it safely, and what it doesn't cover. The detail is in the pages it links to:

- [Threat model](threat-model.md): every threat, its mitigation, its tests and what is left over.
- [Authorization](authorization.md): the access rules, as a table.
- [Audit trail](audit.md): what is recorded, and how changes to it are found.
- [Secrets](secrets.md): what is secret, where it lives, and how to replace it.
- [Decision records](adr/README.md): why the main choices were made.

Two tests keep this page true. Every security requirement in [requirements.md](requirements.md) must have a row in [the table below](#each-requirement-and-what-shows-it-is-met), and every test a row names must exist. Every default in [the settings table](#the-settings-that-matter) must be the service's real default. So the page can't claim a test that was renamed, skip a requirement, or quote a limit that was changed.

## The promise

**Nobody receives information from a document they may not read.** Not its text, its title, a passage, a citation, a count, or the fact that it exists. This is enforced by ordinary code before the language model is called, never by asking the model (decision record 0001). The model is given only text the person may read, so nothing it says, obeys or makes up can reach further.

The promise is about what the service hands out. It assumes the host, the database and the people who administer them can be trusted, and it can't stop a person working out a restricted fact from documents they may read (T12).

## Where it is kept

The path of a question, and what each step holds to:

1. **Who is asking comes from the token, and nothing else.** A token holds the user ID and an expiry, signed with HS256, and the algorithm is fixed in code, never read from the token (`app/auth/tokens.py`). The department, roles and whether the account is active are read from the database on every request, so a change applies on the next request with the same token (decision record 0004). Nothing in a body, header or question can say who is asking.
2. **Each route checks the one permission its audience needs.** A test walks every route the service serves and fails on a route nobody decided an audience for (`tests/security/test_api_routes.py`).
3. **One module decides read access** (`app/authorization/service.py`). It gives the same rule in two forms: `readable_documents`, a database query, and `can_read`, the same rule in Python. Both start from "no".
4. **The search only looks inside the person's scope.** The scope is the list of chunks the person may read, worked out from the query for this question at this moment, and handed to the vector index as the only IDs it may return (`app/retrieval/scope.py`, `app/retrieval/search.py`, decision record 0003). Chunks have no permissions of their own: they are their document's.
5. **Every chunk found is checked again,** with `can_read` on its loaded document. The two forms of the rule are written differently on purpose, so the same mistake in both is unlikely (`app/retrieval/retriever.py`).
6. **Nothing relevant means "no information", and the model isn't called.** The response is the same whether nothing the person may read is relevant or the document they asked about is one they may not read (decision record 0005).
7. **The prompt holds the person's passages and question, escaped, and nothing about the person** (`app/rag/prompt.py`).
8. **What comes back is checked in code.** A citation is looked up in this answer's own passages and nowhere else (`app/rag/citations.py`). A link stays only if exactly that URL is in the passages; every other link and every image is taken out, so a planted instruction can't send the reader's content anywhere (`app/rag/links.py`).
9. **The UI shows text as text.** It talks only to the API, never imports the service, and escapes everything it draws, so a title or answer can't become a link or markup (`ui/client.py`, `ui/text.py`).
10. **Everything is recorded,** in a trail sealed record by record, and a request that can't be recorded fails instead (decision records 0006 and 0013).

Around every request:

- **A refusal looks like absence.** A document the person may not read gets the 404 of one that doesn't exist, with the same body and headers, after the same work (T11).
- **Error responses hold a fixed message,** never the input, the document, the rule or a traceback.
- **No response is stored by a cache:** every response carries `Cache-Control: no-store`, plus `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer` (`app/api/middleware.py`).
- **The log holds no content:** no question, answer, passage, title, password or token, at any level (T21).
- **Limits on everything a client controls:** body size, question length, context size, model output, failed logins, and questions and searches per person (T16, T24, T10).

## Each requirement and what shows it is met

The security requirements are SR-1 to SR-19 in [requirements.md](requirements.md#security-requirements). Each row names the tests that show the requirement is met; the [threat model](threat-model.md#where-each-check-lives) has the rest.

| Requirement | Met by | Shown by |
|---|---|---|
| SR-1 | The scope is the only list of IDs the index may return, and every chunk is checked again before anything uses it | `tests/security/test_retrieval_boundaries.py::test_no_retrieval_returns_anything_the_person_may_not_read`, `tests/security/test_restricted_search.py::test_no_search_returns_a_chunk_of_a_document_the_person_may_not_read`, `tests/unit/test_search.py::test_the_index_is_only_given_the_scope` |
| SR-2 | Access is decided before the prompt is built; the prompt's instructions are a second line, not the first | `tests/security/test_answer_boundaries.py::test_a_model_that_repeats_everything_leaks_nothing`, `tests/integration/test_evaluation_injection.py::test_obeying_in_full_does_no_harm` |
| SR-3 | One authorization module; every route depends on exactly the checks its audience needs | `tests/security/test_api_routes.py::test_each_route_depends_on_exactly_the_checks_its_audience_needs`, `tests/security/test_api_routes.py::test_no_route_gives_anyone_a_fact_or_title_from_a_document_they_may_not_read`, `tests/security/test_restricted_search.py::test_the_scope_and_the_second_check_agree_on_every_search` |
| SR-4 | Both forms of the rule start from "no"; a document whose labels don't fit the rules is readable by nobody | `tests/unit/test_chunks.py::test_chunks_of_a_document_with_a_broken_label_are_readable_by_nobody`, `tests/unit/test_permissions.py::test_user_with_no_roles_has_no_permissions` |
| SR-5 | A chunk has no permission columns: it is read through its document | `tests/unit/test_chunks.py::test_a_chunk_has_no_permission_columns`, `tests/integration/test_seed.py::test_each_person_reads_the_chunks_of_their_documents_only` |
| SR-6 | The token carries the user ID only, verified with a fixed algorithm; identity claimed anywhere else is ignored | `tests/security/test_unauthorized_access.py::test_identity_claimed_in_headers_or_parameters_is_ignored`, `tests/security/test_unauthorized_access.py::test_role_and_department_claims_inside_a_valid_token_are_ignored`, `tests/unit/test_tokens.py::test_token_signed_with_a_different_algorithm_is_rejected`, `tests/unit/test_tokens.py::test_expired_token_is_rejected` |
| SR-7 | Passwords are stored as Argon2id hashes; a failed login logs why, never what was typed | `tests/unit/test_passwords.py::test_hash_is_not_the_password`, `tests/integration/test_seed.py::test_the_password_is_stored_hashed`, `tests/integration/test_api_auth.py::test_a_failed_login_logs_why_but_not_what_was_typed` |
| SR-8 | Permissions are read from the database on every request, and the index holds none | `tests/security/test_unauthorized_access.py::test_deactivated_user_is_locked_out_with_a_still_valid_token`, `tests/integration/test_current_user.py::test_role_change_applies_on_the_next_request_with_the_same_token`, `tests/security/test_restricted_search.py::test_a_revoked_grant_stops_the_search_at_once_without_rebuilding`, `tests/integration/test_api_admin.py::test_relabelling_moves_a_document_to_another_audience_at_once` |
| SR-9 | Managing documents is a permission of its own, which reads nothing; an admin can't make a document readable by themselves | `tests/security/test_unauthorized_access.py::test_admin_who_can_ingest_still_cannot_read`, `tests/integration/test_api_admin.py::test_what_the_admin_sees_has_no_title_file_name_or_text`, `tests/security/test_authorization_bypass.py::test_no_manage_route_lets_an_admin_who_can_also_read_open_a_document_to_themselves` |
| SR-10 | No access looks exactly like no such document, and nothing a person gets back depends on what they may not read | `tests/security/test_non_interference.py::test_what_a_person_gets_back_is_the_same_whatever_they_may_not_read`, `tests/integration/test_permission_checks.py::test_hidden_document_and_missing_document_are_indistinguishable`, `tests/security/test_non_interference.py::test_a_refused_lookup_never_reads_the_document` |
| SR-11 | Follows from SR-1: a document's text can change an answer but not who may read what; links it plants are taken out | `tests/security/test_answer_boundaries.py::test_a_document_asking_for_more_gets_nothing_more`, `tests/security/test_answer_boundaries.py::test_a_planted_link_carries_nothing_of_the_readers_out` |
| SR-12 | Every response says it must not be stored; the demo keeps answers per person | `tests/integration/test_api_documents.py::test_the_list_must_not_be_stored`, `tests/integration/test_api_auth.py::test_the_token_response_must_not_be_cached`, `tests/unit/test_demo.py::test_a_kept_answer_is_never_given_to_someone_else` |
| SR-13 | The provider is a setting, `none` by default; only the prompt is sent, and nothing about the person is in it | `tests/unit/test_llm.py::test_only_a_prompt_can_be_sent`, `tests/security/test_answer_boundaries.py::test_nothing_about_the_person_reaches_the_model` |
| SR-14 | The UI only talks to the API; the index is local, never committed, and needs the database's protection | `tests/unit/test_ui_client.py::test_the_ui_never_imports_the_service` |
| SR-15 | Every question and search is recorded with the chunks given; records are insert-only and sealed; reading them needs its own permission | `tests/security/test_audit_completeness.py::test_a_call_records_what_the_table_says_by_whoever_made_it`, `tests/unit/test_audit_seals.py::test_a_record_changed_in_any_field_no_longer_matches_its_seal`, `tests/integration/test_api_audit.py::test_only_the_security_reviewer_can_read_the_trail` |
| SR-16 | Every change an admin makes, every refusal and every login are recorded | `tests/integration/test_api_audit.py::test_every_change_an_admin_makes_is_recorded_with_its_document`, `tests/security/test_audit_completeness.py::test_a_refused_call_records_one_denial_and_nothing_else`, `tests/security/test_audit_completeness.py::test_a_throttled_login_is_recorded_with_its_account` |
| SR-17 | Secrets come from the environment; every file and every commit is scanned for them | `tests/security/test_secrets.py::test_no_secret_is_in_any_tracked_file_or_anywhere_in_the_history`, `tests/security/test_secrets.py::test_the_models_key_goes_in_its_header_and_nowhere_else` |
| SR-18 | Strict request models, size limits before reading, parsers treated as untrusted, and generated inputs to every route | `tests/integration/test_api_admin.py::test_a_request_far_over_the_upload_limit_is_refused_before_it_is_read`, `tests/unit/test_fuzzing.py::test_a_damaged_pdf_gives_text_or_is_refused`, `tests/security/test_api_fuzzing.py::test_any_question_body_is_answered_or_refused_in_the_services_words` |
| SR-19 | Built, not only designed: questions and searches per person, in sliding windows, every call counted | `tests/integration/test_api_limits.py::test_the_eleventh_question_in_a_minute_is_refused_with_when_to_try_again`, `tests/integration/test_api_limits.py::test_a_question_about_what_you_may_not_read_counts_like_any_other` |

## How it is tested

The tests are written against the rules, not the code: who may read what comes from a table in `tests/questions.py`, written from the personas, not from the authorization module.

- **Boundary walks.** Every person asks every question, hostile ones included, through retrieval, through answers and through the API, and nothing they may not read comes back (`tests/security/`).
- **The worst model.** A fake model that repeats its whole prompt and cites every label it can think of. If that leaks nothing, no real model can, because the model only has what it was given.
- **Non-interference.** Two companies that differ only in documents one person may not read must give that person byte-identical responses on every route.
- **Every route.** The route walk fails on a route without a decided audience, a refusal that isn't recorded, or a route that records the wrong thing.
- **Generated inputs.** Damaged files, prompts built from the prompt's own markup, answers built from every way to write a link, and requests of every shape to every route (Hypothesis, the same examples on every run).
- **The evaluation.** 18 forbidden and injection probes measured through the API with real models, and none leaked ([evaluation report](evaluation-report.md)).
- **Deliberate breaks.** Each security change was checked by breaking the code on purpose, a check removed or an off-by-one put in, and running the tests to see that one fails. A break no test caught got a test, unless it turned out to change nothing.
- **Secrets and the trail.** `python -m scripts.scan_secrets` searches every file and every commit; `python -m scripts.audit check` checks every audit record against its seal.

## The settings that matter

The service's defaults, as `app/core/config.py` has them. A test compares this table with the code.

| Setting | Default | What it protects |
|---|---|---|
| `APP_ENVIRONMENT` | `local` | `production` hides the interactive docs and route list, refuses a signing key that looks typed, and stops the seed script |
| `APP_JWT_SECRET_KEY` | none, required | Signs tokens and, through a derived key, the audit seals. At least 32 characters |
| `APP_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | How long a stolen token works, at most |
| `APP_LOGIN_MAX_FAILURES` | `5` | Failed logins for one email in the window before the password isn't checked at all |
| `APP_LOGIN_FAILURE_WINDOW_SECONDS` | `900` | That window |
| `APP_ASK_LIMIT_PER_MINUTE` | `10` | Questions per person in any minute |
| `APP_ASK_LIMIT_PER_DAY` | `200` | Questions per person in any 24 hours: the cap on what one person can spend on the model |
| `APP_SEARCH_LIMIT_PER_MINUTE` | `30` | Searches per person in any minute |
| `APP_MAX_UPLOAD_BYTES` | `10485760` | The largest file; a body much larger is refused before it is read |
| `APP_MAX_QUESTION_CHARACTERS` | `1000` | The longest question |
| `APP_CONTEXT_MAX_CHARACTERS` | `6000` | The most document text sent to the model for one question |
| `APP_LLM_PROVIDER` | `none` | Where the prompt, which is document text, goes. `none` sends nothing anywhere |
| `APP_LLM_MAX_OUTPUT_TOKENS` | `600` | The longest answer the model may write |
| `APP_LLM_TIMEOUT_SECONDS` | `30.0` | The longest one model call may take |
| `APP_LLM_MAX_RETRIES` | `2` | Retries of a call the provider may recover from |

## Running it for real

What a deployment has to add, because the service doesn't do it itself:

- **Set `APP_ENVIRONMENT=production`**, and generate the signing key with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Changing the key signs everyone out and makes the existing audit seals stop matching ([secrets](secrets.md)).
- **Put TLS in front.** The service speaks plain HTTP; tokens and questions must not cross a network that way. Run it behind a proxy that terminates TLS, listening on this machine only, as `scripts/run.py` does by default.
- **Run one process.** The login throttle and the rate limits are in memory: two copies count separately, and a restart forgets. More than one needs a shared store such as Redis first.
- **Protect `data/`.** The database and the vector index hold every document, restricted ones included. Only the service's account should read them, and backups need the same care (T19).
- **Check the trail.** Run `python -m scripts.audit check` on a schedule, and keep the application log somewhere the database's administrators can't change: the log's "audit record sealed" lines are what show the newest records deleted ([audit](audit.md)).
- **Decide on the provider.** With a hosted model, every question sends the person's passages to that provider. For a real company that is a decision for whoever owns the data, made before anything is sent.
- **Load real permissions, not hand-typed ones.** The system enforces whatever labels documents were given (T17). In production they should come from the source system, or a second person should approve each upload.
- **Don't run the seed script.** It loads the fictional company, and it refuses to in production.

## What it doesn't cover

Read these with the [residual risks](threat-model.md#residual-risks). The main ones:

- A wrong label is enforced correctly (T17).
- Facts can be put together from documents a person may read (T12).
- An admin can widen access for someone else; the trail shows it, nothing stops it (T18).
- Text a person may read leaves the service when a hosted model answers (T23).
- Document IDs count the documents stored between the ones a reader sees (T36).
- Encryption at rest, network security, host hardening and key rotation are outside this project.

## Reporting a problem

See [SECURITY.md](../SECURITY.md).
