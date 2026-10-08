"""What each role is allowed to do.

This table is the whole role-based policy. It is deliberately code and not
database rows: changing who can do what is a reviewed change with tests, not
an update statement.

Rules:
- A user's permissions are the union of the permissions of their roles.
- There are no "deny" entries. Access is only ever added, starting from none.
- A role the code doesn't know about grants nothing.
- An inactive user has no permissions, whatever roles they still hold.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from app.models import RoleCode, User


class Permission(StrEnum):
    READ_GENERAL_DOCUMENTS = "documents:read:general"
    READ_OWN_DEPARTMENT_DOCUMENTS = "documents:read:own_department"
    READ_ALL_DEPARTMENT_DOCUMENTS = "documents:read:all_departments"
    READ_GRANTED_DOCUMENTS = "documents:read:granted"
    MANAGE_DOCUMENTS = "documents:manage"
    READ_AUDIT_LOG = "audit:read"


ROLE_PERMISSIONS: Mapping[RoleCode, frozenset[Permission]] = MappingProxyType(
    {
        RoleCode.EMPLOYEE: frozenset(
            {
                Permission.READ_GENERAL_DOCUMENTS,
                Permission.READ_OWN_DEPARTMENT_DOCUMENTS,
                Permission.READ_GRANTED_DOCUMENTS,
            }
        ),
        RoleCode.EXECUTIVE: frozenset(
            {
                Permission.READ_GENERAL_DOCUMENTS,
                Permission.READ_OWN_DEPARTMENT_DOCUMENTS,
                Permission.READ_GRANTED_DOCUMENTS,
                Permission.READ_ALL_DEPARTMENT_DOCUMENTS,
            }
        ),
        # Managing documents and reading the audit log include no read access
        # to document content.
        RoleCode.KNOWLEDGE_ADMIN: frozenset({Permission.MANAGE_DOCUMENTS}),
        RoleCode.SECURITY_REVIEWER: frozenset({Permission.READ_AUDIT_LOG}),
    }
)


def permissions_for(user: User) -> frozenset[Permission]:
    """Return everything this user is allowed to do."""
    if not user.is_active:
        return frozenset()

    granted: set[Permission] = set()
    for role in user.roles:
        try:
            code = RoleCode(role.code)
        except ValueError:
            continue
        granted |= ROLE_PERMISSIONS.get(code, frozenset())
    return frozenset(granted)
