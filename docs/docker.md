# Docker

Two images from one [Dockerfile](../Dockerfile): the API, and the UI. Running them together comes with Docker Compose; this page is the images themselves, and how to run each by hand.

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
- [tests/docker/test_images.py](../tests/docker/test_images.py) builds and runs both images, and needs Docker, so it is left out of a normal run:

  ```
  pytest -m docker
  ```

  It seeds and indexes a volume with no network, starts the API on it, signs in as David and searches, checks the log holds no question, starts the UI against the API, and looks inside both images: who they run as, that the service can't change its code, the CPU build of PyTorch, the model loaded with no network, the UI's missing service, and no local secret anywhere in them.

## Not done here

- **No health check in the image.** Health checks come in their own step.
- **No Compose.** Running the two together, with the volume and network made for them, is the next step.
- **Image signing and scanning for known vulnerabilities** are not set up. The base is pinned, so it also doesn't get security fixes until the pin is moved.
- **One process.** The rate limits and the login throttle are in memory, so one API container is the supported shape ([API](api.md#how-often)).
