from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from helpers import Rota, seed_rota
from pagekeeper import cli
from pagekeeper.engine import EscalationEngine
from pagekeeper.models import Target
from pagekeeper.server import App, PagekeeperServer, Ticker


@pytest.fixture
def rota(db, clock) -> Rota:
    return seed_rota(db, clock)


@pytest.fixture
def live(db, engine, rota, policies):
    """A real HTTP server on a random loopback port."""
    policies.add("ops", [(5, [Target("schedule", str(rota.schedule.id))]),
                         (5, [Target("ntfy", "oncall-board")])])
    app = App(db, engine, default_policy="ops")
    httpd = PagekeeperServer(("127.0.0.1", 0), app)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def post(base: str, path: str, payload, raw: bool = False):
    body = payload if raw else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_webhook_creates_an_alert_and_pages_the_on_call_engineer(live, engine):
    status, body = post(live, "/hook", {"fingerprint": "abc", "title": "disk full"})
    payload = json.loads(body)

    assert status == 201
    assert payload["created"] is True
    assert payload["state"] == "open"
    assert engine.alerts.get(payload["alert_id"]).title == "disk full"
    assert [request.full_url for request in engine.notifier.opener.requests] == [
        "https://ntfy.test/alice-phone"
    ]


def test_duplicate_webhook_returns_the_open_alert(live, engine):
    first = json.loads(post(live, "/hook", {"fingerprint": "abc", "title": "disk full"})[1])
    second = json.loads(post(live, "/hook", {"fingerprint": "abc", "title": "disk full"})[1])

    assert second["alert_id"] == first["alert_id"]
    assert second["deduplicated"] is True
    assert len(engine.notifier.opener.requests) == 1


def test_status_page_shows_the_rotation_and_the_alert(live, engine):
    post(live, "/hook", {"fingerprint": "abc", "title": "db-1 disk full"})
    status, page = get(live, "/")

    assert status == 200
    assert "pagekeeper status" in page
    assert "alice" in page
    assert "db-1 disk full" in page
    assert "abc" in page


def test_status_page_escapes_what_a_webhook_sent(live, engine):
    post(live, "/hook", {"fingerprint": "xss", "title": "<script>alert(1)</script>"})
    _, page = get(live, "/")

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_healthz_is_plain(live):
    assert get(live, "/healthz")[0] == 200


def test_internal_failures_become_a_500(live, monkeypatch):
    def explode(self, payload, policy=None):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(App, "handle_webhook", explode)

    status, body = post(live, "/hook", {"fingerprint": "abc"})
    assert status == 500
    assert "internal error" in body


def test_bad_requests_are_refused(live):
    status, body = post(live, "/hook", b"this is not json", raw=True)
    assert status == 400
    assert "not valid JSON" in body

    status, _ = post(live, "/hook", b'["an", "array"]', raw=True)
    assert status == 400

    status, _ = post(live, "/somewhere-else", b"{}", raw=True)
    assert status == 404

    status, _ = get(live, "/nope")
    assert status == 404


def test_policy_can_be_named_per_request(live, engine, policies, rota):
    policies.add("billing", [(5, [Target("ntfy", "billing-team")])])
    status, body = post(live, "/hook?policy=billing", {"fingerprint": "z", "title": "invoices"})
    alert = engine.alerts.get(json.loads(body)["alert_id"])

    assert status == 201
    assert alert.policy_id == policies.get("billing").id
    assert engine.notifier.opener.requests[0].full_url == "https://ntfy.test/billing-team"


def test_unknown_policy_is_a_client_error(live):
    status, body = post(live, "/hook?policy=nope", {"fingerprint": "z"})
    assert status == 400
    assert "no such policy" in body


def test_acknowledging_from_the_cli_clears_the_page(live, engine, db, capsys):
    alert_id = json.loads(post(live, "/hook", {"fingerprint": "abc", "title": "disk full"})[1])["alert_id"]
    capsys.readouterr()

    assert cli.main(["--db", str(db.path), "ack", str(alert_id)]) == 0

    status, page = get(live, "/")
    assert status == 200
    assert "acked" in page
    assert "stopped (acked)" in page


def test_the_ticker_escalates_when_nobody_answers(db, engine, rota, policies):
    policies.add(
        "ops",
        [(5, [Target("schedule", str(rota.schedule.id))]),
         (5, [Target("ntfy", "oncall-board")])],
    )
    alert = engine.alerts.create(
        fingerprint="abc", title="disk full", policy_id=policies.get("ops").id
    )
    engine.fire(alert)

    ticker = Ticker(engine, 0.01)
    ticker.start()
    try:
        engine.clock.advance(minutes=6)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and engine.alerts.get(alert.id).step == 0:
            time.sleep(0.01)
    finally:
        ticker.stop()
        ticker.join(timeout=5)

    escalated = engine.alerts.get(alert.id)
    assert escalated.step == 1
    assert escalated.due_at is None
    assert [request.full_url for request in engine.notifier.opener.requests] == [
        "https://ntfy.test/alice-phone",
        "https://ntfy.test/oncall-board",
    ]
