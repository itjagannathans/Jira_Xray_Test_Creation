"""Flask-Login user model wrapping a Tb_user row."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class User:
    id: int
    username: str
    password_hash: str
    is_admin: bool
    is_active_flag: bool
    jira_pat: Optional[str]
    created_date: str
    full_name: Optional[str] = None

    # Flask-Login interface
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_active(self) -> bool:  # used by Flask-Login
        return self.is_active_flag

    @property
    def is_anonymous(self) -> bool:
        return False

    def get_id(self) -> str:
        return str(self.id)

    @classmethod
    def from_row(cls, row) -> "User":
        keys = row.keys() if hasattr(row, "keys") else []
        return cls(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            is_admin=bool(row["is_admin"]),
            is_active_flag=bool(row["is_active"]),
            jira_pat=row["jira_pat"],
            created_date=row["created_date"],
            full_name=row["full_name"] if "full_name" in keys else None,
        )
