"""SQLite schema and connection handling.

Connections are short lived and opened per operation so that the HTTP handler
threads and the escalation ticker never share a cursor.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import TARGET_KINDS

#: The `kind` list is baked into a CHECK constraint, so it has to be written
#: from one source of truth: databases created before a channel existed are
#: rebuilt on first open (see _upgrade_target_kinds).
_TARGET_KINDS_SQL = ", ".join(f"'{kind}'" for kind in TARGET_KINDS)

_STEP_TARGETS = f"""
CREATE TABLE IF NOT EXISTS step_targets (
    id      INTEGER PRIMARY KEY,
    step_id INTEGER NOT NULL REFERENCES policy_steps(id) ON DELETE CASCADE,
    kind    TEXT    NOT NULL CHECK (kind IN ({_TARGET_KINDS_SQL})),
    value   TEXT    NOT NULL
);
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    ntfy_topic  TEXT,
    webhook_url TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id             INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL UNIQUE,
    period_minutes INTEGER NOT NULL CHECK (period_minutes > 0),
    start_at       INTEGER NOT NULL,
    created_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_users (
    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (schedule_id, position)
);

CREATE TABLE IF NOT EXISTS overrides (
    id          INTEGER PRIMARY KEY,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    start_at    INTEGER NOT NULL,
    end_at      INTEGER NOT NULL,
    CHECK (end_at > start_at)
);

CREATE TABLE IF NOT EXISTS policies (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_steps (
    id            INTEGER PRIMARY KEY,
    policy_id     INTEGER NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    delay_minutes INTEGER NOT NULL CHECK (delay_minutes >= 0),
    UNIQUE (policy_id, position)
);

{_STEP_TARGETS}

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY,
    fingerprint TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL DEFAULT '',
    policy_id   INTEGER REFERENCES policies(id) ON DELETE SET NULL,
    state       TEXT    NOT NULL CHECK (state IN ('open', 'acked', 'resolved')),
    step        INTEGER NOT NULL DEFAULT -1,
    due_at      INTEGER,
    created_at  INTEGER NOT NULL,
    acked_at    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_alerts_fingerprint ON alerts(fingerprint, state);
CREATE INDEX IF NOT EXISTS idx_alerts_due ON alerts(state, due_at);

CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY,
    alert_id    INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    step        INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    destination TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '',
    sent_at     INTEGER NOT NULL
);
"""


def _upgrade_target_kinds(conn: sqlite3.Connection) -> None:
    """Widen `step_targets.kind` on a database created before a channel existed.

    SQLite cannot change a CHECK constraint in place.  Nothing references
    step_targets, so the rows survive a rebuild inside one transaction; without
    this an old file would reject `slack:` targets with a raw constraint error.
    """
    stored = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'step_targets'"
    ).fetchone()
    if stored is None or _TARGET_KINDS_SQL in (stored["sql"] or ""):
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE step_targets_upgrade AS"
            " SELECT id, step_id, kind, value FROM step_targets"
        )
        conn.execute("DROP TABLE step_targets")
        conn.execute(_STEP_TARGETS)
        conn.execute(
            "INSERT INTO step_targets (id, step_id, kind, value)"
            " SELECT id, step_id, kind, value FROM step_targets_upgrade"
        )
        conn.execute("DROP TABLE step_targets_upgrade")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


class Database:
    """A file-backed SQLite database with lazily created schema."""

    def __init__(self, path: str | Path) -> None:
        # `~` is resolved once, here, so the directory we create and the file
        # we connect to can never be two different paths again.
        self.path = os.path.expanduser(str(path))
        self._lock = threading.Lock()
        self._ready = False

    def init(self) -> None:
        parent = Path(self.path).parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._open()
            try:
                conn.executescript(SCHEMA)
                conn.commit()
                _upgrade_target_kinds(conn)
                conn.commit()
            finally:
                conn.close()
            self._ready = True

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if not self._ready:
            self.init()
        conn = self._open()
        try:
            with conn:
                yield conn
        finally:
            conn.close()
