"""Escalation policies: ordered steps, each with a delay and its targets.

`delay_minutes` is how long pagekeeper waits on that step before escalating
past it, so the delay of the final step is never used.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from .clock import Clock, current
from .db import Database
from .errors import NotFound, UsageError
from .models import Policy, Step, TARGET_KINDS, Target


class PolicyStore:
    def __init__(self, db: Database, clock: Optional[Clock] = None) -> None:
        self.db = db
        self.clock = clock

    def add(
        self, name: str, steps: Sequence[tuple[int, Iterable[Target]]]
    ) -> Policy:
        name = name.strip()
        if not name:
            raise UsageError("policy name must not be empty")
        if not steps:
            raise UsageError("a policy needs at least one step")

        parsed: list[tuple[int, tuple[Target, ...]]] = []
        for position, (delay, targets) in enumerate(steps):
            targets = tuple(targets)
            if delay < 0:
                raise UsageError(f"step {position + 1}: delay must not be negative")
            if not targets:
                raise UsageError(f"step {position + 1} needs at least one target")
            for target in targets:
                self._check_target(target, position + 1)
            parsed.append((delay, targets))

        with self.db.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM policies WHERE name = ?", (name,)
            ).fetchone():
                raise UsageError(f"policy {name!r} already exists")
            created = current(self.clock)
            cursor = conn.execute(
                "INSERT INTO policies (name, created_at) VALUES (?, ?)",
                (name, created),
            )
            policy_id = cursor.lastrowid
            for position, (delay, targets) in enumerate(parsed):
                step_cursor = conn.execute(
                    "INSERT INTO policy_steps (policy_id, position, delay_minutes)"
                    " VALUES (?, ?, ?)",
                    (policy_id, position, delay),
                )
                for target in targets:
                    conn.execute(
                        "INSERT INTO step_targets (step_id, kind, value)"
                        " VALUES (?, ?, ?)",
                        (step_cursor.lastrowid, target.kind, target.value),
                    )
        return Policy(
            id=policy_id,
            name=name,
            steps=tuple(
                Step(
                    id=-1,
                    position=position,
                    delay_minutes=delay,
                    targets=targets,
                )
                for position, (delay, targets) in enumerate(parsed)
            ),
        )

    def append_step(
        self, policy_id: int, delay_minutes: int, targets: Iterable[Target]
    ) -> Step:
        targets = tuple(targets)
        policy = self.get(policy_id)
        if delay_minutes < 0:
            raise UsageError("delay must not be negative")
        if not targets:
            raise UsageError("every step needs at least one target")
        for target in targets:
            self._check_target(target, len(policy.steps) + 1)
        position = len(policy.steps)
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO policy_steps (policy_id, position, delay_minutes)"
                " VALUES (?, ?, ?)",
                (policy_id, position, delay_minutes),
            )
            for target in targets:
                conn.execute(
                    "INSERT INTO step_targets (step_id, kind, value) VALUES (?, ?, ?)",
                    (cursor.lastrowid, target.kind, target.value),
                )
        return Step(
            id=cursor.lastrowid,
            position=position,
            delay_minutes=delay_minutes,
            targets=targets,
        )

    @staticmethod
    def _check_target(target: Target, position: Optional[int] = None) -> None:
        if target.kind not in TARGET_KINDS:
            raise UsageError(
                f"unknown target kind {target.kind!r}; "
                f"expected one of {', '.join(TARGET_KINDS)}"
            )
        if not target.value.strip():
            where = "" if position is None else f" in step {position}"
            raise UsageError(f"empty target value{where}")

    def steps(self, policy_id: int) -> list[Step]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM policy_steps WHERE policy_id = ? ORDER BY position",
                (policy_id,),
            ).fetchall()
            steps = []
            for row in rows:
                targets = [
                    Target(kind=t["kind"], value=t["value"])
                    for t in conn.execute(
                        "SELECT kind, value FROM step_targets WHERE step_id = ?"
                        " ORDER BY id",
                        (row["id"],),
                    )
                ]
                steps.append(
                    Step(
                        id=row["id"],
                        position=row["position"],
                        delay_minutes=row["delay_minutes"],
                        targets=tuple(targets),
                    )
                )
        return steps

    def get(self, ident: int | str) -> Policy:
        with self.db.connect() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM policies WHERE id = ?", (int(ident),)
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        "SELECT * FROM policies WHERE name = ?", (str(ident),)
                    ).fetchone()
            except (TypeError, ValueError):
                row = conn.execute(
                    "SELECT * FROM policies WHERE name = ?", (str(ident),)
                ).fetchone()
        if row is None:
            raise NotFound(f"no such policy: {ident}")
        return Policy(
            id=row["id"], name=row["name"], steps=tuple(self.steps(row["id"]))
        )

    def find(self, ident: Optional[int | str]) -> Optional[Policy]:
        if ident is None:
            return None
        try:
            return self.get(ident)
        except NotFound:
            return None

    def list_all(self) -> list[Policy]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM policies ORDER BY name").fetchall()
        return [
            Policy(id=row["id"], name=row["name"]) for row in rows
        ]
