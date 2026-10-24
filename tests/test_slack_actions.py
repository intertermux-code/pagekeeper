from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

from helpers import Rota, seed_rota
from pagekeeper.models import Target
from pagekeeper.server import (
    SLACK_FRESHNESS_SECONDS,
    SLACK_SIGNATURE_HEADER,
    SLACK_TIMESTAMP_HEADER,
    App,
    PagekeeperServer,
    slack_signature,
)

SECRET = "test-signing-secret"
ACTIONS = "/slack/actions"


@pytest.fixture
def rota(db, clock) -> Rota:
    return seed_rota(db, clock)


@pytest.fixture
def alert(db, engine, policies, rota):
    """An alert paged once and armed to escalate in five minutes."""
    policy = policies.add(
        "ops",
        [
            (5, [Target("schedule", str(rota.schedule.id))]),
            (10, [Target("ntfy", "board")]),
        ],
    )
    return engine.fire(
        engine.alerts.create(fingerprint="db1", title="disk full", policy_id=policy.id)
    )


@pytest.fixture
def serving(db, engine):
    """Start the server with, or without, a Slack signing secret."""
    httpds = []

    def start(secret):
        app = App(db, engine, default_policy="ops", slack_signing_secret=secret)
        httpd = PagekeeperServer(("127.0.0.1", 0), app)
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        httpds.append((httpd, thread))
        return f"http://127.0.0.1:{httpd.server_port}"

    yield start

    for httpd, thread in httpds:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def click(alert_id: int, action_id: str = "ack") -> dict:
    """The interaction Slack sends when somebody presses our Ack button."""
    return {
        "type": "block_actions",
        "user": {"id": "U0ALICE", "username": "alice"},
        "trigger_id": "T0ABC",
        "actions": [
            {
                "type": "button",
                "action_id": action_id,
                "value": str(alert_id),
                "text": {"type": "plain_text", "text": "Acknowledge"},
            }
        ],
    }


def send(url: str, body: bytes, *, secret=SECRET, timestamp=None):
    """POST the way Slack does: form-encoded, dated and signed with the secret."""
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if timestamp is not None:
        stamp = str(timestamp)
        headers[SLACK_TIMESTAMP_HEADER] = stamp
        if secret is not None:
            headers[SLACK_SIGNATURE_HEADER] = slack_signature(secret, stamp, body)
    request = urllib.request.Request(url + ACTIONS, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def form(**fields) -> bytes:
    return urlencode(fields).encode("utf-8")


def click_body(alert_id: int, action_id: str = "ack") -> bytes:
    return form(payload=json.dumps(click(alert_id, action_id)))


def test_a_signed_ack_click_stops_the_escalation(serving, clock, engine, alert):
    url = serving(SECRET)

    status, body = send(url, click_body(alert.id), timestamp=clock.at)

    assert status == 200
    assert body == {"ok": True, "alert_id": alert.id, "state": "acked"}
    acked = engine.alerts.get(alert.id)
    assert (acked.state, acked.due_at, acked.acked_at) == ("acked", None, clock.at)

    # Nothing further is owed to this alert, deadline or not.
    clock.advance(minutes=30)
    assert engine.tick(now=clock.at) == 0
    assert len(engine.notifier.opener.requests) == 1


def test_the_status_page_shows_the_click(serving, clock, engine, alert, db):
    url = serving(SECRET)
    send(url, click_body(alert.id), timestamp=clock.at)

    with urllib.request.urlopen(url + "/", timeout=5) as response:
        page = response.read().decode("utf-8")

    assert "acked" in page
    assert "stopped (acked)" in page


def test_a_wrong_signature_changes_nothing(serving, clock, engine, alert):
    url = serving(SECRET)

    status, body = send(
        url, click_body(alert.id), secret="someone-elses-secret", timestamp=clock.at
    )

    assert status == 401
    assert "signature" in body["error"]
    after = engine.alerts.get(alert.id)
    assert (after.state, after.due_at) == ("open", alert.due_at)


def test_a_stale_timestamp_is_refused(serving, clock, engine, alert):
    url = serving(SECRET)
    stale = clock.at - SLACK_FRESHNESS_SECONDS - 1

    status, body = send(url, click_body(alert.id), timestamp=stale)

    assert status == 401
    assert "replay window" in body["error"]
    assert engine.alerts.get(alert.id).state == "open"


def test_a_recent_click_survives_the_hop(serving, clock, engine, alert):
    """Inside the window is inside the window, even at four minutes."""
    url = serving(SECRET)

    status, body = send(
        url, click_body(alert.id), timestamp=clock.at - 4 * 60
    )

    assert status == 200
    assert engine.alerts.get(alert.id).state == "acked"


def test_requests_without_slack_headers_are_refused(serving, clock, engine, alert):
    url = serving(SECRET)

    assert send(url, click_body(alert.id))[0] == 401
    assert send(url, click_body(alert.id), timestamp="not-a-number")[0] == 401

    signature = slack_signature(SECRET, str(clock.at), click_body(alert.id))
    body = form(payload=json.dumps(click(alert.id)))
    request = urllib.request.Request(
        url + ACTIONS,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 SLACK_SIGNATURE_HEADER: signature},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=5)
    assert refused.value.code == 401
    assert engine.alerts.get(alert.id).state == "open"


def test_a_signature_that_is_not_plain_text_is_simply_wrong(
    serving, clock, engine, alert
):
    """Headers arrive from anywhere; a weird one must be a 401, not a 500."""
    url = serving(SECRET)
    request = urllib.request.Request(
        url + ACTIONS,
        data=click_body(alert.id),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            SLACK_TIMESTAMP_HEADER: str(clock.at),
            SLACK_SIGNATURE_HEADER: "v0=caf\u00e9",
        },
        method="POST",
    )

    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=5)

    assert refused.value.code == 401
    assert engine.alerts.get(alert.id).state == "open"


def test_unsigned_clicks_are_refused_when_no_secret_is_configured(
    serving, clock, engine, alert
):
    """Perfectly signed by a secret we never set still gets nothing."""
    url = serving(None)

    status, body = send(url, click_body(alert.id), timestamp=clock.at)

    assert status == 401
    assert "signing secret" in body["error"]
    assert engine.alerts.get(alert.id).state == "open"


def test_a_click_for_an_alert_we_do_not_have_is_a_404(serving, clock, engine, alert):
    url = serving(SECRET)

    status, body = send(url, click_body(4242), timestamp=clock.at)

    assert status == 404
    assert "no such alert" in body["error"]
    assert engine.alerts.get(alert.id).state == "open"


def test_a_payload_we_cannot_use_is_a_400(serving, clock, engine, alert):
    url = serving(SECRET)
    unusable = {
        "no payload field": form(nothing="at all"),
        "payload is not JSON": form(payload="this is not json"),
        "payload is not an object": form(payload=json.dumps(["a", "list"])),
        "not an interaction": form(payload=json.dumps({"type": "shortcut"})),
        "no actions": form(payload=json.dumps({"type": "block_actions"})),
        "someone else's button": click_body(alert.id, action_id="snooze"),
        "value is not an id": form(
            payload=json.dumps(
                {"type": "block_actions", "actions": [{"action_id": "ack", "value": "abc"}]}
            )
        ),
    }

    for reason, body in unusable.items():
        status, reply = send(url, body, timestamp=clock.at)
        assert status == 400, reason
        assert "error" in reply, reason

    assert engine.alerts.get(alert.id).state == "open"
    assert engine.alerts.get(alert.id).due_at == alert.due_at


def test_slack_actions_is_not_a_webhook(serving, clock, engine, alert):
    """The ingress endpoints keep doing exactly what they did before."""
    url = serving(SECRET)

    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(url + "/slack/actions", data=b"{}", timeout=5)
    assert refused.value.code == 401

    created = urllib.request.Request(
        url + "/hook",
        data=json.dumps({"fingerprint": "other", "title": "queue backlog"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(created, timeout=5) as response:
        assert response.status == 201
    assert len(engine.alerts.open()) == 2


def test_only_post_reaches_the_endpoint(serving, clock):
    url = serving(SECRET)

    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(url + ACTIONS, timeout=5)
    assert refused.value.code == 404
