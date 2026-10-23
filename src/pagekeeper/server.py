"""HTTP server: webhook ingress, status page and the escalation ticker."""

from __future__ import annotations

import html
import json
import logging
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from .alerts import extract_fingerprint
from .clock import current, format_time
from .db import Database
from .engine import EscalationEngine
from .errors import PagekeeperError
from .ingest import ingest
from .models import Alert
from .schedules import OnCall

log = logging.getLogger("pagekeeper")

MAX_BODY_BYTES = 256 * 1024
REFRESH_SECONDS = 30


@dataclass(frozen=True)
class OnCallRow:
    schedule: str
    user: str
    source: str
    until: str


@dataclass(frozen=True)
class AlertRow:
    alert_id: int
    title: str
    fingerprint: str
    policy: str
    step: str
    state: str
    escalates: str
    raised: str


class App:
    """Shared state for request handlers."""

    def __init__(
        self,
        db: Database,
        engine: EscalationEngine,
        *,
        default_policy: Optional[str] = None,
    ) -> None:
        self.db = db
        self.engine = engine
        self.default_policy = default_policy

    def handle_webhook(
        self, payload: Mapping[str, Any], policy: Optional[str] = None
    ):
        chosen = policy if policy is not None else self.default_policy
        result = ingest(
            self.db,
            payload,
            engine=self.engine,
            default_policy=chosen,
            clock=self.engine.clock,
        )
        alert = result.alert
        return {
            "alert_id": alert.id if alert is not None else None,
            "fingerprint": alert.fingerprint if alert is not None else extract_fingerprint(payload),
            "state": alert.state if alert is not None else "none",
            "created": result.created,
            "deduplicated": result.deduplicated,
            "resolved": result.resolved,
        }

    def on_call_rows(self, now: int) -> list[OnCallRow]:
        rows: list[OnCallRow] = []
        for schedule in self.engine.schedules.list_all():
            try:
                entry: OnCall = self.engine.schedules.on_call(schedule.id, now=now)[0]
            except PagekeeperError as exc:
                rows.append(OnCallRow(schedule.name, str(exc), "error", "-"))
                continue
            rows.append(
                OnCallRow(
                    schedule=schedule.name,
                    user=entry.user.name,
                    source=entry.source,
                    until=format_time(entry.until),
                )
            )
        return rows

    def alert_rows(self, now: int) -> list[AlertRow]:
        names: dict[Optional[int], str] = {}
        rows: list[AlertRow] = []
        for alert in self.engine.alerts.open():
            if alert.policy_id not in names:
                policy = self.engine.policies.find(alert.policy_id)
                names[alert.policy_id] = policy.name if policy else "-"
            rows.append(
                AlertRow(
                    alert_id=alert.id,
                    title=alert.title,
                    fingerprint=alert.fingerprint,
                    policy=names[alert.policy_id],
                    step=("-" if alert.step < 0 else f"{alert.step + 1}"),
                    state=alert.state,
                    escalates=_escalates(alert, now),
                    raised=format_time(alert.created_at),
                )
            )
        return rows

    def status_html(self, now: Optional[int] = None) -> str:
        at = current(self.engine.clock) if now is None else now
        return render_status(at, self.on_call_rows(at), self.alert_rows(at))


def _escalates(alert: Alert, now: int) -> str:
    if alert.state != "open":
        return "stopped (acked)" if alert.state == "acked" else "-"
    if alert.due_at is None:
        return "last step reached"
    minutes = max(0, (alert.due_at - now) // 60)
    return f"in {minutes} min"


def _query_policy(path: str) -> Optional[str]:
    values = parse_qs(urlparse(path).query).get("policy")
    return values[0] if values else None


def _cells(*values: Any) -> str:
    return "".join(f"<td>{html.escape(str(value))}</td>" for value in values)


def _row(*values: Any) -> str:
    return "<tr>" + _cells(*values) + "</tr>"


def render_status(
    now: int, on_calls: list[OnCallRow], alerts: list[AlertRow]
) -> str:
    if on_calls:
        schedule_rows = "\n".join(
            _row(row.schedule, row.user, row.source, row.until) for row in on_calls
        )
    else:
        schedule_rows = '<tr><td colspan="4" class="empty">No schedules yet.</td></tr>'

    if alerts:
        alert_rows = "\n".join(
            "<tr>"
            + _cells(
                row.alert_id,
                row.title,
                row.fingerprint,
                row.policy,
                row.step,
            )
            + f'<td class="state-{html.escape(row.state)}">{html.escape(row.state)}</td>'
            + _cells(row.escalates, row.raised)
            + "</tr>"
            for row in alerts
        )
    else:
        alert_rows = (
            '<tr><td colspan="8" class="empty">Nothing is paging right now.</td></tr>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>pagekeeper status</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 60rem;
         color: #1a1a1a; }}
  h1 {{ font-size: 1.3rem; margin-bottom: .2rem; }}
  p.stamp {{ color: #666; margin-top: 0; font-size: .85rem; }}
  h2 {{ font-size: 1rem; margin-top: 2rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
  th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #ddd; }}
  th {{ background: #f5f5f5; font-weight: 600; }}
  code {{ font-family: ui-monospace, monospace; font-size: .85rem; }}
  .empty {{ color: #666; }}
  .state-open {{ color: #b00000; font-weight: 600; }}
  .state-acked {{ color: #1565c0; }}
</style>
</head>
<body>
<h1>pagekeeper status</h1>
<p class="stamp">{html.escape(format_time(now))}</p>

<h2>On call</h2>
<table>
<thead><tr><th>Schedule</th><th>Who</th><th>Source</th><th>Until</th></tr></thead>
<tbody>
{schedule_rows}
</tbody>
</table>

<h2>Open alerts</h2>
<table>
<thead><tr><th>Id</th><th>Title</th><th>Fingerprint</th><th>Policy</th><th>Step</th>
<th>State</th><th>Escalates</th><th>Raised</th></tr></thead>
<tbody>
{alert_rows}
</tbody>
</table>

<p class="stamp">Acknowledge from the CLI: <code>pagekeeper ack &lt;id&gt;</code></p>
</body>
</html>
"""


class IngressHandler(BaseHTTPRequestHandler):
    server: "PagekeeperServer"

    def do_GET(self) -> None:
        path = self._path()
        if path == "/":
            body = self.server.app.status_html().encode("utf-8")
            self._respond(200, body, "text/html; charset=utf-8")
        elif path == "/healthz":
            self._respond(200, b"ok\n", "text/plain; charset=utf-8")
        else:
            self._error(404, "not found")

    def do_POST(self) -> None:
        path = self._path()
        if path not in ("/hook", "/webhook"):
            self._error(404, "use POST /hook")
            return

        try:
            payload = self._read_json()
        except ValueError as exc:
            self._error(400, str(exc))
            return

        try:
            result = self.server.app.handle_webhook(payload, policy=_query_policy(self.path))
        except PagekeeperError as exc:
            self._error(400, str(exc))
            return
        except Exception:  # a broken database must not eat the alert silently
            log.exception("webhook handling failed")
            self._error(500, "internal error")
            return

        status = 201 if result["created"] else 200
        self._respond(
            status,
            json.dumps(result, sort_keys=True).encode("utf-8") + b"\n",
            "application/json",
        )

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def _read_json(self) -> Mapping[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length must be a number") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError(f"body must be between 0 and {MAX_BODY_BYTES} bytes")
        body = self.rfile.read(length)
        try:
            payload = json.loads(body or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        return payload

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8") + b"\n"
        self._respond(status, body, "application/json")


class PagekeeperServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: App) -> None:
        self.app = app
        super().__init__(address, IngressHandler)


class Ticker(threading.Thread):
    """Drives the escalation state machine while the server serves requests."""

    def __init__(self, engine: EscalationEngine, interval: float) -> None:
        super().__init__(name="pagekeeper-ticker", daemon=True)
        self.engine = engine
        self.interval = interval
        self._finished = threading.Event()

    def run(self) -> None:
        while not self._finished.wait(self.interval):
            try:
                self.engine.tick()
            except Exception:
                log.exception("escalation tick failed")

    def stop(self) -> None:
        self._finished.set()


def serve(
    db: Database,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    tick_seconds: float = 15.0,
    default_policy: Optional[str] = None,
    engine: Optional[EscalationEngine] = None,
) -> None:
    engine = engine if engine is not None else EscalationEngine(db)
    app = App(db, engine, default_policy=default_policy)
    httpd = PagekeeperServer((host, port), app)
    ticker = Ticker(engine, tick_seconds)
    ticker.start()
    print(
        f"pagekeeper serving http://{host}:{httpd.server_port} "
        f"(database {db.path}, ticking every {tick_seconds:g}s)",
        file=sys.stderr,
        flush=True,
    )
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("shutting down", file=sys.stderr, flush=True)
    finally:
        ticker.stop()
        httpd.shutdown()
        httpd.server_close()
