from __future__ import annotations

import pytest

from helpers import Rota, minutes, seed_rota
from pagekeeper.alerts import MAX_BODY, extract_body, extract_fingerprint, extract_title
from pagekeeper.errors import NotFound
from pagekeeper.ingest import ingest
from pagekeeper.models import Target


@pytest.fixture
def rota(db, clock) -> Rota:
    return seed_rota(db, clock)


def post(db, engine, payload, **kwargs):
    return ingest(db, payload, engine=engine, clock=engine.clock, **kwargs)


def urls(engine) -> list[str]:
    return [request.full_url for request in engine.notifier.opener.requests]


def on_call(rota) -> list[Target]:
    return [Target("schedule", str(rota.schedule.id))]


def test_two_webhooks_with_the_same_fingerprint_are_one_alert(db, engine, rota, policies):
    policies.add(
        "ops", [(5, on_call(rota)), (5, [Target("ntfy", "oncall-board")])]
    )
    payload = {"fingerprint": "abc", "title": "disk full"}

    first = post(db, engine, payload, default_policy="ops")
    assert first.created is True
    assert first.alert.state == "open"
    assert urls(engine) == ["https://ntfy.test/alice-phone"]

    second = post(db, engine, payload, default_policy="ops")
    assert second.created is False
    assert second.deduplicated is True
    assert second.alert.id == first.alert.id
    # A duplicate must not restart escalation or page anybody twice.
    assert urls(engine) == ["https://ntfy.test/alice-phone"]
    assert second.alert.due_at == minutes(5)


def test_different_fingerprints_are_different_alerts(db, engine):
    first = post(db, engine, {"fingerprint": "abc", "title": "disk full"})
    second = post(db, engine, {"fingerprint": "def", "title": "disk full"})
    assert first.created and second.created
    assert first.alert.id != second.alert.id


def test_alertmanager_style_payload_uses_nested_labels(db, engine):
    payload = {
        "version": "4",
        "status": "firing",
        "labels": {"alertname": "HighErrorRate", "service": "checkout"},
        "annotations": {"description": "5% of requests failing"},
    }
    result = post(db, engine, payload)

    assert result.created is True
    assert result.alert.title == "HighErrorRate"
    assert "5% of requests failing" in result.alert.body
    assert post(db, engine, payload).deduplicated is True


def test_fingerprint_derived_from_identity_keys_is_stable():
    base = {"labels": {"alertname": "DiskFull", "service": "db"}, "value": 1}
    reordered = {"labels": {"service": "db", "alertname": "DiskFull"}, "value": 99}
    assert extract_fingerprint(base) == extract_fingerprint(reordered)
    assert extract_fingerprint({"labels": {"alertname": "CpuFull"}}) != extract_fingerprint(base)


def test_fingerprint_falls_back_to_a_hash_of_the_payload():
    assert extract_fingerprint({"a": 1}) == extract_fingerprint({"a": 1})
    assert extract_fingerprint({"a": 1}) != extract_fingerprint({"a": 2})


def test_title_falls_back_to_the_fingerprint():
    fingerprint = extract_fingerprint({"widgets": 3})
    assert extract_title({"widgets": 3}, fingerprint) == f"alert {fingerprint[:12]}"
    assert "widgets" in extract_body({"widgets": 3})


def test_long_bodies_are_truncated():
    body = extract_body({"description": "x" * (MAX_BODY + 500)})
    assert len(body) == MAX_BODY
    assert body.endswith("…")


def test_resolution_closes_the_alert_and_a_later_page_opens_a_new_one(db, engine, rota, policies):
    policies.add("ops", [(5, on_call(rota))])
    fired = {"fingerprint": "abc", "alertname": "disk full", "status": "firing"}

    open_alert = post(db, engine, fired, default_policy="ops").alert
    resolved = post(db, engine, {"fingerprint": "abc", "status": "resolved"})

    assert resolved.resolved is True
    assert resolved.alert.id == open_alert.id
    assert resolved.alert.state == "resolved"

    reopened = post(db, engine, fired, default_policy="ops")
    assert reopened.created is True
    assert reopened.alert.id != open_alert.id


def test_resolving_something_that_is_not_open_is_a_no_op(db, engine):
    result = post(db, engine, {"fingerprint": "ghost", "status": "resolved"})
    assert result.alert is None
    assert result.resolved is True
    assert result.deduplicated is False


def test_acked_alert_still_absorbs_duplicates(db, engine, rota, policies):
    policies.add("ops", [(5, on_call(rota))])
    payload = {"fingerprint": "abc", "title": "disk full"}
    alert = post(db, engine, payload, default_policy="ops").alert

    engine.ack(alert.id)
    again = post(db, engine, payload, default_policy="ops")
    assert again.deduplicated is True
    assert again.alert.state == "acked"
    assert len(urls(engine)) == 1


def test_policy_can_come_from_the_payload_or_the_default(db, engine, rota, policies):
    policies.add("billing", [(5, [Target("ntfy", "billing-team")])])
    policies.add("infra", [(5, [Target("ntfy", "infra-team")])])

    named = post(db, engine, {"fingerprint": "a", "policy": "billing"})
    assert named.alert.policy_id == policies.get("billing").id

    defaulted = post(db, engine, {"fingerprint": "b"}, default_policy="infra")
    assert defaulted.alert.policy_id == policies.get("infra").id

    paged = len(urls(engine))
    unrouted = post(db, engine, {"fingerprint": "c"})
    assert unrouted.alert.policy_id is None
    assert unrouted.created is True
    assert len(urls(engine)) == paged


def test_unknown_policy_is_an_error(db, engine):
    with pytest.raises(NotFound):
        post(db, engine, {"fingerprint": "a", "policy": "nope"})
