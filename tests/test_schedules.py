from __future__ import annotations

import pytest

from helpers import BASE, epoch, minutes
from pagekeeper.errors import NotFound, UsageError
from pagekeeper.schedules import ScheduleStore
from pagekeeper.users import UserStore


@pytest.fixture
def people(db, clock) -> UserStore:
    return UserStore(db, clock)


@pytest.fixture
def rota(db, clock) -> ScheduleStore:
    return ScheduleStore(db, clock)


def make_rota(rota, people, names, period=1440):
    rotation = [people.add(name).id for name in names]
    return rota.add("eu", period, rotation, start_at=epoch(BASE))


def who(rota, schedule, offset):
    return rota.on_call(schedule.id, now=offset)[0]


def test_rotation_walks_in_order_and_wraps(rota, people):
    schedule = make_rota(rota, people, ("alice", "bob", "carol"), period=60)

    for offset, expected in (
        (minutes(0), "alice"),
        (minutes(30), "alice"),
        (minutes(60), "bob"),
        (minutes(120), "carol"),
        (minutes(180), "alice"),
        (minutes(300), "carol"),
    ):
        entry = who(rota, schedule, offset)
        assert entry.user.name == expected
        assert entry.source == "rotation"


def test_stint_reports_when_it_ends(rota, people):
    schedule = make_rota(rota, people, ("alice", "bob"), period=60)
    entry = who(rota, schedule, minutes(10))
    assert entry.user.name == "alice"
    assert entry.until == minutes(60)


def test_times_before_the_start_still_resolve(rota, people):
    schedule = make_rota(rota, people, ("alice", "bob"), period=60)
    entry = who(rota, schedule, minutes(-90))
    assert entry.user.name in {"alice", "bob"}


def test_override_wins_inside_its_window(rota, people):
    schedule = make_rota(rota, people, ("alice", "bob"), period=60)
    bob = people.get("bob")
    start, end = minutes(20), minutes(50)
    rota.add_override(schedule.id, bob.id, start, end)

    entry = who(rota, schedule, start)
    assert (entry.user.name, entry.source) == ("bob", "override")
    assert entry.until == end

    assert who(rota, schedule, minutes(19)).source == "rotation"
    # The override window is half open: at `end` the rotation answers again,
    # and minute 50 is still inside the first stint of a 60 minute rotation.
    after = who(rota, schedule, end)
    assert (after.user.name, after.source) == ("alice", "rotation")


def test_last_added_override_wins(rota, people):
    schedule = make_rota(rota, people, ("alice",), period=60)
    carol = people.add("carol")
    bob = people.add("bob")
    rota.add_override(schedule.id, bob.id, minutes(0), minutes(30))
    rota.add_override(schedule.id, carol.id, minutes(0), minutes(30))

    entry = who(rota, schedule, minutes(0))
    assert (entry.user.name, entry.source) == ("carol", "override")


def test_on_call_answers_for_every_schedule(rota, people):
    alice = people.add("alice")
    rota.add("eu", 60, [alice.id], start_at=epoch(BASE))
    rota.add("us", 120, [alice.id], start_at=epoch(BASE))

    entries = rota.on_call(now=minutes(0))
    assert [entry.schedule.name for entry in entries] == ["eu", "us"]
    assert {entry.user.name for entry in entries} == {"alice"}


def test_lookup_accepts_id_or_name(rota, people):
    schedule = make_rota(rota, people, ("alice",))
    assert rota.get(schedule.id).name == "eu"
    assert rota.get("eu").id == schedule.id
    with pytest.raises(NotFound):
        rota.get("apac")


def test_unknown_user_rejected(rota, people):
    with pytest.raises(NotFound):
        rota.add("eu", 60, [404])


def test_empty_rotation_cannot_answer_on_call(rota):
    schedule = rota.add("eu", 60, (), start_at=epoch(BASE))
    with pytest.raises(UsageError, match="no rotation"):
        rota.on_call(schedule.id, now=minutes(0))
    with pytest.raises(UsageError, match="at least one"):
        rota.set_rotation(schedule.id, [])


def test_rejects_duplicate_names_and_bad_periods(rota, people):
    make_rota(rota, people, ("alice",))
    with pytest.raises(UsageError):
        rota.add("eu", 60)
    with pytest.raises(UsageError):
        rota.add("apac", 0)
    with pytest.raises(UsageError):
        rota.add(" ", 60)


def test_override_must_move_forward(rota, people):
    schedule = make_rota(rota, people, ("alice",))
    with pytest.raises(UsageError, match="end after"):
        rota.add_override(schedule.id, people.get("alice").id, minutes(0), minutes(0))


def test_assign_replaces_rotation(rota, people):
    schedule = make_rota(rota, people, ("alice", "bob"))
    carol = people.add("carol")
    rota.set_rotation(schedule.id, [carol.id])

    assert [user.name for user in rota.rotation(schedule.id)] == ["carol"]
    assert who(rota, schedule, minutes(0)).user.name == "carol"
