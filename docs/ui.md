# UI

The web UI (FR-16), being built in phase 10; this page grows with it.

The UI is a separate process that **only calls the HTTP API** ([architecture](architecture.md)). It holds no rules of its own: it never imports the service's code, reads the database or loads the index. Everything it shows came from an API response, decided by the service for the token it sent. Hiding a page from someone in the UI is a convenience, never the check; the API refuses them anyway.

This is different from the [demo page](demo.md), which calls the application code directly so it can run on Streamlit Community Cloud without an API server. The demo is the one exception, and a test checks it stays the only one.

## The client

`ui/client.py` is the UI's one way to the service: a call for every route the UI uses, with the API's responses as typed models.

```python
api = Api("http://127.0.0.1:8000")
login = api.login("david@acmecorp.example", password)
answer = api.ask(login.token, "How many days of annual leave do I get?")
```

**The models mirror the API's, written out again rather than imported**, because the UI must not import the service. A contract test compares every UI model with the API model it mirrors, field by field and required by required, so the two can't drift apart unnoticed. A field a newer service adds is ignored, so the UI keeps working against it.

**Every failure is an error with the service's own code and message**, and the request ID to quote:

| Status | Error | Meaning for the UI |
|---|---|---|
| 401 | `NotSignedIn` | Sign in again: the token is missing, expired, or its account was deactivated |
| 403 | `NotAllowed` | This account may not do this |
| 404 | `NotFound` | No such thing, or one this account may not see; the service makes them look the same |
| 422 | `InvalidInput` | Refused, with the reason and the fields |
| 429 | `TooManyAttempts` | Wait `retry_after` seconds |
| 503, or no answer | `Unavailable` | The service, or its model, can't answer now |

A reply the UI doesn't understand, such as a proxy's HTML error page, is an error too, never a half-read model.

**The token** is only ever sent in the `Authorization` header: never in a URL, never in a body, never logged. A test records every request the client makes and checks. An empty token isn't sent at all. A document ID is always a whole number in the path, and an email in a path is quoted, so neither can turn a call into a different route.

A question gets a longer timeout than other calls, six minutes, because a local model can take that long.

## Tested against the real service

The client's integration tests start the service on a free local port and talk to it over real HTTP, as the UI will: every call, as each kind of person, and every way a call can fail ([tests/integration/test_ui_against_the_api.py](../tests/integration/test_ui_against_the_api.py)).

## Running it

The UI needs the service running, and finds it at `UI_API_URL` (default `http://127.0.0.1:8000`):

```
uvicorn app.main:serve --factory --no-access-log
streamlit run ui/app.py
```

## Signing in

The page starts at a sign-in form. Once signed in, the sidebar shows who the person is, with their department and roles, and a sign-out button, and the page says what their account can do: ask and search, manage documents, or review the audit trail. Those lines come from the permissions the service reports, not from the UI's own idea of the roles.

**What is kept.** The token, with when it stops working, and who the service last said the person is, in that browser's session on the Streamlit server. Not in the URL, a cookie or the browser's storage, and never shown on the page.

**The password is used once and dropped.** A widget's value stays in the visitor's session on the server even when the form is cleared in the browser, which testing showed: after a failed sign-in the password was still there. So the sign-in runs in the submit button's callback, which blanks the field before calling the service. The browser's message for that one redraw carries the password, as any form submission does, and by the next interaction there is no trace of it. A test checks every part of the session for it.

**Who they are is asked again on every interaction.** So a role taken away or an account deactivated applies on the person's next click, as it does in the API. A token that has expired, or that the service no longer accepts, signs them out: "Your session has ended. Sign in again." A service that can't be reached doesn't sign anyone out; the page says it's unreachable and carries on when it's back.

**A failed sign-in says the same whatever was wrong**: "Sign-in failed. Check your email and password." for a wrong password, an unknown email or a deactivated account, as the service does. Only two failures say more, because they tell the person what to do and nothing about anyone's account: too many attempts, with how many minutes to wait, and a service that can't be reached.

**Signing out** forgets the token and who the person was. The token itself still works at the service until it expires, because tokens here can't be withdrawn early ([decision record 0004](adr/0004-identity-only-token.md)); with 30-minute tokens that is the window. A shared computer should be signed out of, and the browser closed.

## Asking and searching

Someone who may read documents gets an **Ask and search** page. The pages in the sidebar are offered afresh on every interaction from the permissions the service reports, so the page disappears on the next click once that permission is taken away, and Henry and Irene, who may read nothing, aren't offered it. The API answers them with "no information" anyway.

**Ask** sends the question to `/ask` and shows the answer with the sources it cites, each opening to the passage it came from. **Search only** sends it to `/search` and shows the passages, best first, with how relevant each was, without asking the model. With no model configured, asking says "Answers are switched off on this service. Search still works." and search does.

An answer is flagged when it needs care:

- **cut off**: it reached the length limit and stops part way;
- **cites nothing**: no source backs it, so check the documents before relying on it.

"No information available to answer this question." is shown as it comes, whatever the reason; the service doesn't say, and neither does the UI.

**Narrowing.** A question can be narrowed to some departments, classifications or documents. The choices are taken from the documents the service lists for that person, so they never name a document they may not read. They only narrow: the service applies them inside the person's access ([retrieval](retrieval.md)). An empty question, or more documents or departments than the service accepts, is caught before sending; a question over the service's length limit comes back with its reason.

**Plain text only.** The answer and every passage are drawn as plain text, never Markdown; see [Text from the service](#text-from-the-service).

**Nothing is kept.** An answer is drawn in the interaction that asked for it and not stored, so the next click clears it. There is nothing left in the session for whoever uses the browser next, and nothing stale on screen after someone's access changes. Signing out leaves neither the question nor the answer behind; a test checks the whole session.

## Documents

The **Documents** page, offered with asking to anyone who may read, lists the documents the service says they may read, by title, with department, classification, version and when each was last updated. It is `GET /documents` a page of 20 at a time, with the service's department and classification filters; the filter choices come from the documents they may read, as on the ask page. A filter that leaves fewer pages than the one being looked at goes to the last page.

**Opening a document** shows its whole text, with where it came from: department, classification, version, the file it was made from and its size. Only the choice of document is remembered between interactions, never its text: the document is fetched again on every interaction, so a grant taken away, or a document replaced or removed, shows on the next click. Each of those fetches is an opening as far as the service is concerned, and the audit trail records each one; that is accurate, because the text was sent again. A document that has gone between listing and opening says "This document isn't available. It may have been removed, or you may no longer have access." and nothing about which.

## Managing documents

Someone with the manage-documents permission (Henry) gets a **Manage documents** page. Managing a document isn't reading it, and the service never tells an admin a document's title, file name or text, so the page works with documents by ID. For a document it shows how it is managed, from `GET /admin/documents/{id}`: status, version, department, classification, file type, size, number of chunks, which document it replaced, who holds a grant and until when, and whether search can find it yet. A new document or version can't be found by search until the index is rebuilt, and the page says so.

From there an admin can:

- **give someone access**, until the end of a day (UTC) or until revoked, and **revoke** a grant;
- **change the labels**: only the ones that changed are sent;
- **replace it with a new version**: the page moves to the new document's ID;
- **withdraw it**, after ticking that nobody will be able to read or find it.

**Uploading** takes the file (`.txt`, `.pdf` or `.docx`, the types the pipeline reads), a title, the owning department, the classification and, for a restricted document, the readers, one per line or separated by commas. A missing file, title or department, or more than 50 readers, is caught before sending. Everything else is the service's to refuse, and the page shows its reason: an unknown department, a reader with no role that can read, a file it can't read, or one over the upload limit. The page moves to the new document's ID afterwards. The title the admin typed isn't shown back; the page knows documents by ID like the service does.

**Forms start again empty after each action.** A Streamlit form clears itself only when its run ends, and moving to the new document cuts the run short, which left the uploaded file in the form, ready to be sent a second time. Each finished action gives the forms new names, so they are drawn fresh. A test checks that after an upload no file is left in any uploader.

The lists offered, classifications and file types, are written out in the UI because it can't import the service; a test checks they are the service's.

## The audit trail

Someone with the audit permission (Irene) gets an **Audit trail** page: `GET /audit`, newest first, 50 records a page, in UTC. It can be narrowed by who did something (their email), what they did, which document, and a range of days, from the start of the first to the end of the last. A range that ends before it starts is caught before sending. A failed login for an email no account has shows as "(no such account)".

Questions and details are shown in a table, whose cells are plain text, so a question holding Markdown is shown as it was asked.

**Every look at the page is a read of the trail**, and the service records each one, so the newest record is usually the reviewer's own previous look. The filters sit in a form, so typing in them doesn't read the trail on every keystroke.

## Text from the service

A document, or an answer made from one, can hold an image link. Drawn as Markdown, the reader's browser would fetch that URL by itself, carrying whatever was put in it, without anyone clicking (threat T29 in the [threat model](threat-model.md)). Streamlit reads Markdown, images included, in headings, captions, messages and the labels of expanders, not only in `st.markdown`. So:

- **Long text is drawn as plain text** with `st.text`: answers, passages and whole documents.
- **Everything else from the service is escaped** before it is drawn anywhere Streamlit reads Markdown: titles, names, emails, department names and the service's messages. Every punctuation character gets a backslash, which in Markdown always shows the character itself, and line breaks are folded, so a title can't start a heading or a list ([ui/text.py](../ui/text.py)). A test renders each of eighteen hostile strings, images, links, raw HTML, tables, headings and Streamlit's own colour and maths syntax among them, through a Markdown parser and checks each comes out as exactly the text in one paragraph.
- **Tables and choice lists** draw their cells and options as plain text and need neither.

