"""The tick-based escalation state machine.

An alert walks its policy one step at a time.  Each step is paged immediately
and then given `delay_minutes` of grace; if nobody acknowledges before the
deadline, the next step is paged.  The final step has nothing to escalate to,
so the alert stays open without a deadline.
"""

from __future__ import annotations

import logging
from typing import Optional

from .alerts import AlertStore
from .clock import Clock, current, format_time
from .db import Database
from .models import Alert, CHANNELS, PageMessage, Recipient, Step, User
from .notifiers import DEFAULT_NTFY_BASE, Notifier
from .policies import PolicyStore
from .schedules import ScheduleStore
from .errors import PagekeeperError
from .users import UserStore

log = logging.getLogger("pagekeeper")

FIRST_PRIORITY = 3


class EscalationEngine:
    def __init__(
        self,
        db: Database,
        *,
        notifier: Optional[Notifier] = None,
        clock: Optional[Clock] = None,
        ntfy_base: str = DEFAULT_NTFY_BASE,
    ) -> None:
        self.db = db
        self.clock = clock
        self.alerts = AlertStore(db, clock)
        self.policies = PolicyStore(db, clock)
        self.users = UserStore(db, clock)
        self.schedules = ScheduleStore(db, clock)
        self.notifier = notifier if notifier is not None else Notifier(ntfy_base)

    # -- state transitions ------------------------------------------------

    def fire(self, alert: Alert) -> Alert:
        """Page the first step and arm the first deadline."""
        return self._advance(alert, current(self.clock))

    def tick(self, *, now: Optional[int] = None) -> int:
        """Advance every overdue alert.  Returns how many escalated."""
        at = current(self.clock) if now is None else now
        escalations = 0
        for alert in self.alerts.due(now=at):
            advanced = self._advance(alert, at)
            if advanced.step > alert.step:
                escalations += 1
        return escalations

    def ack(self, alert_id: int) -> Alert:
        """Acknowledging always stops escalation, whoever did it."""
        return self.alerts.ack(alert_id)

    def _advance(self, alert: Alert, at: int) -> Alert:
        if alert.state != "open":
            return alert
        steps = self.policies.steps(alert.policy_id) if alert.policy_id is not None else []
        position = alert.step + 1
        if position >= len(steps):
            return self.alerts.update(alert.id, due_at=None)

        step = steps[position]
        self._page(alert, step, len(steps))
        last = position == len(steps) - 1
        due_at = None if last else at + step.delay_minutes * 60
        return self.alerts.update(alert.id, step=position, due_at=due_at)

    # -- notification -----------------------------------------------------

    def _page(self, alert: Alert, step: Step, total: int) -> None:
        message = self.message(alert, step, total)
        for recipient in self.recipients(step):
            ok, detail = self.notifier.send(
                recipient.channel, recipient.destination, message
            )
            self.alerts.record_page(
                alert.id,
                step=step.position,
                kind=recipient.channel,
                destination=recipient.destination,
                ok=ok,
                detail=f"{recipient.label}: {detail}" if not ok else detail,
            )
            if not ok:
                log.warning(
                    "page for alert %s step %s failed: %s %s",
                    alert.id,
                    step.position + 1,
                    recipient.label,
                    detail,
                )

    def recipients(self, step: Step) -> list[Recipient]:
        found: list[Recipient] = []
        for target in step.targets:
            label = f"{target.kind}:{target.value}"
            try:
                if target.kind == "user":
                    user = self.users.get(target.value)
                    self._append_channels(found, user, user.name)
                elif target.kind == "schedule":
                    for on_call in self.schedules.on_call(target.value):
                        user = on_call.user
                        self._append_channels(
                            found, user, f"{on_call.schedule.name}: {user.name}"
                        )
                elif target.kind in CHANNELS:
                    found.append(
                        Recipient(target.kind, target.value, label)  # type: ignore[arg-type]
                    )
                else:
                    raise PagekeeperError(f"unknown target kind {target.kind!r}")
            except PagekeeperError as exc:
                found.append(Recipient("unresolved", target.value, f"{label} ({exc})"))
        return found

    def _append_channels(
        self, out: list[Recipient], user: User, label: str
    ) -> None:
        if user.ntfy_topic:
            out.append(Recipient("ntfy", user.ntfy_topic, f"{label} ntfy"))
        if user.webhook_url:
            out.append(Recipient("webhook", user.webhook_url, f"{label} webhook"))
        if not user.ntfy_topic and not user.webhook_url:
            out.append(
                Recipient("unresolved", f"user:{user.name}", f"{label} has no contact")
            )

    def message(self, alert: Alert, step: Step, total: int) -> PageMessage:
        policy = self.policies.get(alert.policy_id) if alert.policy_id is not None else None
        name = policy.name if policy is not None else "unrouted"
        remaining = total - step.position - 1
        text = "\n".join(
            [
                alert.title,
                "",
                f"alert #{alert.id}  fingerprint {alert.fingerprint}",
                f"policy {name}, step {step.position + 1} of {total}",
                f"raised {format_time(alert.created_at)}",
                (
                    "escalates again in "
                    f"{step.delay_minutes} min"
                    if remaining > 0
                    else "this is the last step"
                ),
                "",
                f"acknowledge with: pagekeeper ack {alert.id}",
                "",
                alert.body,
            ]
        )
        return PageMessage(
            title=f"[{name}] {alert.title}",
            text=text,
            priority=min(5, FIRST_PRIORITY + step.position),
            fields={
                "alert_id": alert.id,
                "fingerprint": alert.fingerprint,
                "policy": name,
                "step": step.position + 1,
                "steps_total": total,
                "state": alert.state,
            },
        )
