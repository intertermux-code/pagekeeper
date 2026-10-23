from __future__ import annotations

import json
import urllib.error

import pytest

from helpers import RecordingOpener
from pagekeeper.errors import UsageError
from pagekeeper.models import PageMessage
from pagekeeper.notifiers import (
    DEFAULT_NTFY_BASE,
    Notifier,
    ntfy_destination,
    ntfy_request,
    webhook_request,
)

MESSAGE = PageMessage(
    title="[ops] disk full",
    text="disk full on db-1\n\nacknowledge with: pagekeeper ack 7",
    priority=4,
    fields={"alert_id": 7, "fingerprint": "abc", "step": 1},
)


def test_ntfy_request_shape():
    request = ntfy_request("alice-phone", MESSAGE, base="https://ntfy.example")

    assert request.full_url == "https://ntfy.example/alice-phone"
    assert request.get_method() == "POST"
    assert request.headers["Title"] == "[ops] disk full"
    assert request.headers["Priority"] == "4"
    assert request.headers["Tags"] == "rotating_light"
    assert request.data.decode("utf-8").startswith("disk full on db-1")


def test_ntfy_accepts_a_full_server_url():
    request = ntfy_request("https://ntfy.internal/my-team", MESSAGE)
    assert request.full_url == "https://ntfy.internal/my-team"


def test_ntfy_defaults_to_the_public_server():
    assert ntfy_destination("team-alerts") == f"{DEFAULT_NTFY_BASE}/team-alerts"


def test_ntfy_rejects_things_that_are_not_topics():
    for bad in ("", "has spaces", "../../etc/passwd", "topic?query=1", "a" * 200):
        with pytest.raises(UsageError):
            ntfy_destination(bad)


def test_webhook_request_is_json_with_a_text_field():
    request = webhook_request("https://hooks.test/services/T0/B0/XYZ", MESSAGE)

    assert request.full_url == "https://hooks.test/services/T0/B0/XYZ"
    assert request.headers["Content-type"] == "application/json"
    payload = json.loads(request.data)
    # Slack incoming webhooks only need `text`; the rest is for generic sinks.
    assert payload["text"] == MESSAGE.text
    assert payload["alert_id"] == 7
    assert payload["fingerprint"] == "abc"
    assert payload["step"] == 1


def test_webhook_rejects_non_http_destinations():
    for bad in ("file:///etc/passwd", "not a url", "", "javascript:alert(1)"):
        with pytest.raises(UsageError):
            webhook_request(bad, MESSAGE)


def test_header_values_are_fenced_to_one_safe_line():
    request = ntfy_request("ops", PageMessage(
        title="boom\r\nX-Injected: yes 😅", text="body", priority=3
    ))
    title = request.headers["Title"]
    assert "\r" not in title and "\n" not in title
    assert "X-Injected" in title  # the text survives, the newline does not


def test_priority_is_clamped_to_what_ntfy_understands():
    assert ntfy_request("ops", PageMessage(title="t", text="b", priority=99)).headers["Priority"] == "5"
    assert ntfy_request("ops", PageMessage(title="t", text="b", priority=0)).headers["Priority"] == "1"
    assert "Tags" not in ntfy_request("ops", PageMessage(title="t", text="b", priority=2)).headers


def test_notifier_reports_success():
    opener = RecordingOpener(status=204)
    ok, detail = Notifier(opener=opener).send("ntfy", "ops", MESSAGE)

    assert (ok, detail) == (True, "HTTP 204")
    assert opener.urls == ["https://ntfy.sh/ops"]


def test_notifier_reports_http_failures():
    ok, detail = Notifier(opener=RecordingOpener(status=500)).send("webhook", "https://hooks.test/x", MESSAGE)
    assert (ok, detail) == (False, "HTTP 500")


def test_notifier_reports_transport_failures():
    opener = RecordingOpener(error=urllib.error.URLError("no route to host"))
    ok, detail = Notifier(opener=opener).send("webhook", "https://hooks.test/x", MESSAGE)
    assert ok is False
    assert "no route to host" in detail


def test_notifier_rejects_bad_destinations_without_calling_the_network():
    opener = RecordingOpener()
    ok, detail = Notifier(opener=opener).send("webhook", "ftp://hooks.test/x", MESSAGE)
    assert ok is False
    assert opener.requests == []

    ok, detail = Notifier(opener=opener).send("carrier-pigeon", "coop", MESSAGE)
    assert ok is False
    assert "unknown channel" in detail
