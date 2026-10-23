from __future__ import annotations

import pytest

from helpers import seed_rota
from pagekeeper.errors import NotFound, UsageError
from pagekeeper.models import Target


def test_steps_are_read_back_in_order(db, clock, policies):
    rota = seed_rota(db, clock)
    policies.add(
        "ops",
        [
            (5, [Target("schedule", str(rota.schedule.id))]),
            (30, [Target("user", rota.id_of("bob")), Target("webhook", "https://hooks.test/x")]),
        ],
    )

    policy = policies.get("ops")
    assert policy.name == "ops"
    assert [(step.position, step.delay_minutes) for step in policy.steps] == [(0, 5), (1, 30)]
    assert policy.steps[1].targets == (
        Target("user", rota.id_of("bob")),
        Target("webhook", "https://hooks.test/x"),
    )
    assert policies.steps(policy.id)[0].id > 0


def test_append_step_adds_to_the_end(db, clock, policies):
    rota = seed_rota(db, clock)
    policy = policies.add("ops", [(5, [Target("user", rota.id_of("alice"))])])

    step = policies.append_step(policy.id, 20, [Target("ntfy", "ops-channel")])

    assert (step.position, step.delay_minutes) == (1, 20)
    assert [s.delay_minutes for s in policies.steps(policy.id)] == [5, 20]


def test_rejects_unusable_policies(db, policies):
    with pytest.raises(UsageError):
        policies.add("ops", [])
    with pytest.raises(UsageError):
        policies.add(" ", [(5, [Target("ntfy", "ops")])])
    with pytest.raises(UsageError, match="at least one target"):
        policies.add("ops", [(5, [])])
    with pytest.raises(UsageError, match="negative"):
        policies.add("ops", [(-1, [Target("ntfy", "ops")])])
    with pytest.raises(UsageError, match="unknown target kind"):
        policies.add("ops", [(5, [Target("carrier-pigeon", "coop")])])
    with pytest.raises(UsageError, match="empty target"):
        policies.add("ops", [(5, [Target("ntfy", "  ")])])


def test_rejects_duplicate_names(db, policies):
    policies.add("ops", [(5, [Target("ntfy", "ops")])])
    with pytest.raises(UsageError):
        policies.add("ops", [(5, [Target("ntfy", "ops2")])])


def test_lookup_by_id_name_or_nothing(db, policies):
    policy = policies.add("ops", [(5, [Target("ntfy", "ops")])])
    assert policies.get(policy.id).name == "ops"
    assert policies.get("ops").id == policy.id
    with pytest.raises(NotFound):
        policies.get("missing")
    assert policies.find("missing") is None
    assert policies.find(None) is None
    assert [item.name for item in policies.list_all()] == ["ops"]


def test_append_step_checks_the_policy_exists(db, policies):
    with pytest.raises(NotFound):
        policies.append_step(99, 5, [Target("ntfy", "ops")])
