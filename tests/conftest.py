from __future__ import annotations

import pytest

from helpers import FakeClock, RecordingOpener
from pagekeeper.db import Database
from pagekeeper.engine import EscalationEngine
from pagekeeper.notifiers import Notifier
from pagekeeper.policies import PolicyStore


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "pagekeeper.db")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def opener() -> RecordingOpener:
    return RecordingOpener()


@pytest.fixture
def policies(db, clock) -> PolicyStore:
    return PolicyStore(db, clock)


@pytest.fixture
def engine(db, clock, opener) -> EscalationEngine:
    """An engine whose clock and network are both under test control."""
    return EscalationEngine(
        db,
        notifier=Notifier(ntfy_base="https://ntfy.test", opener=opener),
        clock=clock,
    )
