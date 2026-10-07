"""Database models. Importing this package registers every table on Base.metadata."""

from app.models.department import Department
from app.models.user import User

__all__ = ["Department", "User"]
