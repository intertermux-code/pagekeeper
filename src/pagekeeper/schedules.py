"""Rotations and one-off overrides.

A schedule is a list of users walked in order every `period_minutes`.  Who is
on call is derived, never stored, so a schedule needs no maintenance job.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from .clock import Clock, current
from .db import Database
from .errors import NotFound, UsageError
from .models import OnCall, Schedule, User
from .users import UserStore


class ScheduleStore:
    def __init__(self, db: Database, clock: Optional[Clock] = None) -> None:
        self.db = db
        self.clock = clock
        self.users = UserStore(db, clock)

    def add(
        self,
        name: str,
        period_minutes: int,
        rotation: Iterable[int] = (),
        *,
        start_at: Optional[int] = None,
    ) -> Schedule:
        name = name.strip()
        if not name:
            raise UsageError("schedule name must not be empty")
        if period_minutes <= 0:
            raise UsageError("period_minutes must be greater than zero")
        rotation = list(rotation)
        for user_id in rotation:
            self.users.get(user_id)
        with self.db.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM schedules WHERE name = ?", (name,)
            ).fetchone():
                raise UsageError(f"schedule {name!r} already exists")
            start = current(self.clock) if start_at is None else start_at
            cursor = conn.execute(
                "INSERT INTO schedules (name, period_minutes, start_at, created_at)"
                " VALUES (?, ?, ?, ?)",
                (name, period_minutes, start, current(self.clock)),
            )
            schedule = Schedule(
                id=cursor.lastrowid,
                name=name,
                period_minutes=period_minutes,
                start_at=start,
            )
            for position, user_id in enumerate(rotation):
                conn.execute(
                    "INSERT INTO schedule_users (schedule_id, position, user_id)"
                    " VALUES (?, ?, ?)",
                    (schedule.id, position, user_id),
                )
            return schedule

    def set_rotation(self, schedule_id: int, user_ids: Sequence[int]) -> None:
        if not user_ids:
            raise UsageError("a rotation needs at least one user")
        self.get(schedule_id)
        for user_id in user_ids:
            self.users.get(user_id)
        with self.db.connect() as conn:
            conn.execute(
                "DELETE FROM schedule_users WHERE schedule_id = ?", (schedule_id,)
            )
            for position, user_id in enumerate(user_ids):
                conn.execute(
                    "INSERT INTO schedule_users (schedule_id, position, user_id)"
                    " VALUES (?, ?, ?)",
                    (schedule_id, position, user_id),
                )

    def rotation(self, schedule_id: int) -> list[User]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT u.* FROM schedule_users su JOIN users u ON u.id = su.user_id"
                " WHERE su.schedule_id = ? ORDER BY su.position",
                (schedule_id,),
            ).fetchall()
        return [User.from_row(row) for row in rows]

    def add_override(
        self, schedule_id: int, user_id: int, start_at: int, end_at: int
    ) -> None:
        self.get(schedule_id)
        self.users.get(user_id)
        if end_at <= start_at:
            raise UsageError("override must end after it starts")
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO overrides (schedule_id, user_id, start_at, end_at)"
                " VALUES (?, ?, ?, ?)",
                (schedule_id, user_id, start_at, end_at),
            )

    def get(self, ident: int | str) -> Schedule:
        with self.db.connect() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM schedules WHERE id = ?", (int(ident),)
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        "SELECT * FROM schedules WHERE name = ?", (str(ident),)
                    ).fetchone()
            except (TypeError, ValueError):
                row = conn.execute(
                    "SELECT * FROM schedules WHERE name = ?", (str(ident),)
                ).fetchone()
        if row is None:
            raise NotFound(f"no such schedule: {ident}")
        return Schedule.from_row(row)

    def list_all(self) -> list[Schedule]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM schedules ORDER BY name").fetchall()
        return [Schedule.from_row(row) for row in rows]

    def on_call(
        self, ident: Optional[int | str] = None, *, now: Optional[int] = None
    ) -> list[OnCall]:
        """Who is on call right now, for one schedule or for all of them."""
        at = current(self.clock) if now is None else now
        schedules = (
            [self.get(ident)] if ident is not None else self.list_all()
        )
        return [self._on_call(schedule, at) for schedule in schedules]

    def _on_call(self, schedule: Schedule, at: int) -> OnCall:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT u.*, o.end_at AS override_end FROM overrides o"
                " JOIN users u ON u.id = o.user_id"
                " WHERE o.schedule_id = ? AND o.start_at <= ? AND o.end_at > ?"
                " ORDER BY o.end_at DESC, o.id DESC LIMIT 1",
                (schedule.id, at, at),
            ).fetchone()
        if row is not None:
            return OnCall(
                schedule=schedule,
                user=User.from_row(row),
                source="override",
                until=row["override_end"],
            )

        rotation = self.rotation(schedule.id)
        if not rotation:
            raise UsageError(
                f"schedule {schedule.name!r} has no rotation; "
                "add users with `pagekeeper schedule assign`"
            )
        period = schedule.period_minutes * 60
        elapsed = at - schedule.start_at
        stints = elapsed // period
        user = rotation[stints % len(rotation)]
        return OnCall(
            schedule=schedule,
            user=user,
            source="rotation",
            until=schedule.start_at + (stints + 1) * period,
        )
