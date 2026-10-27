"""Making the secret files Docker Compose gives the containers (docs/configuration.md).

    python -m scripts.make_secrets [--folder secrets] [--from-env-file .env]

Makes, in the folder, each file that isn't there yet:

    app_jwt_secret_key   a new signing key, from secrets.token_urlsafe(48)
    seed_password        a new password for the fictional company's people
    app_llm_api_key      empty: put a provider's key in it to use a hosted model

With --from-env-file, a value that file has (APP_JWT_SECRET_KEY,
SEED_PASSWORD, APP_LLM_API_KEY) is used instead of a new one. That is for a
stack started before secrets were files: its audit seals were made with the
key in .env, and a new key would make every one of them stop matching.

A file that is already there is kept as it is, never replaced, for the same
reason: a new signing key signs everyone out and makes every audit seal stop
matching (docs/secrets.md). It says what it made, copied and kept, never
what is in them; the password is read from its file.

The folder is ignored by git. On Linux and macOS the files are made readable
by their owner only.

Exit codes: 0 fine, 1 when a folder is where a file should be. Docker leaves
one there when it was started before the file existed.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FOLDER = ROOT / "secrets"
IN_THE_WAY = 1
# The file the seed reads the password from (scripts/seed.py).
PASSWORD_FILE = "seed_password"  # noqa: S105 - the file's name, not a password


def _signing_key() -> str:
    return secrets.token_urlsafe(48)


def _password() -> str:
    return secrets.token_urlsafe(16)


def _nothing() -> str:
    return ""


# Each file, and how its first value is made.
FILES = {
    "app_jwt_secret_key": _signing_key,
    PASSWORD_FILE: _password,
    "app_llm_api_key": _nothing,
}
# The setting in a .env file each file's value can be taken from instead.
FROM_ENV = {
    "app_jwt_secret_key": "APP_JWT_SECRET_KEY",
    PASSWORD_FILE: "SEED_PASSWORD",
    "app_llm_api_key": "APP_LLM_API_KEY",
}


class InTheWayError(Exception):
    """A folder is where one of the files should be."""


def env_values(env_file: Path) -> dict[str, str]:
    """The values a .env file gives the settings the files hold, by setting name."""
    found: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        name, equals, value = (part.strip() for part in line.partition("="))
        value = value.strip("'\"")
        if equals and name in FROM_ENV.values() and value:
            found[name] = value
    return found


def make(folder: Path, given: dict[str, str] | None = None) -> dict[str, str]:
    """Make each file that is missing. Returns what happened to each: made, copied or kept.

    ``given`` holds values by setting name, from a .env file, to use instead
    of new ones.
    """
    given = given or {}
    folder.mkdir(parents=True, exist_ok=True)
    blocked = [name for name in FILES if (folder / name).is_dir()]
    if blocked:
        raise InTheWayError(", ".join(blocked))
    done: dict[str, str] = {}
    for name, first in FILES.items():
        path = folder / name
        if path.exists():
            done[name] = "kept"
            continue
        taken = given.get(FROM_ENV[name])
        # Made unreadable to others before anything is written into it.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write((first() if taken is None else taken) + "\n")
        done[name] = "made" if taken is None else "copied"
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--folder", type=Path, default=DEFAULT_FOLDER, help="default: secrets/")
    parser.add_argument("--from-env-file", type=Path, help="take the values this .env file has")
    arguments = parser.parse_args(argv)
    given = {} if arguments.from_env_file is None else env_values(arguments.from_env_file)
    try:
        done = make(arguments.folder, given)
    except InTheWayError as error:
        print(
            f"error: a folder is where a file should be, in {arguments.folder}: {error}. "
            "Docker Desktop makes an empty one when it starts before the file exists. "
            "Remove it and run this again.",
            file=sys.stderr,
        )
        return IN_THE_WAY
    for name, what in done.items():
        print(f"{what:6} {arguments.folder / name}")
    if done[PASSWORD_FILE] != "kept":
        print(f"the fictional company's password is in {arguments.folder / PASSWORD_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
