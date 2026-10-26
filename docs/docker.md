# Docker

Two images from one [Dockerfile](../Dockerfile): the API, and the UI, and [compose.yaml](../compose.yaml) to run them together. This page is the images, how to run them with Compose, and how to run each by hand.

## The two images

```
docker build --target api -t knowledge-api .
docker build --target ui  -t knowledge-ui  .
```

**The API image** holds the service, the embedding model and the fictional company's files, so it can be seeded. The model is downloaded once, when the image is built, and a container loads it from the image with nothing fetched (`APP_EMBEDDING_OFFLINE=true`, `HF_HUB_OFFLINE=1`). A test loads it in a container with no network at all. PyTorch is the CPU-only build: the default Linux one brings several gigabytes of GPU libraries the service never uses.

**The UI image** holds the UI's own modules and three libraries: Streamlit, httpx and pydantic. It has **no copy of the service**: no code, no database, no index, no model, no PyTorch. The UI only talks to the API, and in its image there is nothing else it could talk to. The demo page, `ui/demo.py` and `ui/streamlit_app.py`, runs the service in its own process, so it is not part of this UI and stays out. A test checks the image holds every UI module that doesn't import the service and none that does.

`docker build .` with no target gives the API.

| | API | UI |
|---|---|---|
| Size, as `docker images` reports it | 2.38 GB | 864 MB |
| First build, nothing cached | 214 s | 64 s |

Measured on the development laptop (8 CPUs, 8 GB given to Docker Desktop 29.9.2), once each. Most of the API image is PyTorch, and most of its first build is downloading it; a rebuild after a code change reuses those layers. Most of the UI image is what Streamlit brings with it. Building both and running every Docker test from nothing took 8 min 20 s; with the cache warm, the tests take about 80 s.

## What is in them, and what isn't

- **No secrets.** The signing key and the model key are given when a container starts. Neither is set anywhere in the Dockerfile, and a test searches both images, their settings and their build history for the values in the local `.env`.
- **Only settings decided on.** Anyone with an image can read its settings, so a secret put there is given away whatever it is called. Every variable the images may hold is listed, with why, in [tests/imagesettings.py](../tests/imagesettings.py), the base image's own included; both the Dockerfile and the built images are checked against it. A first version looked for names that sound secret instead, and a deliberate break called `APP_SEED` got past it.
- **Nothing local.** [.dockerignore](../.dockerignore) leaves everything out of the build except what it names: the code, the UI, the scripts, the company's files and the requirements. A list of what to leave out would have to keep up with every file that shouldn't reach an image (`.env`, the database, the index); a list of what to let in can't miss one.
- **Not root.** Both run as `app`, uid 10001. The code belongs to root, so the service can't change it; a test tries.
- **Pinned.** The base is Python 3.11.16 on Debian 12, by tag and by digest, so a rebuild gets the same base. PyTorch is the version the tests run with, and so are the UI's libraries; tests compare them.
- **State on a volume.** The database and the index are at `/data` (`APP_DATABASE_URL`, `APP_INDEX_DIRECTORY`), a volume, kept apart from the code. They hold every document, restricted ones included, so the volume needs the same protection as the database (T19).

The tests run with 3.11.9 on the development machine, and the image has 3.11.16: the same minor version, with later security fixes.

## Running them together

```
python -m scripts.make_secrets
docker compose up --build
```

The first line makes the secret files, once ([configuration](configuration.md#secrets-as-files)).

Then the UI is at http://127.0.0.1:8501 and the API at http://127.0.0.1:8000. `API_PORT` and `UI_PORT` in `.env` move them. `docker compose down` stops everything and keeps the data; `docker compose down -v` deletes the volume too.

[compose.yaml](../compose.yaml) starts three services:

| Service | What it does | Network | Data |
|---|---|---|---|
| `setup` | Seeds the fictional company, then builds the index if there is none or it is out of date, and exits | none | the volume |
| `api` | The service, once `setup` has finished without an error | the stack's own | the volume |
| `ui` | The UI, pointed at `http://api:8000`, once the API is healthy | the stack's own | none |

**The first start** seeds the 17 documents and builds the index, which takes about 40 seconds once the images are built. **Later starts** find both on the volume: the seed adds nothing, the index is up to date and isn't rebuilt, and the stack is up in about 5 seconds. Measured once each on the development laptop.

**Secrets are files,** mounted in `/run/secrets` and read through `APP_SECRETS_DIR`, never settings: a container's settings can be read by anyone who can run `docker inspect`, and a test inspects every container for them.

| Secret file | Given to |
|---|---|
| `app_jwt_secret_key` | `setup`, `api` |
| `seed_password` | `setup` |
| `app_llm_api_key` | `api`; empty, no hosted model |

A missing file doesn't stop Compose: Docker mounts an empty folder in its place, and leaves one on the host. The service refuses that and names it, so `setup` fails and the API never starts; `make_secrets` says the same if it finds one.

**Settings.** Compose reads `.env` itself and passes on only the settings `compose.yaml` names, so the rest of `.env`, the database's path for example, never reaches a container:

| Setting | Given to | If `.env` doesn't set it |
|---|---|---|
| `APP_ENVIRONMENT` | `setup`, `api` | `local` |
| `APP_LLM_PROVIDER` | `api` | `none`: no answers, search only |
| `APP_LLM_MAX_OUTPUT_TOKENS` | `api` | `600`; set `1000` with Groq |

The UI is given none of them, and no secret file. A setting that isn't set reaches a container as an empty one, which mattered twice: an empty `SEED_PASSWORD` was taken as the password, giving every account one nobody could type, and an empty `APP_LLM_API_KEY` counted as a key. Both now count as not set.

**In production** (`APP_ENVIRONMENT=production`) the seed refuses to load the fictional company, so `setup` fails and the API is never started. That is on purpose: this file runs the demo company. A real deployment loads its own documents.

**Every container is hardened** beyond what the images do:

- the image is read-only while it runs, and only the volume and `/tmp` can be written;
- every Linux capability is dropped, and no process can gain privileges (`no-new-privileges`);
- both ports are published on `127.0.0.1` only;
- `setup` has no network at all.

## Health checks

Each image says how to tell it is ready, so `docker run` gets the same check as Compose:

| Image | Asks | Starting | Then |
|---|---|---|---|
| API | `GET /health` on port 8000 | every 2 s, for up to 120 s | every 30 s |
| UI | Streamlit's `/_stcore/health` on port 8501 | every 2 s, for up to 60 s | every 30 s |

A check passes on a 200 within 4 seconds and fails on anything else: another status, no answer, or nothing listening. Three failures in a row after the start make the container `unhealthy`; failures while it starts don't count. The images have no curl, so the check is a line of Python's own `urllib`.

**Healthy means ready.** The API's port opens only once the embedding model and the index are loaded, so the first `/health` that answers comes from a service that can search. `/health` also asks the database, and gives 503 if it doesn't answer ([API](api.md#health)). On the development laptop the API was healthy about 10 seconds after its container started, after three failed checks, on a first start and on a restart alike: two minutes leaves room for a much slower machine.

**What uses it:**

- **Compose starts the UI only once the API is healthy,** so the UI never comes up talking to an API that is still loading.
- **`docker compose up --wait`** returns once both are healthy, and fails if either never is. Before there were health checks it returned once the containers were running, and the first requests found nothing listening.
- **`docker compose ps`** and `docker ps` show `healthy` or `unhealthy`; `docker inspect knowledge-api-1` has the last five checks and what each printed.

`setup` runs the API's image, so it would inherit the API's check, which a job that never serves can't pass; Compose switches it off there.

**The UI's check is its own,** not the API's. If the API is down, a new UI wouldn't fix it, and the UI tells whoever is using it that the service can't be reached.

**One check, not two.** Kubernetes separates "alive" (restart it if not) from "ready" (send it no requests if not), because a liveness check that asks a shared database restarts every replica when the database is down. Here the database is a file on the container's own volume, so the database not answering is this container's own problem, and one check serves both.

**It doesn't fill the log.** A passing check is logged at DEBUG, below the default level; a failing one is logged like any other request.

## Running them by hand

The service refuses to start without an index, so a new volume is seeded and indexed first. These run with no network, because they need none:

```
docker volume create knowledge-data
docker run --rm --network none -v knowledge-data:/data \
  -e APP_JWT_SECRET_KEY=... -e SEED_PASSWORD=... knowledge-api python -m scripts.seed
docker run --rm --network none -v knowledge-data:/data \
  -e APP_JWT_SECRET_KEY=... knowledge-api python -m scripts.index build
```

Then the API, published on this machine only:

```
docker run -d --name knowledge-api -p 127.0.0.1:8000:8000 -v knowledge-data:/data \
  -e APP_JWT_SECRET_KEY=... knowledge-api
```

`docker ps` shows `(health: starting)` until it has loaded, then `(healthy)`; `docker inspect --format "{{.State.Health.Status}}" knowledge-api` gives the word alone, for a script.

Inside the container it listens on every interface, because otherwise it couldn't be reached at all. Which of the host's addresses it is published on is decided by `-p`: `127.0.0.1:8000:8000` keeps it on this machine, and a bare `8000:8000` would put it on the network.

The signing key must be the same for seeding, indexing and serving: the audit seals are made with a key derived from it ([secrets](secrets.md)). Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Passing it with `-e` puts it in the container's settings, where anyone who can run `docker inspect` on this machine reads it; `--env-file` with a file only you can read is better.

The UI finds the API at `UI_API_URL`, `http://api:8000` in its image: the API's name on a network the two share.

```
docker network create knowledge
docker network connect --alias api knowledge knowledge-api
docker run -d --name knowledge-ui --network knowledge -p 127.0.0.1:8501:8501 knowledge-ui
```

## Tests

- [tests/unit/test_dockerfile.py](../tests/unit/test_dockerfile.py) reads the Dockerfile and `.dockerignore`, and runs with every other test: the pinned base, the versions, no secret set, the user, the volume, the UI's image holding none of the service, and what the build may see. And the health checks: each asks its own image's route on its own port, gives up before Docker would kill it, and has a start period; and each image's own probe, run against a local server, passes on a 200 and fails on a 503, a 500, a 404 and nothing listening.
- [tests/unit/test_compose.py](../tests/unit/test_compose.py) reads `compose.yaml`: nothing published beyond this machine, every container hardened, no value written into the file, only the settings above passed and only where they're needed, the UI given none, the defaults the service's own, the UI waiting for the API to be healthy, and `setup`'s inherited check switched off.
- [tests/docker/test_images.py](../tests/docker/test_images.py) builds and runs both images, and [tests/docker/test_stack.py](../tests/docker/test_stack.py) starts the stack. Both need Docker, so they are left out of a normal run:

  ```
  pytest -m docker
  ```

  The images: it seeds and indexes a volume with no network, starts the API on it, signs in as David and searches, checks the log holds no question, starts the UI against the API, waiting each time for Docker to call it healthy by the image's own check, and looks inside both images: that each carries its health check and the API's fails with nothing listening, who they run as, that the service can't change its code, the CPU build of PyTorch, the model loaded with no network, the UI's missing service, and no local secret anywhere in them.

  The stack: the UI was started only after the API's first passing check, both are healthy and `setup` has no check, no secret is in any container's settings and each gets only its own secret files, a missing key file stops `setup` naming it, each person sees their own documents through it, the UI reaches the API by name, the UI's container holds no setting of the service's, nothing is published beyond this machine, every container runs hardened and only the data can be written, a restart keeps the data and builds nothing again, production refuses the demo data and never starts the API, and, on Linux in the API's image, the secret files are made readable by their owner only. Each run gets its own project name, ports, env file and secret files, so it never reads the local `.env` or `secrets/`, and it is taken down with its volume however it ends.

## Not done here

- **Nothing restarts an unhealthy container.** Docker and Compose only report it; `restart: unless-stopped` acts when a container stops, not when it fails its check. An orchestrator (Kubernetes, Swarm) acts on health; under Compose, a person reads `docker compose ps`.
- **A busy API can look unhealthy.** `/health` is served by the same worker threads as everything else; if every one is waiting on a model call (up to 30 seconds each), a check can time out. Three must fail in a row, 90 seconds, before Docker says so.
- **Image signing and scanning for known vulnerabilities** are not set up. The base is pinned, so it also doesn't get security fixes until the pin is moved.
- **One process.** The rate limits and the login throttle are in memory, so one API container is the supported shape ([API](api.md#how-often)).
