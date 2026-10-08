"""FastAPI dependencies for permission checks.

    @router.post("/documents")
    def ingest(user: Annotated[User, Depends(require_permission(Permission.MANAGE_DOCUMENTS))]):
        ...

The dependency authenticates first (401 if that fails), then checks the
permission (403 if that fails), and hands the user to the endpoint.
"""

from collections.abc import Callable

from app.auth.dependencies import CurrentUser
from app.authorization.checks import ensure_permission
from app.authorization.permissions import Permission
from app.models import User


def require_permission(permission: Permission) -> Callable[[CurrentUser], User]:
    def dependency(user: CurrentUser) -> User:
        ensure_permission(user, permission)
        return user

    return dependency
