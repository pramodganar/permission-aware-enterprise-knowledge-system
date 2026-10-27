# CI

What runs on GitHub when code is pushed, what it checks, what it can't, and what it does and doesn't hold back. The workflows are in [.github/workflows/](../.github/workflows/); a test reads them ([tests/unit/test_ci.py](../tests/unit/test_ci.py)), so a change that weakens one fails before it is pushed.

## What runs

| Workflow | When | What |
|---|---|---|
| [CI](../.github/workflows/ci.yml) | Every push to `main`, every pull request, and by hand | The README's checks on Linux, `ruff check .`, `ruff format --check .`, `mypy` and `pytest --cov`, then the secret scan over every file and every commit |
| [Docker](../.github/workflows/docker.yml) | A push or pull request that changes anything that goes into the images, and by hand | Builds both images and runs `pytest -m docker`: the images alone, then the Compose stack ([Docker](docker.md#tests)) |

The Docker workflow watches the Dockerfile, `.dockerignore`, `compose.yaml`, the requirements, `app/`, `scripts/`, `ui/`, the company's files and its own tests. A test reads every `COPY` in the Dockerfile and fails if the workflow doesn't watch what it copies, so a new file in an image can't go untested. A change to the docs or the evaluation doesn't run it: it builds PyTorch and the model into an image, which costs most of its time.

Both can be run from the repository's Actions tab with "Run workflow".

## What it can't check

- **The real models.** Tests marked `model` load the embedding model and `llm` a local language model; both are left out here as on a laptop (`pytest -m model` runs them). CI runs with `HF_HUB_OFFLINE=1`, so a test that would fetch a model fails instead of quietly downloading one.
- **Answers from a hosted model.** The evaluation against Groq needs a key, and CI is given no secret ([below](#what-it-may-do)). It is run by hand ([evaluation](evaluation.md)).
- **The local secrets.** The secret scan finds anything shaped like a key in every file and every commit, but `.env` and `secrets/` aren't on GitHub, so their exact values can only be searched for on a machine that has them. Run `python -m scripts.scan_secrets` there before pushing ([secrets](secrets.md)).

## What it may do

Not much, on purpose: a workflow runs the repository's code on GitHub's machines, and the less it may do, the less a bad change to it could.

- **Read the code, nothing else.** `permissions: contents: read` for every job; no job writes to the repository, opens an issue or publishes a package.
- **No secrets.** Nothing in the workflows reads one, and a test fails if one does. The tests make their own keys.
- **Every action pinned to a commit**, with its version beside it. A tag can be moved to other code; a commit can't. `actions/checkout` v7.0.1 and `actions/setup-python` v7.0.0, both released in July 2026. To move one: find the release's commit (`gh api repos/actions/checkout/commits/v7.0.2 --jq .sha`), and change both the hash and the comment.
- **No token left behind.** `persist-credentials: false`, so the code the job runs can't use the checkout's token.
- **Nothing a fork can turn against the repository.** No `pull_request_target` or `workflow_run`, which run a fork's changes with the repository's own token.
- **A time limit on every job,** and a newer push to the same branch cancels the older run.
- **The same versions as everywhere else.** Python 3.11; PyTorch's CPU-only build, the version the images install; everything else from the pinned requirements.

## What it holds back, and what it doesn't

**It reports; it doesn't block.** Changes are pushed straight to `main`, by one person, so CI says whether a push was good after it is there, and a failed run is fixed by the next push. Blocking a merge until CI passes needs a required status check on a protected branch, and GitHub offers protected branches on private repositories only on paid plans. With one, CI here would already be the check to require: NFR-13 asks that a failing security test block the merge, and the security tests are in `pytest --cov`.

**The demo doesn't wait for it.** Streamlit Community Cloud redeploys the demo on every push to `main`, whatever CI says, and it can't be told to wait ([demo](demo.md)). Two ways to make it wait, neither set up:

- Deploy the demo from a branch of its own, and have CI move that branch to `main` only once the checks pass. That job needs `contents: write`, the one permission every job here goes without.
- Deploy somewhere a workflow can start a deploy: a deploy job that `needs` the checks, in a GitHub environment that holds the host's key and, if wanted, waits for someone to approve.

**Nothing is published.** The images are built and tested, then thrown away. GitHub's registry is the obvious place for them, but on this plan private packages get 500 MB of storage, and the API image is 2.4 GB.

## Running the same checks first

Everything CI runs can be run on a laptop, and should be before a push: the commands in the README's [checks](../README.md#checks), the secret scan, and `pytest -m docker` when the images changed. The only difference is the operating system: CI runs on Linux.

**Linux showed one difference before CI ever ran.** Docker on a Linux host mounts the secret files as they are, readable by their owner only, and the service's user isn't their owner ([configuration](configuration.md#secrets-as-files)). The stack tests make their own throwaway secrets readable for that reason; the docs give a real machine's files to the service's user instead.

## Not covered

- **Dependency updates.** Nothing opens a pull request when a pinned version has a fix. Dependabot would, for the requirements and the actions.
- **Known vulnerabilities** in the dependencies and the images aren't scanned ([Docker](docker.md#not-done-here)).
- **Windows and macOS.** CI runs on Linux; the project is developed on Windows, so both are covered, macOS by neither.
