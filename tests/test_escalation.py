from __future__ import annotations

import urllib.error

import pytest

from helpers import Rota, RecordingOpener, minutes, seed_rota
from pagekeeper.engine import EscalationEngine
from pagekeeper.models import Target
from pagekeeper.notifiers import Notifier

ALICE = "https://ntfy.test/alice-phone"
BOB = "https://ntfy.test/bob-phone"


@pytest.fixture
def rota(db, clock) -> Rota:
    return seed_rota(db, clock)


def paged_urls(engine) -> list[str]:
    return [request.full_url for request in engine.notifier.opener.requests]


def raise_alert(engine, policy_id, title="disk full", fingerprint="fp-1"):
    alert = engine.alerts.create(
        fingerprint=fingerprint, title=title, body="df says 100% used",
        policy_id=policy_id,
    )
    return engine.fire(alert)


def on_call(schedule) -> list[Target]:
    return [Target("schedule", str(schedule.id))]


def test_firing_pages_step_one_and_arms_the_deadline(engine, rota, policies):
    policy = policies.add(
        "ops", [(5, on_call(rota.schedule)), (10, [Target("user", rota.id_of("bob"))])]
    )
    alert = raise_alert(engine, policy.id)

    assert alert.step == 0
    assert alert.due_at == minutes(5)
    assert paged_urls(engine) == [ALICE]

    pages = engine.alerts.pages(alert.id)
    assert len(pages) == 1
    assert (pages[0]["step"], pages[0]["kind"]) == (0, "ntfy")
    assert (pages[0]["destination"], pages[0]["ok"]) == ("alice-phone", 1)


def test_single_step_policy_pages_once_and_stops(engine, rota, policies):
    policy = policies.add("ops", [(5, on_call(rota.schedule))])
    alert = raise_alert(engine, policy.id)

    # Nobody is further down the list, so the alert stays open without a
    # deadline instead of escalating into thin air.
    assert (alert.step, alert.due_at) == (0, None)
    engine.clock.advance(minutes=600)
    assert engine.tick() == 0
    assert paged_urls(engine) == [ALICE]


def test_tick_before_the_deadline_stays_quiet(engine, rota, policies):
    policy = policies.add(
        "ops", [(5, on_call(rota.schedule)), (5, [Target("user", rota.id_of("bob"))])]
    )
    raise_alert(engine, policy.id)

    engine.clock.advance(minutes=4)
    assert engine.tick() == 0
    assert len(paged_urls(engine)) == 1


def test_unacknowledged_alert_walks_every_step(engine, rota, policies):
    policy = policies.add(
        "ops",
        [
            (5, on_call(rota.schedule)),
            (10, [Target("user", rota.id_of("bob"))]),
            (0, [Target("webhook", "https://hooks.test/board")]),
        ],
    )
    alert = raise_alert(engine, policy.id)
    assert alert.step == 0

    engine.clock.advance(minutes=5)
    assert engine.tick() == 1
    alert = engine.alerts.get(alert.id)
    assert (alert.step, alert.due_at) == (1, minutes(15))

    engine.clock.advance(minutes=10)
    assert engine.tick() == 1
    alert = engine.alerts.get(alert.id)
    assert alert.step == 2
    # The last step has nobody further to wake, so no deadline is armed.
    assert alert.due_at is None

    engine.clock.advance(minutes=180)
    assert engine.tick() == 0
    assert engine.alerts.get(alert.id).state == "open"
    assert paged_urls(engine) == [ALICE, BOB, "https://hooks.test/board"]


def test_ack_before_the_delay_stops_escalation(engine, rota, policies):
    policy = policies.add(
        "ops",
        [(5, on_call(rota.schedule)), (5, [Target("user", rota.id_of("bob"))])],
    )
    alert = raise_alert(engine, policy.id)

    engine.clock.advance(minutes=3)
    acked = engine.ack(alert.id)
    assert (acked.state, acked.due_at) == ("acked", None)

    engine.clock.advance(minutes=30)
    assert engine.tick() == 0
    assert paged_urls(engine) == [ALICE]


def test_paging_follows_the_rotation_over_time(engine, rota, policies):
    policy = policies.add(
        "ops", [(60, on_call(rota.schedule)), (5, on_call(rota.schedule))]
    )
    raise_alert(engine, policy.id)

    engine.clock.advance(minutes=60)
    engine.tick()
    assert paged_urls(engine) == [ALICE, BOB]


def test_priority_rises_with_each_step(engine, rota, policies):
    policy = policies.add(
        "ops", [(5, on_call(rota.schedule)), (5, [Target("user", rota.id_of("bob"))])]
    )
    raise_alert(engine, policy.id)
    engine.clock.advance(minutes=5)
    engine.tick()

    assert [
        request.headers.get("Priority") for request in engine.notifier.opener.requests
    ] == ["3", "4"]


def test_page_text_tells_the_recipient_how_to_ack(engine, rota, policies):
    policy = policies.add("ops", [(5, on_call(rota.schedule))])
    alert = raise_alert(engine, policy.id)

    body = engine.notifier.opener.requests[0].data.decode("utf-8")
    assert "disk full" in body
    assert f"pagekeeper ack {alert.id}" in body
    assert "step 1 of 1" in body
    assert "this is the last step" in body


def test_every_target_in_a_step_is_paged(engine, rota, policies):
    policy = policies.add(
        "ops",
        [
            (
                5,
                on_call(rota.schedule)
                + [
                    Target("user", rota.id_of("alice")),
                    Target("webhook", "https://hooks.test/board"),
                ],
            )
        ],
    )
    raise_alert(engine, policy.id)

    assert paged_urls(engine) == [ALICE, ALICE, "https://hooks.test/board"]


def test_failed_page_is_recorded_and_timing_continues(db, clock, policies):
    failing = RecordingOpener(error=urllib.error.URLError("connection refused"))
    engine = EscalationEngine(
        db,
        notifier=Notifier(ntfy_base="https://ntfy.test", opener=failing),
        clock=clock,
    )
    rota = seed_rota(db, clock)
    policy = policies.add(
        "ops", [(5, on_call(rota.schedule)), (5, on_call(rota.schedule))]
    )
    alert = raise_alert(engine, policy.id)

    page = engine.alerts.pages(alert.id)[0]
    assert page["ok"] == 0
    assert "connection refused" in page["detail"]

    engine.clock.advance(minutes=5)
    assert engine.tick() == 1
    assert len(failing.requests) == 2


def test_unresolvable_target_shows_up_as_a_failed_page(engine, rota, policies):
    policy = policies.add("ops", [(5, [Target("user", "nobody")])])
    alert = raise_alert(engine, policy.id)

    pages = engine.alerts.pages(alert.id)
    assert len(pages) == 1
    assert pages[0]["ok"] == 0
    assert "no such user" in pages[0]["detail"]


def test_user_without_contact_details_cannot_be_paged(engine, rota, policies):
    quiet = rota.users.add("quiet")
    policy = policies.add("ops", [(5, [Target("user", str(quiet.id))])])
    alert = raise_alert(engine, policy.id)

    pages = engine.alerts.pages(alert.id)
    assert len(pages) == 1
    assert pages[0]["ok"] == 0
    assert "no contact" in pages[0]["detail"]


def test_alert_without_policy_is_recorded_but_never_escalates(engine):
    alert = raise_alert(engine, None)

    assert (alert.state, alert.due_at) == ("open", None)
    engine.clock.advance(minutes=600)
    assert engine.tick() == 0
    assert engine.notifier.opener.requests == []


def test_alerts_escalate_independently(engine, rota, policies):
    policy = policies.add(
        "ops", [(5, on_call(rota.schedule)), (5, [Target("user", rota.id_of("bob"))])]
    )
    first = raise_alert(engine, policy.id, fingerprint="fp-1")
    second = raise_alert(engine, policy.id, title="queue depth", fingerprint="fp-2")

    engine.ack(first.id)
    engine.clock.advance(minutes=5)
    assert engine.tick() == 1

    assert engine.alerts.get(second.id).step == 1
    assert engine.alerts.get(first.id).step == 0
    paged = paged_urls(engine)
    assert paged.count(BOB) == 1
    assert paged.count(ALICE) == 2
