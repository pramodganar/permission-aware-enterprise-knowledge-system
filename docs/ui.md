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
