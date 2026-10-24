from __future__ import annotations

import sqlite3

from pagekeeper.db import SCHEMA, Database, _TARGET_KINDS_SQL
from pagekeeper.models import Target
from pagekeeper.policies import PolicyStore

#: Exactly what the first release wrote into the CHECK constraint.
LEGACY_KINDS = "'user', 'schedule', 'ntfy', 'webhook'"


def seed_legacy_database(path: str) -> None:
    """Build a file the way an installed database would look: old constraint,
    one target already stored against it."""
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA.replace(_TARGET_KINDS_SQL, LEGACY_KINDS))
        policy = conn.execute(
            "INSERT INTO policies (name, created_at) VALUES ('ops', 1)"
        )
        step = conn.execute(
            "INSERT INTO policy_steps (policy_id, position, delay_minutes)"
            " VALUES (?, 0, 5)",
            (policy.lastrowid,),
        )
        conn.execute(
            "INSERT INTO step_targets (step_id, kind, value) VALUES (?, 'ntfy', 'board')",
            (step.lastrowid,),
        )


def test_an_existing_database_learns_the_new_kinds(tmp_path, clock):
    path = str(tmp_path / "legacy.db")
    seed_legacy_database(path)

    db = Database(path)
    policies = PolicyStore(db, clock)
    policy = policies.get("ops")

    # Whatever the old file stored is still there after the upgrade.
    assert [t.kind for t in policies.steps(policy.id)[0].targets] == ["ntfy"]

    policies.append_step(
        policy.id,
        10,
        [
            Target("slack", "https://hooks.slack.test/services/T00/B00/XXXX"),
            Target("discord", "https://discord.test/api/webhooks/123/token"),
        ],
    )

    assert [t.kind for t in policies.steps(policy.id)[1].targets] == [
        "slack",
        "discord",
    ]
    with sqlite3.connect(path) as conn:
        stored = conn.execute("SELECT kind, value FROM step_targets ORDER BY id").fetchall()
    assert [row[0] for row in stored] == ["ntfy", "slack", "discord"]


def test_the_upgrade_is_a_no_op_on_a_current_database(tmp_path):
    db = Database(tmp_path / "fresh.db")
    db.init()
    db.init()

    with sqlite3.connect(db.path) as conn:
        stored = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='step_targets'"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert _TARGET_KINDS_SQL in stored
    assert "step_targets_upgrade" not in tables
