# Configuration

Every setting the service reads, where it can come from, what changes between environments, and how secrets are given to it. The settings are defined in [app/core/config.py](../app/core/config.py); a test compares this page with them, so a setting can't be added, removed or given a new default without this page saying so.

## Where settings come from

The first of these that has a value wins:

1. **An environment variable,** named `APP_` and the setting, such as `APP_LOG_LEVEL`.
2. **The `.env` file** in the project root. [.env.example](../.env.example) lists every setting; copy it to `.env`. `.env` is ignored by git.
3. **A file in the secrets folder,** when `APP_SECRETS_DIR` names one: one file per setting, named after it in lower case, such as `app_jwt_secret_key`, holding the value and nothing else. A newline at the end is ignored.

Then the setting's default, if it has one. The signing key has none, so the service doesn't start without one.

A value that is wrong stops the service at startup with the setting's name and what is wrong with it, never the value given: a mistyped key doesn't end up in a log.

`APP_SECRETS_DIR` itself isn't a setting: it says where settings come from. If it names something that isn't a folder, or the folder has a folder in it, the service refuses to start and says which. That second one is how a missing file shows up under Docker Desktop, which mounts an empty folder in place of a file it can't find; Docker Engine on Linux refuses to start the container instead ([Docker](docker.md#running-them-together)).

An empty value counts as no value for the model key and the seed password, because Docker Compose passes a variable that isn't set as an empty one.

## Secrets as files

A container's environment can be read by anyone who can run `docker inspect` on the machine. So under Docker Compose the secrets are files instead, mounted in `/run/secrets`, and every container's settings hold none of them; a test inspects each one. The other settings are not secret, and stay settings.

```
python -m scripts.make_secrets
docker compose up --build
```

`make_secrets` makes, in `secrets/`:

| File | Holds | Given to |
|---|---|---|
| `app_jwt_secret_key` | a new signing key, from `secrets.token_urlsafe(48)` | `setup`, `api` |
| `seed_password` | a new password for the fictional company's people | `setup` |
| `app_llm_api_key` | nothing: put a provider's key in it to use a hosted model | `api` |

It never replaces a file that is there: a new signing key signs everyone out and makes every audit seal stop matching ([secrets](secrets.md)). It prints what it made and kept, never what is in them; the password is read from its file. On Linux and macOS the files are readable by their owner only.

**A stack started before secrets were files** has its audit seals made with the key in `.env`. `python -m scripts.make_secrets --from-env-file .env` takes the values `.env` has instead of making new ones, so the same key carries on.

**On a Linux host,** Docker mounts the files as they are on the host, and the service runs as uid 10001, which by the file's permissions can't read a file only its owner may read. Give the files to that user, `sudo chown 10001 secrets/*`, or make them readable some other way that suits the machine. Not tested here: this was tested with Docker Desktop on Windows, which mounts the files readable as they are.

`SECRETS_DIR` moves the folder, for example out of the project. The service also takes a secrets folder outside Docker: point `APP_SECRETS_DIR` at it.

## Environments

`APP_ENVIRONMENT` is `local`, `test` or `production`.

| | `local` and `test` | `production` |
|---|---|---|
| Interactive API docs and the list of routes | at `/docs` and `/openapi.json` | not served |
| A signing key with fewer than 16 different characters | taken | refused: it was typed, not generated |
| `python -m scripts.seed` | loads the fictional company | refuses |
| `docker compose up` | seeds and starts | `setup` refuses the demo data, so the API never starts |

Nothing else differs. The tests run with the settings in `tests/conftest.py`: an in-memory database, and limits lifted so a test can ask as often as it needs ([API](api.md#how-often)).

## Every setting

| Setting | Default | Secret | What it is for |
|---|---|---|---|
| `APP_ENVIRONMENT` | `local` |  | `production` hides the interactive docs, refuses a typed signing key, and stops the seed script |
| `APP_LOG_LEVEL` | `INFO` |  | How much is logged. No level logs document text or questions |
| `APP_DATABASE_URL` | `sqlite:///./data/app.db` |  | The database. It holds every document, so it needs their protection |
| `APP_JWT_SECRET_KEY` | required | yes | Signs tokens and, through a derived key, the audit seals. At least 32 characters |
| `APP_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` |  | How long a token works |
| `APP_LOGIN_MAX_FAILURES` | `5` |  | Failed logins for one email in the window before the password isn't checked |
| `APP_LOGIN_FAILURE_WINDOW_SECONDS` | `900` |  | That window |
| `APP_ASK_LIMIT_PER_MINUTE` | `10` |  | Questions per person in any minute |
| `APP_ASK_LIMIT_PER_DAY` | `200` |  | Questions per person in any 24 hours |
| `APP_SEARCH_LIMIT_PER_MINUTE` | `30` |  | Searches per person in any minute |
| `APP_MAX_UPLOAD_BYTES` | `10485760` |  | The largest file accepted |
| `APP_CHUNK_MAX_CHARACTERS` | `800` |  | How long a chunk may be. Changing it means re-chunking everything |
| `APP_CHUNK_OVERLAP_CHARACTERS` | `120` |  | How much neighbouring chunks share |
| `APP_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` |  | The local embedding model. Changing it means embedding everything again |
| `APP_EMBEDDING_BATCH_SIZE` | `32` |  | Chunks embedded at once |
| `APP_EMBEDDING_OFFLINE` | `false` |  | Load the models from the local cache only |
| `APP_INDEX_DIRECTORY` | `data/index` |  | Where the index is kept. It holds every document's vectors |
| `APP_INDEX_KEEP_VERSIONS` | `3` |  | Index versions kept for rollback |
| `APP_MAX_QUESTION_CHARACTERS` | `1000` |  | The longest question |
| `APP_RETRIEVAL_TOP_K` | `5` |  | Chunks a question retrieves |
| `APP_RETRIEVAL_MIN_SCORE` | `0.35` |  | Below this best score: no information. Measured for the default model |
| `APP_RERANKER_MODEL` | not set |  | A cross-encoder to reorder chunks; off when not set |
| `APP_RERANK_CANDIDATES` | `20` |  | Chunks the reranker chooses from |
| `APP_CONTEXT_MAX_CHARACTERS` | `6000` |  | The most document text sent to the model for one question |
| `APP_LLM_PROVIDER` | `none` |  | Where the prompt goes: `none`, `ollama` or `groq` |
| `APP_LLM_MODEL` | not set |  | Overrides the provider's model |
| `APP_LLM_BASE_URL` | not set |  | Overrides the provider's address. With a key, https unless it's this machine |
| `APP_LLM_CONTEXT_TOKENS` | not set |  | Overrides the model's context size |
| `APP_LLM_API_KEY` | not set | yes | The provider's key, sent in a header on every call |
| `APP_LLM_MAX_OUTPUT_TOKENS` | `600` |  | The longest answer |
| `APP_LLM_TIMEOUT_SECONDS` | `30.0` |  | The longest one model call may take |
| `APP_LLM_MAX_RETRIES` | `2` |  | Retries of a call the provider may recover from |

Outside the service's settings:

| Variable | Read by | What it is for |
|---|---|---|
| `APP_SECRETS_DIR` | the service, the seed | The folder of secret files, above |
| `SEED_PASSWORD` | `python -m scripts.seed` | The fictional company's password; or `seed_password` in the secrets folder; or one is made up and printed once |
| `UI_API_URL` | the UI | Where the API is: `http://127.0.0.1:8000` by default, `http://api:8000` in its image |
| `SECRETS_DIR`, `API_PORT`, `UI_PORT` | Docker Compose | Where the secret files are, and where the API and the UI are published |

## Not covered

- **Changing a setting means a restart.** Settings are read once, when a process starts.
- **No secret manager.** Files are what Docker, Kubernetes and most secret managers can all hand a process, which is why the service reads them; fetching from Vault or a cloud's secret store directly is not built.
- **Rotating the signing key** still means everyone signs in again and the existing audit seals stop matching ([secrets](secrets.md)).
