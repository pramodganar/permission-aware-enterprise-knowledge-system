"""Look for secrets in the repository: every tracked file, and every commit ever made.

    python -m scripts.scan_secrets

Two kinds of finding:

- Something shaped like a secret: a provider's API key, a private key, a
  signed token, or a value given to one of the service's secret settings.
- One of the secrets in the local ``.env``, word for word, wherever it is.
  This is the one that matters most and the one a pattern can't see: a
  signing key is just random text.

A finding says where and what kind, never the text it matched, so the
output can be pasted anywhere. Deleting a secret in a later commit doesn't
remove it from the history, which is why the history is scanned too: a
secret found there has leaked, and the only fix is to replace it.

Binary files (the PDF and Word documents) are scanned as they are stored.
Text inside a compressed Word file isn't seen.

Exit codes: 0 nothing found, 1 something found, 2 not a git checkout.
"""

import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOT_A_CHECKOUT = 2

# What each kind of secret looks like. Written so they don't match themselves.
PATTERNS = {
    "Groq API key": re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),
    "OpenAI-style API key": re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "Hugging Face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "signed token": re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    ),
    # A value on the same line as one of the service's secret settings. Not
    # one that starts with { or $: that names a variable, as an f-string or
    # compose.yaml does, and holds no value of its own.
    "value for a secret setting": re.compile(
        r"\b(?:APP_JWT_SECRET_KEY|APP_LLM_API_KEY|SEED_PASSWORD)[ \t]*[=:][ \t]*"
        r"['\"]?(?![{$])[^\s'\"#]{8,}"
    ),
}
# Settings in .env whose values are secrets.
SECRET_NAMES = re.compile(r"KEY|SECRET|PASSWORD|TOKEN")
# Shorter than this, a value is too likely to occur by chance to search for.
MIN_SECRET_LENGTH = 8


@dataclass(frozen=True)
class Finding:
    # A tracked file, or "history".
    where: str
    kind: str

    def __str__(self) -> str:
        return f"{self.where}: {self.kind}"


class NotACheckoutError(Exception):
    """There is no git repository to scan."""


def _git(root: Path, *args: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            ["git", *args],  # noqa: S607 - git from the PATH, like a developer runs it
            cwd=root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise NotACheckoutError(f"git {args[0]} failed in {root}") from error
    return done.stdout.decode("utf-8", "replace")


def kinds_in(text: str, secrets: dict[str, str] | None = None) -> list[str]:
    """The kinds of secret in ``text``: by shape, and the local secrets by name."""
    found = [kind for kind, pattern in PATTERNS.items() if pattern.search(text)]
    found.extend(f"the value of {name}" for name, value in (secrets or {}).items() if value in text)
    return found


def local_secrets(env_file: Path) -> dict[str, str]:
    """The secret values in a .env file, by setting name. Empty if there is none."""
    if not env_file.exists():
        return {}
    found = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        value = value.strip("'\"")
        if SECRET_NAMES.search(name) and len(value) >= MIN_SECRET_LENGTH:
            found[name] = value
    return found


def scan(root: Path = ROOT, secrets: dict[str, str] | None = None) -> list[Finding]:
    """Every finding in the files and the history of the repository at ``root``.

    The files are the tracked ones and the new ones git would add, so the scan
    run before a commit sees what that commit is about to hold. What git
    ignores, .env above all, is left out.
    """
    looking_for = local_secrets(root / ".env") if secrets is None else secrets
    findings: list[Finding] = []
    listed = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for name in filter(None, listed.split("\0")):
        path = root / name
        if path.is_file():
            text = path.read_bytes().decode("utf-8", "replace")
            findings.extend(Finding(name, kind) for kind in kinds_in(text, looking_for))
    history = _git(root, "log", "--all", "-p", "--no-color", "--no-ext-diff")
    findings.extend(Finding("history", kind) for kind in kinds_in(history, looking_for))
    return findings


def report(findings: Iterable[Finding], searched: Iterable[str]) -> None:
    for finding in findings:
        print(f"FOUND {finding}")
    names = sorted(searched)
    print(f"also searched for the values of: {', '.join(names) if names else 'nothing (no .env)'}")


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        findings = scan(ROOT)
    except NotACheckoutError as error:
        print(f"error: {error}", file=sys.stderr)
        return NOT_A_CHECKOUT
    report(findings, local_secrets(ROOT / ".env"))
    print("nothing found" if not findings else f"{len(findings)} found")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
