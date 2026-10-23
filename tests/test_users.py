from __future__ import annotations

import pytest

from pagekeeper.errors import NotFound, UsageError
from pagekeeper.users import UserStore


def test_add_get_and_list(db, clock):
    store = UserStore(db, clock)
    alice = store.add("alice", ntfy_topic="alice-phone")
    store.add("bob", webhook_url="https://hooks.test/bob")

    assert store.get(alice.id) == alice
    assert store.get("bob").webhook_url == "https://hooks.test/bob"
    assert [user.name for user in store.list_all()] == ["alice", "bob"]
    assert store.find("nobody") is None


def test_names_are_unique_and_never_empty(db, clock):
    store = UserStore(db, clock)
    store.add("alice")
    with pytest.raises(UsageError, match="already exists"):
        store.add("alice")
    with pytest.raises(UsageError, match="must not be empty"):
        store.add("   ")
    with pytest.raises(NotFound):
        store.get(99)
