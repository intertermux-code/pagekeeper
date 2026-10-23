"""The people directory. A user is a name plus however we can reach them."""

from __future__ import annotations

from typing import Optional

from .clock import Clock, current
from .db import Database
from .errors import NotFound, UsageError
from .models import User


class UserStore:
    def __init__(self, db: Database, clock: Optional[Clock] = None) -> None:
        self.db = db
        self.clock = clock

    def add(
        self,
        name: str,
        *,
        ntfy_topic: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> User:
        name = name.strip()
        if not name:
            raise UsageError("user name must not be empty")
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None:
                raise UsageError(
                    f"user {name!r} already exists (id {existing['id']})"
                )
            cursor = conn.execute(
                "INSERT INTO users (name, ntfy_topic, webhook_url, created_at)"
                " VALUES (?, ?, ?, ?)",
                (name, ntfy_topic, webhook_url, current(self.clock)),
            )
            return User(
                id=cursor.lastrowid,
                name=name,
                ntfy_topic=ntfy_topic,
                webhook_url=webhook_url,
            )

    def get(self, ident: int | str) -> User:
        """Look a user up by id or by name."""
        row = self._row(ident)
        if row is None:
            raise NotFound(f"no such user: {ident}")
        return User.from_row(row)

    def find(self, ident: int | str) -> Optional[User]:
        row = self._row(ident)
        return User.from_row(row) if row is not None else None

    def _row(self, ident: int | str):
        with self.db.connect() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM users WHERE id = ?", (int(ident),)
                ).fetchone()
                if row is not None:
                    return row
            except (TypeError, ValueError):
                pass
            return conn.execute(
                "SELECT * FROM users WHERE name = ?", (str(ident),)
            ).fetchone()

    def list_all(self) -> list[User]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY name").fetchall()
        return [User.from_row(row) for row in rows]
