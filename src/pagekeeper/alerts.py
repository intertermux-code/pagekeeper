"""Alert storage, fingerprint extraction and deduplication."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional, Sequence

from .clock import Clock, current
from .db import Database
from .errors import NotFound
from .models import Alert

# Checked in order; the first match wins.  Nested `labels`/`alert` objects are
# searched too so Alertmanager-style payloads work without extra configuration.
FINGERPRINT_KEYS = ("fingerprint", "dedup_key", "routing_key", "dedup", "key", "id")
IDENTITY_KEYS = ("alertname", "service", "host", "instance", "cluster", "severity")
TITLE_KEYS = ("title", "subject", "alertname", "summary", "name", "message")
STATE_KEYS = ("state", "status", "event", "type")
RESOLVED_STATES = ("resolved", "closed", "recovered", "ok", "restore")
NESTED_KEYS = ("labels", "alert", "attributes", "metadata", "event", "data")

MAX_BODY = 4000

# Distinguishes "leave this column alone" from "set it to NULL".
_KEEP = object()


def _sources(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sources = [payload]
    for key in NESTED_KEYS:
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    return sources


def _first_value(payload: Mapping[str, Any], keys: Sequence[str]) -> Optional[Any]:
    for source in _sources(payload):
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def looks_resolved(payload: Mapping[str, Any]) -> bool:
    value = _first_value(payload, STATE_KEYS)
    if value is None:
        return False
    return str(value).strip().lower() in RESOLVED_STATES


def extract_fingerprint(payload: Mapping[str, Any]) -> str:
    given = _first_value(payload, FINGERPRINT_KEYS)
    if given is not None:
        return str(given).strip()[:200]

    identity: dict[str, Any] = {}
    for source in _sources(payload):
        for key in IDENTITY_KEYS:
            value = source.get(key)
            if value not in (None, "") and key not in identity:
                identity[key] = value
    if identity:
        canonical = json.dumps(identity, sort_keys=True, default=str)
    else:
        canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def extract_title(payload: Mapping[str, Any], fingerprint: str) -> str:
    given = _first_value(payload, TITLE_KEYS)
    if given is None:
        return f"alert {fingerprint[:12]}"
    return str(given).strip()[:200]


def extract_body(payload: Mapping[str, Any]) -> str:
    detail = _first_value(payload, ("description", "text", "message", "content"))
    if detail is None:
        detail = json.dumps(payload, sort_keys=True, default=str)
    body = str(detail).strip()
    if len(body) > MAX_BODY:
        body = body[: MAX_BODY - 1] + "…"
    return body


class AlertStore:
    def __init__(self, db: Database, clock: Optional[Clock] = None) -> None:
        self.db = db
        self.clock = clock

    def active(self, fingerprint: str) -> Optional[Alert]:
        """The alert currently holding this fingerprint, open or acked."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE fingerprint = ?"
                " AND state IN ('open', 'acked') ORDER BY id DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
        return Alert.from_row(row) if row is not None else None

    def create(
        self,
        *,
        fingerprint: str,
        title: str,
        body: str = "",
        policy_id: Optional[int] = None,
    ) -> Alert:
        now = current(self.clock)
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO alerts"
                " (fingerprint, title, body, policy_id, state, step, created_at)"
                " VALUES (?, ?, ?, ?, 'open', -1, ?)",
                (fingerprint, title, body, policy_id, now),
            )
        # due_at stays NULL until the engine pages the first step, so the
        # ticker can never pick up an alert that has not been fired yet.
        return Alert(
            id=cursor.lastrowid,
            fingerprint=fingerprint,
            title=title,
            body=body,
            policy_id=policy_id,
            state="open",
            step=-1,
            due_at=None,
            created_at=now,
        )

    def get(self, alert_id: int) -> Alert:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"no such alert: {alert_id}")
        return Alert.from_row(row)

    def update(
        self,
        alert_id: int,
        *,
        state: Any = _KEEP,
        step: Any = _KEEP,
        due_at: Any = _KEEP,
        acked_at: Any = _KEEP,
    ) -> Alert:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("state", state),
            ("step", step),
            ("due_at", due_at),
            ("acked_at", acked_at),
        ):
            if value is _KEEP:
                continue
            clauses.append(f"{column} = ?")
            params.append(value)
        if not clauses:
            return self.get(alert_id)

        params.append(alert_id)
        with self.db.connect() as conn:
            conn.execute(
                f"UPDATE alerts SET {', '.join(clauses)} WHERE id = ?", tuple(params)
            )
        return self.get(alert_id)

    def ack(self, alert_id: int) -> Alert:
        """Acknowledge: stops escalation, keeps dedup protection in place."""
        alert = self.get(alert_id)
        if alert.state == "acked":
            return alert
        if alert.state == "resolved":
            raise NotFound(f"alert {alert_id} is already resolved")
        return self.update(
            alert_id, state="acked", due_at=None, acked_at=current(self.clock)
        )

    def resolve(self, alert_id: int) -> Alert:
        return self.update(alert_id, state="resolved", due_at=None)

    def resolve_by_fingerprint(self, fingerprint: str) -> Optional[Alert]:
        active = self.active(fingerprint)
        if active is None:
            return None
        return self.update(active.id, state="resolved", due_at=None)

    def due(self, *, now: Optional[int] = None) -> list[Alert]:
        at = current(self.clock) if now is None else now
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE state = 'open'"
                " AND due_at IS NOT NULL AND due_at <= ? ORDER BY id",
                (at,),
            ).fetchall()
        return [Alert.from_row(row) for row in rows]

    def open(self) -> list[Alert]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE state IN ('open', 'acked')"
                " ORDER BY CASE state WHEN 'open' THEN 0 ELSE 1 END, id DESC"
                " LIMIT 200"
            ).fetchall()
        return [Alert.from_row(row) for row in rows]

    def record_page(
        self,
        alert_id: int,
        *,
        step: int,
        kind: str,
        destination: str,
        ok: bool,
        detail: str = "",
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO pages"
                " (alert_id, step, kind, destination, ok, detail, sent_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (alert_id, step, kind, destination, int(ok), detail[:500],
                 current(self.clock)),
            )

    def pages(self, alert_id: Optional[int] = None) -> list[dict]:
        query = "SELECT * FROM pages"
        params: tuple = ()
        if alert_id is not None:
            query += " WHERE alert_id = ?"
            params = (alert_id,)
        query += " ORDER BY id"
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute(query, params)]
