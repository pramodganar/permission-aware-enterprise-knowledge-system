"""FastAPI dependencies for permission checks.

    @router.post("/documents")
    def ingest(user: Annotated[User, Depends(require_permission(Permission.MANAGE_DOCUMENTS))]):
        ...

The dependency authenticates first (401 if that fails), then checks the
permission (403 if that fails), and hands the user to the endpoint.

It is an object rather than a closure so that the permission a route needs
can be read off the route. The route walk in the security tests does that
for every route, so a route that lost its check, or never had one, is found.
"""

from dataclasses import dataclass

from app.auth.dependencies import CurrentUser
from app.authorization.checks import ensure_permission
from app.authorization.permissions import Permission
from app.models import User


@dataclass(frozen=True)
class RequirePermission:
    permission: Permission

    def __call__(self, user: CurrentUser) -> User:
        ensure_permission(user, self.permission)
        return user


def require_permission(permission: Permission) -> RequirePermission:
    return RequirePermission(permission)
