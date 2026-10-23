"""Checking that the audit trail is as it was written (threat T22, decision record 0013).

Every record is sealed when it is written: an HMAC of all its fields, its ID
included, under a key only the service has. Someone who can change the
database but doesn't have the key can't change a record, add one, or move
one to another ID without its seal failing to match. A record deleted leaves
a gap in the IDs, since SQLite gives each new record the next ID after the
highest.

What a check can't see is the newest records deleted, with their seals:
nothing is left to say they were there. Each record's ID is written to the
application log once it is stored, and the log is kept apart from the
database, so the highest ID in the log against the highest in the trail
shows that.

The key is derived from the token signing key, so there is one secret to
keep. Anyone with the signing key can already be anyone in the service.
"""

import hashlib
import hmac
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import AuditRecord, AuditSeal, seal_of

# What the derived key is for, so it is a different key from the signing key.
PURPOSE = b"audit record seals"


def seal_key(settings: Settings) -> bytes:
    secret = settings.jwt_secret_key.get_secret_value().encode()
    return hmac.new(secret, PURPOSE, hashlib.sha256).digest()


@dataclass
class Check:
    """What checking the trail found."""

    records: int = 0
    sealed: int = 0
    # Written before records were sealed, so they can't be checked. Only the
    # oldest records can be: one unsealed after a sealed one is a problem.
    before_sealing: int = 0
    last_id: int | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def intact(self) -> bool:
        return not self.problems


def check_trail(db: Session, key: bytes) -> Check:
    """Check every record against its seal, and the IDs for gaps."""
    found = Check()
    rows = db.execute(select(AuditSeal.record_id, AuditSeal.mac))
    # Not dict(rows), which loses the types.
    seals: dict[int, str] = {record_id: mac for record_id, mac in rows}  # noqa: C416
    expected_id = 1
    for record in db.scalars(select(AuditRecord).order_by(AuditRecord.id)):
        found.records += 1
        if record.id == expected_id + 1:
            found.problems.append(f"record {expected_id} is missing")
        elif record.id != expected_id:
            found.problems.append(f"records {expected_id} to {record.id - 1} are missing")
        expected_id = record.id + 1
        found.last_id = record.id

        mac = seals.pop(record.id, None)
        if mac is None:
            if found.sealed:
                found.problems.append(f"record {record.id} has no seal")
            else:
                found.before_sealing += 1
            continue
        found.sealed += 1
        if not hmac.compare_digest(mac, seal_of(record, key)):
            found.problems.append(f"record {record.id} does not match its seal")

    found.problems.extend(f"seal {record_id} has no record" for record_id in sorted(seals))
    return found
