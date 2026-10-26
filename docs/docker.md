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
docker compose up --build
```

Then the UI is at http://127.0.0.1:8501 and the API at http://127.0.0.1:8000. `API_PORT` and `UI_PORT` in `.env` move them. `docker compose down` stops everything and keeps the data; `docker compose down -v` deletes the volume too.

[compose.yaml](../compose.yaml) starts three services:

| Service | What it does | Network | Data |
|---|---|---|---|
| `setup` | Seeds the fictional company, then builds the index if there is none or it is out of date, and exits | none | the volume |
| `api` | The service, once `setup` has finished without an error | the stack's own | the volume |
| `ui` | The UI, pointed at `http://api:8000` | the stack's own | none |

**The first start** seeds the 17 documents and builds the index, which takes about 40 seconds once the images are built. **Later starts** find both on the volume: the seed adds nothing, the index is up to date and isn't rebuilt, and the stack is up in about 5 seconds. Measured once each on the development laptop.

**Settings.** Compose reads `.env` itself and passes on only the settings `compose.yaml` names, so the rest of `.env`, the database's path for example, never reaches a container:

| Setting | Given to | If `.env` doesn't set it |
|---|---|---|
| `APP_JWT_SECRET_KEY` | `setup`, `api` | Compose refuses to start, and says which setting is missing |
| `APP_ENVIRONMENT` | `setup`, `api` | `local` |
| `SEED_PASSWORD` | `setup` | a password is made up and printed once, in `docker compose logs setup`, where anyone who can read this machine's container logs can see it; setting one avoids that |
| `APP_LLM_PROVIDER` | `api` | `none`: no answers, search only |
| `APP_LLM_API_KEY` | `api` | no key |
| `APP_LLM_MAX_OUTPUT_TOKENS` | `api` | `600`; set `1000` with Groq |

The UI is given none of them. A setting that isn't set reaches a container as an empty one, which used to matter twice: an empty `SEED_PASSWORD` was taken as the password, giving every account one nobody could type, and an empty `APP_LLM_API_KEY` counted as a key. Both now count as not set.

**In production** (`APP_ENVIRONMENT=production`) the seed refuses to load the fictional company, so `setup` fails and the API is never started. That is on purpose: this file runs the demo company. A real deployment loads its own documents.

**Every container is hardened** beyond what the images do:

- the image is read-only while it runs, and only the volume and `/tmp` can be written;
- every Linux capability is dropped, and no process can gain privileges (`no-new-privileges`);
- both ports are published on `127.0.0.1` only;
- `setup` has no network at all.

What a container is given with `environment:` can be read by anyone who can run `docker inspect` on this machine, the signing key included. Moving the secrets into files comes with the environment configuration step.

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

Inside the container it listens on every interface, because otherwise it couldn't be reached at all. Which of the host's addresses it is published on is decided by `-p`: `127.0.0.1:8000:8000` keeps it on this machine, and a bare `8000:8000` would put it on the network.

The signing key must be the same for seeding, indexing and serving: the audit seals are made with a key derived from it ([secrets](secrets.md)). Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Passing it with `-e` puts it in the container's settings, where anyone who can run `docker inspect` on this machine reads it; `--env-file` with a file only you can read is better.

The UI finds the API at `UI_API_URL`, `http://api:8000` in its image: the API's name on a network the two share.

```
docker network create knowledge
docker network connect --alias api knowledge knowledge-api
docker run -d --name knowledge-ui --network knowledge -p 127.0.0.1:8501:8501 knowledge-ui
```

## Tests

- [tests/unit/test_dockerfile.py](../tests/unit/test_dockerfile.py) reads the Dockerfile and `.dockerignore`, and runs with every other test: the pinned base, the versions, no secret set, the user, the volume, the UI's image holding none of the service, and what the build may see.
- [tests/unit/test_compose.py](../tests/unit/test_compose.py) reads `compose.yaml`: nothing published beyond this machine, every container hardened, no value written into the file, only the settings above passed and only where they're needed, the UI given none, and the defaults the service's own.
- [tests/docker/test_images.py](../tests/docker/test_images.py) builds and runs both images, and [tests/docker/test_stack.py](../tests/docker/test_stack.py) starts the stack. Both need Docker, so they are left out of a normal run:

  ```
  pytest -m docker
  ```

  The images: it seeds and indexes a volume with no network, starts the API on it, signs in as David and searches, checks the log holds no question, starts the UI against the API, and looks inside both images: who they run as, that the service can't change its code, the CPU build of PyTorch, the model loaded with no network, the UI's missing service, and no local secret anywhere in them.

  The stack: each person sees their own documents through it, the UI reaches the API by name, the UI's container holds no setting of the service's, nothing is published beyond this machine, every container runs hardened and only the data can be written, a restart keeps the data and builds nothing again, production refuses the demo data and never starts the API, and with no signing key Compose won't start. Each run gets its own project name, ports and env file, so it never reads the local `.env`, and it is taken down with its volume however it ends.

## Not done here

- **No health check.** So `docker compose up --wait` returns when the containers are running, not when the API has loaded its model and index, and the first requests can find nothing listening. The tests wait for `/health` themselves; health checks come in their own step.
- **Image signing and scanning for known vulnerabilities** are not set up. The base is pinned, so it also doesn't get security fixes until the pin is moved.
- **One process.** The rate limits and the login throttle are in memory, so one API container is the supported shape ([API](api.md#how-often)).
