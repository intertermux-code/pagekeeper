"""Test doubles: a clock we move by hand and an HTTP layer that records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pagekeeper.schedules import Schedule, ScheduleStore
from pagekeeper.users import UserStore

BASE = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)


def epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def minutes(count: int) -> int:
    """Epoch of BASE + count minutes, for reading test setups at a glance."""
    return epoch(BASE + timedelta(minutes=count))


@dataclass
class Rota:
    users: UserStore
    schedules: ScheduleStore
    schedule: Schedule

    def id_of(self, name: str) -> str:
        return str(self.users.get(name).id)


def seed_rota(db, clock) -> Rota:
    """Two engineers with ntfy topics on an hourly rotation starting at BASE."""
    users = UserStore(db, clock)
    schedules = ScheduleStore(db, clock)
    alice = users.add("alice", ntfy_topic="alice-phone")
    bob = users.add("bob", ntfy_topic="bob-phone")
    schedule = schedules.add("eu", 60, [alice.id, bob.id], start_at=epoch(BASE))
    return Rota(users=users, schedules=schedules, schedule=schedule)


class FakeClock:
    def __init__(self, moment: datetime = BASE) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, *, minutes: int = 0, seconds: int = 0) -> "FakeClock":
        self.moment += timedelta(minutes=minutes, seconds=seconds)
        return self

    @property
    def at(self) -> int:
        return epoch(self.moment)


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def close(self) -> None:
        pass


class RecordingOpener:
    """Stands in for urllib.request.urlopen so no test touches the network."""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.requests: list = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.status)

    @property
    def urls(self) -> list[str]:
        return [request.full_url for request in self.requests]

    @property
    def bodies(self) -> list[bytes]:
        return [request.data or b"" for request in self.requests]
