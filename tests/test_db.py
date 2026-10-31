from __future__ import annotations

import sqlite3

from pagekeeper.db import SCHEMA, Database, _TARGET_KINDS_SQL
from pagekeeper.models import Target
from pagekeeper.policies import PolicyStore
from pagekeeper.users import UserStore

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


def fake_home(tmp_path, monkeypatch):
    """Point `~` at a directory we own, and run from one, so a path that is
    expanded at mkdir time but not at open time shows up as a file we can see."""
    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(workdir)
    return home, workdir


def test_a_home_relative_path_is_created_and_opened_in_home(tmp_path, monkeypatch):
    home, workdir = fake_home(tmp_path, monkeypatch)

    db = Database("~/oncall/pagekeeper.db")
    db.init()
    UserStore(db).add("alice")

    assert db.path == str(home / "oncall" / "pagekeeper.db")
    assert (home / "oncall" / "pagekeeper.db").is_file()
    # The paging happened in home, not beside a literal `~` left in the cwd.
    assert not (workdir / "~").exists()
    assert UserStore(db).get("alice").name == "alice"


def test_a_home_relative_path_survives_a_second_process(tmp_path, monkeypatch):
    home, _ = fake_home(tmp_path, monkeypatch)

    Database("~/oncall/pagekeeper.db").init()
    reopened = Database("~/oncall/pagekeeper.db")
    UserStore(reopened).add("bob")

    with sqlite3.connect(home / "oncall" / "pagekeeper.db") as conn:
        names = [row[0] for row in conn.execute("SELECT name FROM users")]

    assert names == ["bob"]


def test_a_plain_relative_path_is_untouched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    db = Database("nested/pagekeeper.db")
    db.init()

    assert db.path == "nested/pagekeeper.db"
    assert (tmp_path / "nested" / "pagekeeper.db").is_file()


def test_an_absolute_path_is_untouched(tmp_path):
    path = tmp_path / "absolute.db"

    db = Database(path)
    db.init()

    assert db.path == str(path)
    assert path.is_file()
