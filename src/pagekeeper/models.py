"""Row-shaped value objects returned by the stores."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

TARGET_KINDS = ("user", "schedule", "ntfy", "webhook")
CHANNELS = ("ntfy", "webhook")


@dataclass(frozen=True)
class User:
    id: int
    name: str
    ntfy_topic: Optional[str] = None
    webhook_url: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "User":
        return cls(
            id=row["id"],
            name=row["name"],
            ntfy_topic=row["ntfy_topic"],
            webhook_url=row["webhook_url"],
        )


@dataclass(frozen=True)
class Schedule:
    id: int
    name: str
    period_minutes: int
    start_at: int

    @classmethod
    def from_row(cls, row) -> "Schedule":
        return cls(
            id=row["id"],
            name=row["name"],
            period_minutes=row["period_minutes"],
            start_at=row["start_at"],
        )


@dataclass(frozen=True)
class OnCall:
    schedule: Schedule
    user: User
    source: str
    until: int


@dataclass(frozen=True)
class Target:
    kind: str
    value: str


@dataclass(frozen=True)
class Step:
    id: int
    position: int
    delay_minutes: int
    targets: tuple[Target, ...] = ()


@dataclass(frozen=True)
class Policy:
    id: int
    name: str
    steps: tuple[Step, ...] = ()


@dataclass(frozen=True)
class Alert:
    id: int
    fingerprint: str
    title: str
    body: str
    policy_id: Optional[int]
    state: str
    step: int
    due_at: Optional[int]
    created_at: int
    acked_at: Optional[int] = None

    @classmethod
    def from_row(cls, row) -> "Alert":
        return cls(
            id=row["id"],
            fingerprint=row["fingerprint"],
            title=row["title"],
            body=row["body"],
            policy_id=row["policy_id"],
            state=row["state"],
            step=row["step"],
            due_at=row["due_at"],
            created_at=row["created_at"],
            acked_at=row["acked_at"],
        )


@dataclass(frozen=True)
class Recipient:
    """A concrete channel to page, resolved from an escalation target."""

    channel: str
    destination: str
    label: str


@dataclass(frozen=True)
class PageMessage:
    title: str
    text: str
    priority: int = 3
    fields: Mapping[str, Any] = field(default_factory=dict)
