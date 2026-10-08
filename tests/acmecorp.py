"""A small AcmeCorp for tests: departments, users, documents and grants."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.authorization.service import can_read, readable_documents
from app.models import Classification, Department, Document, DocumentGrant, Role, RoleCode, User

# Tests that depend on time use this fixed moment instead of the clock.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@dataclass
class World:
    db: Session
    users: dict[str, User]
    documents: dict[str, Document]

    def grant(self, document: str, user: str, expires_at: datetime | None = None) -> None:
        self.db.add(
            DocumentGrant(
                document=self.documents[document],
                user_id=self.users[user].id,
                granted_by_id=self.users["admin"].id,
                expires_at=expires_at,
            )
        )
        self.db.commit()

    def can_read(self, user: str, document: str) -> bool:
        return can_read(self.users[user], self.documents[document], NOW)

    def readable(self, user: str) -> set[str]:
        found = self.db.scalars(readable_documents(self.users[user], NOW)).all()
        return {name for name, document in self.documents.items() if document in found}


def build_world(db: Session) -> World:
    departments = {
        code: Department(code=code, name=code.title())
        for code in ("engineering", "hr", "executive")
    }
    roles = {code: Role(code=code) for code in RoleCode}
    db.add_all([*departments.values(), *roles.values()])

    def user(name: str, department: str, *role_codes: RoleCode) -> User:
        return User(
            email=f"{name}@acmecorp.example",
            full_name=name.title(),
            hashed_password="x",
            department=departments[department],
            roles=[roles[code] for code in role_codes],
        )

    users = {
        "alice": user("alice", "engineering", RoleCode.EMPLOYEE),
        "carol": user("carol", "hr", RoleCode.EMPLOYEE),
        "grace": user("grace", "executive", RoleCode.EXECUTIVE),
        "admin": user("admin", "engineering", RoleCode.KNOWLEDGE_ADMIN),
        "reviewer": user("reviewer", "hr", RoleCode.SECURITY_REVIEWER),
        "nobody": user("nobody", "engineering"),
    }

    def document(title: str, department: str, classification: Classification) -> Document:
        return Document(
            title=title, department=departments[department], classification=classification
        )

    documents = {
        "leave_policy": document("Leave Policy", "hr", Classification.GENERAL),
        "runbook": document("Deployment Runbook", "engineering", Classification.DEPARTMENTAL),
        "salary_bands": document("Salary Bands", "hr", Classification.DEPARTMENTAL),
        "exec_comp": document(
            "Executive Compensation Strategy", "executive", Classification.DEPARTMENTAL
        ),
        "case_file": document("HR Case File", "hr", Classification.RESTRICTED),
    }

    db.add_all([*users.values(), *documents.values()])
    db.commit()
    return World(db, users, documents)
