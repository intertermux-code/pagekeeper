"""Webhook ingress: turn a JSON payload into an alert, or reuse the one we have."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .alerts import (
    AlertStore,
    extract_body,
    extract_fingerprint,
    extract_title,
    looks_resolved,
)
from .clock import Clock
from .db import Database
from .engine import EscalationEngine
from .models import Alert
from .policies import PolicyStore


@dataclass(frozen=True)
class IngestResult:
    alert: Optional[Alert]
    created: bool
    resolved: bool

    @property
    def deduplicated(self) -> bool:
        return self.alert is not None and not self.created and not self.resolved


def ingest(
    db: Database,
    payload: Mapping[str, Any],
    *,
    engine: Optional[EscalationEngine] = None,
    default_policy: Optional[str] = None,
    clock: Optional[Clock] = None,
) -> IngestResult:
    alerts = AlertStore(db, clock)
    fingerprint = extract_fingerprint(payload)

    if looks_resolved(payload):
        closed = alerts.resolve_by_fingerprint(fingerprint)
        return IngestResult(closed, created=False, resolved=True)

    named = payload.get("policy") or default_policy
    if named:
        policy = PolicyStore(db, clock).get(named)
    else:
        policy = None

    existing = alerts.active(fingerprint)
    if existing is not None:
        # A repeat of an alert we are already working on must not start a new
        # escalation, so hand back the original.
        return IngestResult(existing, created=False, resolved=False)

    alert = alerts.create(
        fingerprint=fingerprint,
        title=extract_title(payload, fingerprint),
        body=extract_body(payload),
        policy_id=policy.id if policy is not None else None,
    )
    if alert.policy_id is not None:
        alert = (engine or EscalationEngine(db, clock=clock)).fire(alert)
    return IngestResult(alert, created=True, resolved=False)
