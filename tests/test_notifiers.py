from __future__ import annotations

import json
import urllib.error

import pytest

from helpers import RecordingOpener
from pagekeeper.engine import EscalationEngine
from pagekeeper.errors import UsageError
from pagekeeper.models import PageMessage, Target
from pagekeeper.notifiers import (
    ACK_ACTION_ID,
    DEFAULT_NTFY_BASE,
    DISCORD_COLOR_URGENT,
    Notifier,
    discord_payload,
    discord_request,
    ntfy_destination,
    ntfy_request,
    slack_payload,
    slack_request,
    webhook_request,
)

MESSAGE = PageMessage(
    title="[ops] disk full",
    text="disk full on db-1\n\nacknowledge with: pagekeeper ack 7",
    priority=4,
    fields={"alert_id": 7, "fingerprint": "abc", "step": 1},
)

FULL = PageMessage(
    title="[ops] disk full",
    text="disk full on db-1\n\nacknowledge with: pagekeeper ack 7",
    priority=4,
    fields={
        "alert_id": 7,
        "fingerprint": "abc",
        "policy": "ops",
        "step": 2,
        "steps_total": 3,
        "state": "open",
    },
)

SLACK_URL = "https://hooks.slack.test/services/T00/B00/XXXX"
DISCORD_URL = "https://discord.test/api/webhooks/123/token"


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


def button(block: dict) -> dict:
    assert block["type"] == "actions"
    return block["elements"][0]


def test_slack_request_is_block_kit_with_an_ack_button():
    request = slack_request(SLACK_URL, FULL)

    assert request.get_method() == "POST"
    assert request.headers["Content-type"] == "application/json"
    payload = json.loads(request.data)
    header, detail, fields, actions = payload["blocks"]

    assert header["text"]["text"] == "[ops] disk full"
    assert "disk full on db-1" in detail["text"]["text"]
    assert [field["text"] for field in fields["fields"]] == [
        "*alert*\n7",
        "*fingerprint*\nabc",
        "*policy*\nops",
        "*step*\n2",
        "*state*\nopen",
    ]
    assert button(actions)["action_id"] == ACK_ACTION_ID
    # The value is all the interactive endpoint needs to find this alert again.
    assert button(actions)["value"] == "7"
    assert payload["text"] == "[ops] disk full"


def test_slack_leaves_out_the_button_when_there_is_nothing_to_ack():
    blocks = slack_payload(PageMessage(title="t", text="b", fields={"policy": "ops"}))["blocks"]

    assert [block["type"] for block in blocks] == ["header", "section", "section"]


def test_slack_fills_in_metadata_it_was_never_given():
    fields = slack_payload(MESSAGE)["blocks"][2]["fields"]

    assert [field["text"] for field in fields] == [
        "*alert*\n7",
        "*fingerprint*\nabc",
        "*policy*\n-",
        "*step*\n1",
        "*state*\n-",
    ]


def test_slack_keeps_every_string_inside_what_the_platform_accepts():
    payload = slack_payload(
        PageMessage(title="boom\n\n" + "x" * 400, text="y" * 5000, fields={})
    )

    header = payload["blocks"][0]["text"]["text"]
    assert "\n" not in header and len(header) <= 150
    assert len(payload["blocks"][1]["text"]["text"]) <= 2900
    assert payload["blocks"][1]["text"]["text"].endswith("…")


def test_discord_request_posts_an_embed_with_the_alert_fields():
    payload = json.loads(discord_request(DISCORD_URL, FULL).data)

    (embed,) = payload["embeds"]
    assert embed["title"] == "[ops] disk full"
    assert "disk full on db-1" in embed["description"]
    assert [(field["name"], field["value"]) for field in embed["fields"]] == [
        ("alert", "7"),
        ("fingerprint", "abc"),
        ("policy", "ops"),
        ("step", "2"),
        ("state", "open"),
    ]
    assert embed["color"] == DISCORD_COLOR_URGENT


def test_discord_colours_a_first_step_less_alarmingly():
    embed = discord_payload(PageMessage(title="t", text="b", priority=3))["embeds"][0]

    assert embed["color"] != DISCORD_COLOR_URGENT


def test_chat_channels_reject_destinations_that_are_not_urls():
    for builder in (slack_request, discord_request):
        for bad in ("file:///etc/passwd", "not a url", "", "javascript:alert(1)"):
            with pytest.raises(UsageError):
                builder(bad, FULL)


def test_notifier_sends_slack_and_discord_bodies():
    opener = RecordingOpener()

    assert Notifier(opener=opener).send("slack", SLACK_URL, FULL)[0] is True
    assert "blocks" in json.loads(opener.bodies[0])

    assert Notifier(opener=opener).send("discord", DISCORD_URL, FULL)[0] is True
    assert "embeds" in json.loads(opener.bodies[1])


def test_chat_failures_come_back_as_a_result_not_an_exception():
    opener = RecordingOpener(error=urllib.error.URLError("connection refused"))

    for channel in ("slack", "discord"):
        ok, detail = Notifier(opener=opener).send(channel, SLACK_URL, FULL)
        assert ok is False
        assert "connection refused" in detail


def test_slack_and_discord_targets_page_through_a_policy_step(db, clock, policies, sink):
    """The real HTTP client, the engine's own routing, endpoints on loopback."""
    policy = policies.add(
        "ops",
        [
            (5, [Target("slack", f"{sink.url}/services/T00/B00/XXXX"),
                 Target("discord", f"{sink.url}/api/webhooks/123/token")]),
            (10, [Target("ntfy", "later-board")]),
        ],
    )
    engine = EscalationEngine(db, notifier=Notifier(), clock=clock)
    alert = engine.fire(
        engine.alerts.create(fingerprint="db1", title="disk full", policy_id=policy.id)
    )

    slack, discord = sink.payloads
    assert button(slack["blocks"][-1])["action_id"] == ACK_ACTION_ID
    assert button(slack["blocks"][-1])["value"] == str(alert.id)
    assert [field["name"] for field in discord["embeds"][0]["fields"]] == [
        "alert",
        "fingerprint",
        "policy",
        "step",
        "state",
    ]
    pages = engine.alerts.pages(alert.id)
    assert [(page["kind"], page["ok"]) for page in pages] == [("slack", 1), ("discord", 1)]
    assert alert.due_at == clock.at + 5 * 60


def test_a_later_step_reaches_the_new_channels_too(db, clock, policies, sink):
    policy = policies.add(
        "ops",
        [(5, [Target("ntfy", "oncall-board")]), (10, [Target("slack", f"{sink.url}/hooks")])],
    )
    engine = EscalationEngine(db, notifier=Notifier(ntfy_base=sink.url), clock=clock)
    alert = engine.fire(
        engine.alerts.create(fingerprint="db1", title="disk full", policy_id=policy.id)
    )

    clock.advance(minutes=5)
    assert engine.tick(now=clock.at) == 1

    escalated = engine.alerts.get(alert.id)
    assert (escalated.step, escalated.due_at) == (1, None)
    assert [path for path, _headers, _body in sink.requests] == [
        "/oncall-board",
        "/hooks",
    ]
    # The first call is ntfy, whose body is plain text, so pick the Slack one.
    assert "blocks" in json.loads(sink.requests[1][2])


def test_a_dead_endpoint_is_recorded_and_the_clock_keeps_running(
    db, clock, policies, sink, caplog
):
    sink.close()
    policy = policies.add(
        "ops",
        [(5, [Target("slack", f"{sink.url}/dead")]), (10, [Target("ntfy", "board")])],
    )
    engine = EscalationEngine(db, notifier=Notifier(), clock=clock)
    alert = engine.fire(
        engine.alerts.create(fingerprint="db1", title="disk full", policy_id=policy.id)
    )

    page = engine.alerts.pages(alert.id)[0]
    assert (page["ok"], page["kind"]) == (0, "slack")
    assert "URLError" in page["detail"]
    assert alert.due_at == clock.at + 5 * 60
    assert "slack:" in caplog.text
