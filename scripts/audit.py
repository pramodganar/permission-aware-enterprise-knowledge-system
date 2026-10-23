"""Check that the audit trail is as it was written.

    python -m scripts.audit check

Checks every record against its seal and the IDs for gaps, and prints the
highest ID. Compare that with the highest ``audit_id`` in the application
log: the log is kept apart from the database, and records deleted from the
end of the trail, seals and all, show only there (decision record 0013).

Exit codes: 0 intact, 1 error, 2 when something doesn't match, so a
scheduled check can tell them apart.
"""

import argparse
import sys

from sqlalchemy.exc import SQLAlchemyError

from app.audit.seals import Check, check_trail, seal_key
from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.logging_setup import configure_logging

CHANGED = 2


def check(settings: Settings) -> int:
    engine = create_db_engine(settings.database_url)
    try:
        with create_session_factory(engine)() as db:
            found = check_trail(db, seal_key(settings))
    finally:
        engine.dispose()
    report(found)
    return 0 if found.intact else CHANGED


def report(found: Check) -> None:
    print(f"records: {found.records}, sealed: {found.sealed}")
    if found.before_sealing:
        print(f"written before sealing, not checked: {found.before_sealing}")
    print(f"highest ID: {found.last_id if found.last_id is not None else 'none'}")
    for problem in found.problems:
        print(f"PROBLEM: {problem}")
    print("intact" if found.intact else f"{len(found.problems)} problems")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the audit trail against its seals.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="check every record against its seal")
    parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return check(settings)
    except SQLAlchemyError as error:
        # The type only: a database error can quote what it was reading.
        print(f"error: the trail could not be read ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
