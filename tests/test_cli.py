from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pagekeeper import cli
from pagekeeper.alerts import AlertStore
from pagekeeper.db import Database
from pagekeeper.models import Target
from pagekeeper.policies import PolicyStore
from pagekeeper.schedules import ScheduleStore
from pagekeeper.users import UserStore


@pytest.fixture
def database(tmp_path) -> Database:
    return Database(tmp_path / "cli.db")


def run(database, *argv):
    """Invoke the CLI against the test database and hand back its exit code."""
    return cli.main(["--db", str(database.path), *argv])


def test_user_add_and_duplicate(database, capsys):
    assert run(database, "user", "add", "alice", "--ntfy-topic", "alice-phone") == 0
    assert "user alice (id 1)" in capsys.readouterr().out

    assert run(database, "user", "add", "alice") == 1
    assert "already exists" in capsys.readouterr().err
    assert [user.name for user in UserStore(database).list_all()] == ["alice"]


def test_schedule_add_and_oncall(database, capsys):
    run(database, "user", "add", "alice")
    run(database, "user", "add", "bob")

    started = (datetime.now(timezone.utc) - timedelta(minutes=70)).isoformat(timespec="seconds")
    assert run(
        database, "schedule", "add", "eu", "--period-minutes", "60",
        "--users", "alice,bob", "--start", started,
    ) == 0
    assert "rotates every 60 minutes" in capsys.readouterr().out

    assert run(database, "oncall") == 0
    # 70 minutes into an hourly alice -> bob rotation, bob is holding the pager.
    assert "eu: bob (rotation, until" in capsys.readouterr().out

    code = run(database, "schedule", "add", "apac", "--period-minutes", "60")
    assert code == 0
    assert "no rotation yet" in capsys.readouterr().out


def test_schedule_assign_replaces_the_rotation(database, capsys):
    run(database, "user", "add", "alice")
    run(database, "user", "add", "carol")
    run(database, "schedule", "add", "eu", "--period-minutes", "1440", "--users", "alice")
    capsys.readouterr()

    assert run(database, "schedule", "assign", "eu", "--users", "carol,alice") == 0
    assert "carol, alice" in capsys.readouterr().out
    assert run(database, "oncall") == 0
    assert "eu: carol" in capsys.readouterr().out


def test_override_moves_the_pager(database, capsys):
    run(database, "user", "add", "alice")
    run(database, "user", "add", "bob")
    run(database, "schedule", "add", "eu", "--period-minutes", "1440", "--users", "alice")
    capsys.readouterr()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    assert run(
        database, "override", "add", "--schedule", "eu", "--user", "bob",
        "--start", now.isoformat(timespec="seconds"),
        "--end", (now + timedelta(hours=8)).isoformat(timespec="seconds"),
    ) == 0
    capsys.readouterr()

    assert run(database, "oncall", "--schedule", "eu") == 0
    assert "eu: bob (override" in capsys.readouterr().out


def test_policy_add_and_step_add(database, capsys):
    run(database, "user", "add", "alice")
    assert run(
        database, "policy", "add", "ops", "--delay", "5", "--target", "user:alice"
    ) == 0
    assert run(
        database, "policy", "step", "add", "ops", "--delay", "15",
        "--target", "webhook:https://hooks.test/board",
        "--target", "ntfy:oncall-board",
    ) == 0

    steps = PolicyStore(database).get("ops").steps
    assert [(s.delay_minutes, s.targets) for s in steps] == [
        (5, (Target("user", "1"),)),
        (15, (Target("webhook", "https://hooks.test/board"), Target("ntfy", "oncall-board"))),
    ]


def test_policy_target_accepts_schedules_by_name(database, capsys):
    run(database, "user", "add", "alice")
    run(database, "schedule", "add", "eu", "--period-minutes", "60", "--users", "alice")
    capsys.readouterr()

    assert run(
        database, "policy", "add", "ops", "--delay", "0", "--target", "schedule:eu"
    ) == 0
    schedule_id = ScheduleStore(database).get("eu").id
    assert PolicyStore(database).get("ops").steps[0].targets == (
        Target("schedule", str(schedule_id)),
    )


def test_ack_stops_escalation(database, capsys):
    run(database, "user", "add", "alice")
    run(database, "policy", "add", "ops", "--delay", "5", "--target", "ntfy:ops-board")
    store = AlertStore(database)
    alert = store.create(
        fingerprint="abc", title="disk full", policy_id=PolicyStore(database).get("ops").id
    )
    capsys.readouterr()

    assert run(database, "ack", str(alert.id)) == 0
    out = capsys.readouterr().out
    assert "escalation stopped" in out

    acked = store.get(alert.id)
    assert acked.state == "acked"
    assert acked.due_at is None
    assert acked.acked_at is not None


def test_cli_reports_problems_and_exits_one(database, capsys):
    assert run(database, "ack", "404") == 1
    assert "no such alert: 404" in capsys.readouterr().err

    assert run(database, "user", "add", "alice", "--ntfy-topic", "a") == 0
    capsys.readouterr()
    assert run(database, "policy", "add", "ops", "--delay", "5", "--target", "smoke:signals") == 1
    assert "unknown target kind" in capsys.readouterr().err

    assert run(database, "policy", "add", "ops", "--delay", "5", "--target", "ntfy") == 1
    assert "kind:value" in capsys.readouterr().err

    run(database, "user", "add", "bob")
    assert run(
        database, "override", "add", "--schedule", "missing", "--user", "bob",
        "--start", "2026-01-01T00:00:00", "--end", "2026-01-02T00:00:00",
    ) == 1
    assert "no such schedule" in capsys.readouterr().err

    assert run(database, "schedule", "add", "eu", "--period-minutes", "60", "--start", "last tuesday") == 1
    assert "ISO-8601" in capsys.readouterr().err


def test_oncall_without_schedules_is_not_an_error(database, capsys):
    assert run(database, "oncall") == 0
    assert "no schedules configured" in capsys.readouterr().out


def test_init_creates_the_database_file(database, capsys):
    assert run(database, "init") == 0
    assert "database ready" in capsys.readouterr().out
    assert Path(database.path).exists()


def test_bad_target_value_for_an_unknown_user(database, capsys):
    assert run(
        database, "policy", "add", "ops", "--delay", "5", "--target", "user:nobody"
    ) == 1
    assert "no such user" in capsys.readouterr().err


def test_chat_targets_keep_their_urls(database, capsys):
    """The value is split on the first colon only, so https:// survives."""
    assert run(
        database, "policy", "add", "ops", "--delay", "5",
        "--target", "slack:https://hooks.slack.test/services/T00/B00/XXXX",
    ) == 0
    assert run(
        database, "policy", "step", "add", "ops", "--delay", "10",
        "--target", "discord:https://discord.test/api/webhooks/123/token",
    ) == 0
    capsys.readouterr()

    policies = PolicyStore(database)
    steps = policies.steps(policies.get("ops").id)
    assert [(step.targets[0].kind, step.targets[0].value) for step in steps] == [
        ("slack", "https://hooks.slack.test/services/T00/B00/XXXX"),
        ("discord", "https://discord.test/api/webhooks/123/token"),
    ]


def test_serve_reads_the_slack_signing_secret_from_flag_or_env(monkeypatch):
    monkeypatch.delenv("PAGEKEEPER_SLACK_SIGNING_SECRET", raising=False)
    assert cli.build_parser().parse_args(["serve"]).slack_signing_secret is None

    monkeypatch.setenv("PAGEKEEPER_SLACK_SIGNING_SECRET", "from-the-environment")
    assert (
        cli.build_parser().parse_args(["serve"]).slack_signing_secret
        == "from-the-environment"
    )
    assert (
        cli.build_parser().parse_args(
            ["serve", "--slack-signing-secret", "from-the-flag"]
        ).slack_signing_secret
        == "from-the-flag"
    )


def run_with_db(db_path, *argv):
    """Invoke the CLI with an arbitrary database path; return its exit code."""
    return cli.main(["--db", str(db_path), *argv])


def assert_clean_db_error(code, err, db_path):
    """A bad --db path must fail cleanly: exit 2, path named, no traceback."""
    assert code == 2
    assert "Traceback" not in err
    assert "cannot open database" in err
    assert str(db_path) in err


def test_bad_db_uncreatable_parent_exits_cleanly(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a regular file where a directory is needed")
    db_path = blocker / "nested" / "pagekeeper.db"

    code = run_with_db(db_path, "init")
    err = capsys.readouterr().err

    assert_clean_db_error(code, err, db_path)
    # No database file materialised beside the cwd.
    assert not (tmp_path / "pagekeeper.db").exists()


def test_bad_db_path_is_a_directory_exits_cleanly(tmp_path, capsys):
    assert run_with_db(tmp_path, "init") == 2

    err = capsys.readouterr().err
    assert_clean_db_error(2, err, tmp_path)
    assert not (tmp_path / "pagekeeper.db").exists()


def test_bad_db_unwritable_location_exits_cleanly(tmp_path, capsys):
    if os.geteuid() == 0:
        pytest.skip("root bypasses permission bits: a directory cannot be made unwritable")

    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        code = run_with_db(readonly / "pagekeeper.db", "init")
        err = capsys.readouterr().err
        assert_clean_db_error(code, err, readonly / "pagekeeper.db")
    finally:
        readonly.chmod(0o755)

    assert not (tmp_path / "pagekeeper.db").exists()


def test_home_relative_db_still_works_after_the_fix(tmp_path, capsys, monkeypatch):
    """Guard: the `~` expansion fix (2026-10-31) still behaves as before."""
    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(workdir)

    assert run_with_db("~/pagekeeper.db", "init") == 0
    assert "database ready" in capsys.readouterr().out
    assert (home / "pagekeeper.db").is_file()
    # A literal `~` must never be created beside the cwd.
    assert not (workdir / "~").exists()
