# Secrets

What the service has to keep secret, where each secret lives, who reads it, what someone could do with it, and how it is checked (SR-17, threat T20 in the [threat model](threat-model.md)).

## The secrets

| Secret | Setting | Read by | With it, someone can | Without it, the service |
|---|---|---|---|---|
| Token signing key | `APP_JWT_SECRET_KEY` | The API | Sign a token as anyone, the executive included, and check or forge the audit trail's seals | Refuses to start. There is no default |
| Language model key | `APP_LLM_API_KEY` | The API, to call the provider | Spend the provider account's quota, and read nothing else of ours | Answers nothing with a hosted model; search still works |
| Seed password | `SEED_PASSWORD`, or `--password` | `python -m scripts.seed`, once | Sign in as any of the nine AcmeCorp people | Makes one up and prints it once |
| Demo secrets | Streamlit Cloud's secrets, copied into `APP_` settings | The demo page | As above, for the demo's own key and model key | Makes up a signing key for the process; no model key means passages without answers |

Users' passwords aren't secrets the service holds: they are stored as bcrypt hashes and checked, never kept or logged (SR-7).

## Where they live

In the environment, or in `.env`, which is ignored by git, and on Streamlit Cloud in the app's secrets, which are ignored too (`.streamlit/secrets.toml`). Never in the code, the tests, the documents or the evaluation results. `.env.example` lists every setting with every secret left empty.

## What keeps them in

**At startup.**
- The signing key has no default and must be at least 32 characters, so the service can't start with a known one.
- In production it must also have at least 16 different characters. That stops a word repeated to reach the length, not a guessable phrase: "my-production-secret-key-2026!" gets through, and a test says so. The key should come from `secrets.token_urlsafe(48)`.
- A model key is never sent over plain HTTP to another machine: with `APP_LLM_API_KEY` set, `APP_LLM_BASE_URL` must be `https`, unless it is this machine, where Ollama runs.
- A setting that fails to load is reported by name, never with the value given, so a mistyped secret doesn't end up in a startup log.

**While it runs.**
- Settings hold secrets as `SecretStr`, which prints as asterisks.
- The model key goes in one place, the `Authorization` header of each call to the provider. It isn't in any log line, error or traceback, even with the HTTP libraries logging at DEBUG.
  - This holds whatever the call does: answers, gets 401, 429 or 500, times out, or can't connect.
  - It holds even when the provider's reply, or the library's error, quotes the key back. Error bodies are never read into a message, and a library's error never becomes ours, only its type.
- The signing key never leaves the process: not in a token, which carries only who and until when, nor in a response or a log.

**Between processes.** `python -m scripts.run` starts the API and the UI. The UI only needs the API's address, so it is started without any `APP_` setting or `SEED_PASSWORD` in its environment. It still runs on the same machine as the `.env` file, so this keeps the secrets out of a process that doesn't need them; it isn't a wall between the two.

## The scan

```
python -m scripts.scan_secrets
```

looks through every tracked file and every commit ever made for two things:

- **Something shaped like a secret.** A Groq, OpenAI-style, AWS, GitHub or Hugging Face key, a private key, a signed token, or a value given to `APP_JWT_SECRET_KEY`, `APP_LLM_API_KEY` or `SEED_PASSWORD`.
- **The secrets in the local `.env`, word for word.** A signing key is random text with no shape, so this is the only way to find one, and the most important check here.

It prints where and what kind, never the text it matched, and exits 1 if it finds anything. The test suite runs it on this repository every time. The tests also show it working on a repository made for the purpose: a key committed and deleted in the next commit is still found, in the history.

Text inside a compressed Word file isn't seen. Nor is a secret pasted somewhere other than git: an issue, a chat, a screenshot.

**If it finds one, removing the line doesn't help.** The secret is in the history, and anyone with a clone has it. Replace it, as below, then clean the history if the repository was ever shared.

## Replacing one

| Secret | How | What happens |
|---|---|---|
| Model key | Revoke it at the provider, make a new one, set `APP_LLM_API_KEY`, restart | Nothing visible. Questions asked while it is revoked get the usual 503, and search keeps working |
| Signing key | Generate a new one, set `APP_JWT_SECRET_KEY`, restart | Everyone is signed out at once: every token in use stops working. Every audit seal stops matching, because the seals' key comes from the signing key ([decision record 0013](adr/0013-seal-each-audit-record.md)). Run `python -m scripts.audit check` with the old key first, and keep its output |
| Seed password | Change each account's password | There is no route for this yet: it means seeding a fresh database |

There is no rotation without a restart, and no way to accept an old key and a new one for a while. Both are out of scope here, as the threat model says.
