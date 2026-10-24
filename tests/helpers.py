"""Test doubles: a clock we move by hand and an HTTP layer that records."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


class Sink:
    """A local HTTP endpoint that records what was POSTed to it.

    Stands in for Slack, Discord or anything else on the other side of a real
    `urlopen`, so payload shapes can be checked over the wire without leaving
    the test process.
    """

    def __init__(self, status: int = 200) -> None:
        self.requests: list[tuple[str, dict, bytes]] = []
        recorded = self.requests
        reply = bytes(str(status), "ascii")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                recorded.append((self.path, dict(self.headers), self.rfile.read(length)))
                self.send_response(status)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass  # the suite is quieter without a line per request

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def payloads(self) -> list[dict]:
        """Every recorded body parsed as JSON, so only for JSON-only sinks."""
        return [json.loads(body) for _path, _headers, body in self.requests]

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()
