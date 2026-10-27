# Deployment

How to run the system on a server, from an empty machine to a running service behind HTTPS that is backed up, watched and can be upgraded: what it needs, each step, and what is still missing before a real company could use it. Every step here was run: the backup, the restore and the HTTPS proxy are Docker tests, run in [CI](ci.md) on Linux whenever anything they touch changes.

## What this deploys

**The stack, for the fictional company.** The API, the UI and their data, with HTTPS in front, on one Linux machine with Docker. The people who can sign in are AcmeCorp's nine, and the documents are its seventeen, loaded by the seed when the stack first starts.

**Not yet a real company's.** Three things a real deployment needs aren't built, by design ([requirements](requirements.md#assumptions)):

- **Its people.** Users, departments and roles come only from the seed. A real company's come from its identity provider (SSO), and nothing here talks to one.
- **Its documents, with their permissions.** The admin routes upload documents one at a time with the labels someone types ([API](api.md)). A real company's should come from the systems that hold them, with their access rules, synced.
- **Production mode.** `APP_ENVIRONMENT=production` refuses to load the fictional company, so `setup` fails and the API never starts ([Docker](docker.md#running-them-together)). With nothing else to load, the stack runs in `local` mode, which also serves the interactive API docs and the list of routes ([configuration](configuration.md#environments)).

So this page deploys the system the way it can be shown: properly, on a server, to the people who are to try it. The [last section](#before-a-real-company-uses-it) lists what comes before anything real goes in.

## What it needs

| | Needs | Measured |
|---|---|---|
| **The machine** | Linux, 2 CPUs | Built and tested on GitHub's `ubuntu-24.04` machines, 4 CPUs; developed on Windows with Docker Desktop |
| **Docker** | Docker Engine 25 or later, with Compose v2 | 25 is the first with health checks that check quickly while a container starts |
| **Memory** | 1 GB for the stack, 2 GB to be comfortable | The API 400 MB, the UI 50 MB, the proxy 15 MB; the API's the same after 30 searches |
| **Disk** | 8 GB free to build | The images are 3.3 GB (the API's 2.4 GB, PyTorch and the model in it), and the build downloads more; the data is 350 KB for the 17 documents |
| **Network** | Ports 80 and 443 open to the people using it; for a real certificate, a name that points at the machine | The build downloads PyTorch and the model; a running stack fetches nothing |
| **Python** | 3.11, for the two scripts run on the machine itself | `make_secrets` and `monitor` use only Python's standard library, so nothing is installed; a test keeps it so |

## The first time

**1. The code.** On the machine, in the folder the service will live in:

```
git clone https://github.com/<owner>/permission-aware-enterprise-knowledge-system.git knowledge
cd knowledge
```

**2. The secrets.** The signing key, the fictional company's password and the model key, as files in `secrets/`, then given to the user the containers run as, who can't read a file only its owner may ([configuration](configuration.md#secrets-as-files)):

```
python3 -m scripts.make_secrets
sudo chown 10001 secrets/*
```

Keep `secrets/` apart from everything else: the signing key signs every token and, through a key derived from it, every audit record. A copy goes with the backups, kept separately ([below](#backups)).

**3. The settings.** Compose reads `.env`; only a few of its settings reach the containers ([Docker](docker.md#running-them-together)):

```
SITE_ADDRESS=knowledge.example.com
APP_LLM_PROVIDER=none
```

`SITE_ADDRESS` is the name people use: with a public name that points at this machine, the proxy gets a certificate from Let's Encrypt on its own and renews it ([below](#https)). `APP_LLM_PROVIDER=none` is search without answers. With a hosted model, every question sends the asker's passages to the provider, which is a decision for whoever owns the documents ([security](security.md#running-it-for-real)); the key goes in `secrets/app_llm_api_key`, never in `.env`.

**4. Start it.** The first start builds both images, seeds the company and builds the index; later ones find all three:

```
docker compose -f compose.yaml -f compose.tls.yaml up -d --build --wait
```

`--wait` returns once the API and the UI pass their health checks ([Docker](docker.md#health-checks)). Building both images from nothing took 8 minutes on the development laptop; on GitHub's machines the build and the 45 Docker tests took 4. Once built, the first start, which seeds and indexes, took 30 to 40 seconds, and later ones about 20, most of it the API loading the model.

**5. Check it.**

```
docker compose -f compose.yaml -f compose.tls.yaml ps
curl https://knowledge.example.com/api/health
docker compose logs --no-log-prefix --since 15m api | python3 -m scripts.monitor
```

`ps` shows `setup` exited and the others `healthy`; `/api/health` answers `{"status": "ok"}`; the monitor says nothing needs anyone ([monitoring](monitoring.md)). Then sign in at `https://knowledge.example.com` as `david@acmecorp.example`, with the password in `secrets/seed_password`.

## HTTPS

[compose.tls.yaml](../compose.tls.yaml) puts a proxy, Caddy, in front, configured by [deploy/Caddyfile](../deploy/Caddyfile):

| Path | Goes to |
|---|---|
| `/api/...` | the API, without the `/api`: `/api/health` is the service's `/health` |
| everything else | the UI, its websocket included |

- **It is the only thing reachable from other machines.** The API and the UI are published on `127.0.0.1` only, as without it, so nothing reaches them except through the proxy or from the machine itself.
- **Plain HTTP is redirected** to HTTPS, and browsers are told to use HTTPS only for a year (`Strict-Transport-Security`).
- **No access log.** Caddy's would hold every URL; the service already logs each request without its query string ([logging](logging.md)).
- **No admin endpoint,** so nothing can reconfigure the proxy while it runs.
- **Hardened like the rest:** read-only, every Linux capability dropped but the one that listens on ports 80 and 443, no privileges gained. The image is pinned to a version and a digest.

**The certificate.** With a public name, Caddy gets one from Let's Encrypt, which needs ports 80 and 443 reachable from the internet, and renews it on its own. With `localhost`, an IP address or a name only a private network knows, it makes one from an authority of its own, which browsers don't trust until told to. To trust it on a machine:

```
docker compose -f compose.yaml -f compose.tls.yaml cp proxy:/data/caddy/pki/authorities/local/root.crt caddy-root.crt
```

and add `caddy-root.crt` to that machine's trusted certificates. The tests do exactly this, and trust nothing else, so a certificate for the wrong name fails them ([tests/docker/test_tls.py](../tests/docker/test_tls.py)). The Let's Encrypt path isn't tested here: it needs a public name.

`HTTPS_PORT` and `HTTP_PORT` in `.env` move the ports, for a machine where something else has them.

## Day to day

From cron, each a single command ([monitoring](monitoring.md#running-it)):

| How often | What | Exit code that needs someone |
|---|---|---|
| Every 5 minutes | `docker compose logs --no-log-prefix --since 15m api \| python3 -m scripts.monitor` | 2 a page, 1 a ticket, 3 the API writing nothing |
| Once a day | `docker compose exec -T api python -m scripts.audit check` | 2: a record doesn't match its seal |
| Once a day | `docker compose exec -T api python -m scripts.index status` | 2: the index is behind the documents |
| Once a day | a backup ([below](#backups)) | any but 0 |

Every log line is JSON on Docker's own log, rotated at five files of 10 MB ([logging](logging.md)); `docker compose logs` reads it.

## Upgrading

```
git pull
python3 -m scripts.scan_secrets
docker compose -f compose.yaml -f compose.tls.yaml up -d --build --wait
```

The images are rebuilt, and `setup` runs again before the API: the seed adds nothing that is already there, and the index is rebuilt only if it is out of date. The data stays on its volume. Each start of `setup` adds one audit record, a re-chunk of the documents by the knowledge admin that changes none of them: the seed checks the chunks against the settings every time, and checking them is an admin action, so it is recorded.

**Going back** is the same command on the earlier commit: `git checkout <commit>`, then `up -d --build --wait`. There are no database migrations: at startup the service only creates the tables that are missing. So going back works as long as no version in between changed a table that already existed; one that did would need a migration, which isn't built. A bad index goes back on its own: `docker compose exec api python -m scripts.index rollback` ([vector index](../README.md#vector-index)).

Take a backup first.

## Backups

Everything on the data volume, the database and the index, in one file, with the API stopped so the database isn't copied half written:

```
mkdir -p backups
docker compose stop api
docker run --rm --user 0 --network none -v knowledge_data:/data:ro -v "$PWD/backups:/backup" knowledge-api tar -czf /backup/knowledge-data.tgz -C /data .
docker compose start api
```

On the development laptop the API was down for 14 seconds: 2 to stop, under 1 to copy this company's 108 KB, and 11 to start and load the model again. The UI says the service can't be reached meanwhile. The copy grows with the documents, and the rest doesn't. The copy runs as root in a container of the API's own image with no network, so every file keeps its owner, and the volume is mounted read-only.

**The volume holds every document,** restricted ones included, and the backup is all of them in one file: it needs the same protection as the machine ([security](security.md#running-it-for-real)), and it should be kept somewhere else as well as here.

**Back up `secrets/` too, separately.** Without the signing key the audit records can't be checked against their seals, and everyone has to sign in again; with it, anyone holding the backup can sign tokens. Keep the two apart.

**Restoring,** into a stack that has never started, or after `down -v`:

```
docker compose -f compose.yaml -f compose.tls.yaml up --no-start
docker run --rm --user 0 --network none -v knowledge_data:/data -v "$PWD/backups:/backup:ro" knowledge-api tar -xzf /backup/knowledge-data.tgz --numeric-owner -C /data
docker compose -f compose.yaml -f compose.tls.yaml up -d --wait
docker compose exec -T api python -m scripts.audit check
```

`up --no-start` makes the volume without starting anything, so `setup` doesn't seed an empty one first. With the same `secrets/`, the check says `intact`, and its highest ID is the backup's plus one: `setup`'s re-chunk on starting.

A Docker test does all of this: it backs up a running stack, restores into a new one, and checks that the trail is intact against the same key, that each person sees the same documents, and that nothing was seeded or indexed again ([tests/docker/test_stack.py](../tests/docker/test_stack.py)).

## Replacing a secret

[Secrets](secrets.md#replacing-one) has each one. In short: a new model key is a new file and a restart; a new signing key signs everyone out, and the audit records sealed with the old one stop matching it, so the old key is kept with the backups from before.

## Taking it down

`docker compose -f compose.yaml -f compose.tls.yaml down` stops everything and keeps the data and the certificates. `down -v` deletes them too: every document, the trail and the index, with no way back but a backup.

## Before people use it

- [ ] HTTPS in front, and nothing else published beyond the machine: `docker compose ps` shows the API and the UI on `127.0.0.1` only.
- [ ] `secrets/` readable by the service's user and nobody else on the machine, and not in git (`python3 -m scripts.scan_secrets`).
- [ ] A decision, by whoever owns the documents, on whether a hosted model may see them.
- [ ] The monitor, the trail check, the index check and a backup in cron, and someone who gets the pages.
- [ ] A restore tried once, on another machine.
- [ ] The people who use it told it holds the fictional company's documents only.

## Before a real company uses it

What this design leaves out and a real deployment can't ([security](security.md#what-it-doesnt-cover), [threat model](threat-model.md#residual-risks)):

- **People from the identity provider**, and their roles from its groups, instead of the seed.
- **Documents from their own systems,** with their access rules synced, instead of typed labels (T17).
- **`APP_ENVIRONMENT=production`,** once there is something other than the fictional company to load.
- **More than one API process** needs the rate limits and the login throttle in a shared store first, and a database server instead of SQLite ([decision record 0007](adr/0007-faiss-and-sqlite-locally.md)).
- **The log and the audit trail kept off the machine,** where its administrators can't change them ([audit](audit.md)).
- **Encryption at rest,** of the volume and of the backups.
- **Deploys that wait for CI,** and images scanned for known vulnerabilities ([CI](ci.md#what-it-holds-back-and-what-it-doesnt)).
