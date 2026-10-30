"""Write the service's OpenAPI description to docs/openapi.json.

    python -m scripts.openapi           # write it
    python -m scripts.openapi --check   # exit code 1 if the file isn't what the service says

In production the service doesn't publish its description at /openapi.json,
so the file in the repository is where a reader finds it. A test fails when
the file is out of date, so a change to a route comes with the file
(tests/integration/test_api_responses.py).

The description doesn't depend on any setting or secret. It is made from a
service that is never started, over a database that is never opened, with a
signing key that signs nothing.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "docs" / "openapi.json"
OUT_OF_DATE = 1


def description() -> dict[str, Any]:
    settings = Settings(
        _env_file=None,
        jwt_secret_key=SecretStr("o" * 48),
        database_url="sqlite://",
    )
    return create_app(settings).openapi()


def as_text(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the service's OpenAPI description.")
    parser.add_argument("--check", action="store_true", help="only check the file is up to date")
    args = parser.parse_args(argv)

    text = as_text(description())
    if args.check:
        current = FILE.read_text(encoding="utf-8") if FILE.exists() else ""
        if current != text:
            print(f"{FILE.relative_to(ROOT).as_posix()} is out of date: python -m scripts.openapi")
            return OUT_OF_DATE
        return 0
    FILE.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {FILE.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
