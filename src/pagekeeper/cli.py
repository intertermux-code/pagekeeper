"""The pagekeeper command line."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Callable, Optional, Sequence

from . import __version__
from .clock import format_time, parse_time
from .db import Database
from .engine import EscalationEngine
from .errors import PagekeeperError, UsageError
from .models import OnCall, TARGET_KINDS, Target
from .notifiers import DEFAULT_NTFY_BASE
from .policies import PolicyStore
from .schedules import ScheduleStore
from .server import serve
from .users import UserStore

DEFAULT_DB = "pagekeeper.db"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_number(name: str, default: str, cast: Callable[[str], Any]) -> Any:
    raw = os.environ.get(name, default)
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise UsageError(f"{name}: expected a number, got {raw!r}") from None


def _time(value: str) -> int:
    try:
        return parse_time(value)
    except UsageError as exc:
        raise UsageError(f"{exc} (times are ISO-8601, UTC when naive)") from None


def _user_ids(store: UserStore, names: Sequence[str]) -> list[int]:
    return [store.get(name).id for name in names]


def _split_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _target(raw: str) -> tuple[str, str]:
    if ":" not in raw:
        raise UsageError(
            f"--target expects kind:value, for example user:alice or "
            f"webhook:https://example.invalid/hook (got {raw!r})"
        )
    kind, value = raw.split(":", 1)
    kind = kind.strip().lower()
    value = value.strip()
    if kind not in TARGET_KINDS:
        raise UsageError(
            f"unknown target kind {kind!r}; expected one of {', '.join(TARGET_KINDS)}"
        )
    if not value:
        raise UsageError(f"empty target value in {raw!r}")
    return kind, value


def _resolve_targets(
    users: UserStore, schedules: ScheduleStore, values: Sequence[str]
) -> list[Target]:
    resolved: list[Target] = []
    for raw in values:
        kind, value = _target(raw)
        if kind == "user":
            resolved.append(Target("user", str(users.get(value).id)))
        elif kind == "schedule":
            resolved.append(Target("schedule", str(schedules.get(value).id)))
        else:
            resolved.append(Target(kind, value))
    return resolved


# -- commands -------------------------------------------------------------


def cmd_init(args: argparse.Namespace, db: Database) -> int:
    print(f"database ready at {db.path}")
    return 0


def cmd_user_add(args: argparse.Namespace, db: Database) -> int:
    user = UserStore(db).add(
        args.name, ntfy_topic=args.ntfy_topic, webhook_url=args.webhook_url
    )
    print(f"user {user.name} (id {user.id})")
    return 0


def cmd_schedule_add(args: argparse.Namespace, db: Database) -> int:
    users = UserStore(db)
    schedules = ScheduleStore(db)
    rotation = _user_ids(users, _split_list(args.users)) if args.users else []
    start = _time(args.start) if args.start else None
    schedule = schedules.add(
        args.name, args.period_minutes, rotation, start_at=start
    )
    print(
        f"schedule {schedule.name} (id {schedule.id}) rotates every "
        f"{schedule.period_minutes} minutes from {format_time(schedule.start_at)}"
    )
    if rotation:
        names = ", ".join(user.name for user in schedules.rotation(schedule.id))
        print(f"rotation: {names}")
    else:
        print("no rotation yet: pagekeeper schedule assign "
              f"{schedule.name} --users alice,bob")
    return 0


def cmd_schedule_assign(args: argparse.Namespace, db: Database) -> int:
    users = UserStore(db)
    schedules = ScheduleStore(db)
    schedule = schedules.get(args.name)
    rotation = _user_ids(users, _split_list(args.users))
    schedules.set_rotation(schedule.id, rotation)
    print(f"rotation for {schedule.name}: "
          f"{', '.join(users.get(user_id).name for user_id in rotation)}")
    return 0


def cmd_override_add(args: argparse.Namespace, db: Database) -> int:
    users = UserStore(db)
    schedules = ScheduleStore(db)
    schedule = schedules.get(args.schedule)
    user = users.get(args.user)
    schedules.add_override(
        schedule.id, user.id, _time(args.start), _time(args.end)
    )
    print(
        f"{user.name} covers {schedule.name} from {format_time(_time(args.start))}"
        f" to {format_time(_time(args.end))}"
    )
    return 0


def cmd_policy_add(args: argparse.Namespace, db: Database) -> int:
    users = UserStore(db)
    schedules = ScheduleStore(db)
    targets = _resolve_targets(users, schedules, args.target)
    policy = PolicyStore(db).add(args.name, [(args.delay, targets)])
    print(f"policy {policy.name} (id {policy.id}) with 1 step")
    return 0


def cmd_policy_step_add(args: argparse.Namespace, db: Database) -> int:
    store = PolicyStore(db)
    policy = store.get(args.name)
    targets = _resolve_targets(UserStore(db), ScheduleStore(db), args.target)
    step = store.append_step(policy.id, args.delay, targets)
    print(
        f"policy {policy.name} step {step.position + 1}: wait {args.delay} min, "
        f"{len(targets)} target(s); {len(store.steps(policy.id))} steps in total"
    )
    return 0


def cmd_oncall(args: argparse.Namespace, db: Database) -> int:
    schedules = ScheduleStore(db)
    entries: list[OnCall] = schedules.on_call(args.schedule)
    if not entries:
        print("no schedules configured")
        return 0
    for entry in entries:
        print(
            f"{entry.schedule.name}: {entry.user.name} "
            f"({entry.source}, until {format_time(entry.until)})"
        )
    return 0


def cmd_ack(args: argparse.Namespace, db: Database) -> int:
    alert = EscalationEngine(db).ack(args.alert_id)
    print(
        f"alert {alert.id} acknowledged at {format_time(alert.acked_at)}; "
        "escalation stopped"
    )
    return 0


def cmd_serve(args: argparse.Namespace, db: Database) -> int:
    engine = EscalationEngine(db, ntfy_base=args.ntfy_base)
    serve(
        db,
        host=args.host,
        port=args.port,
        tick_seconds=args.tick,
        default_policy=args.policy,
        engine=engine,
    )
    return 0


# -- parsing --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pagekeeper",
        description="On-call schedules, escalation and paging in one process.",
    )
    parser.add_argument(
        "--db",
        default=_env("PAGEKEEPER_DB", DEFAULT_DB),
        help="SQLite file (default: %(default)s, env PAGEKEEPER_DB)",
    )
    parser.add_argument(
        "--version", action="version", version=f"pagekeeper {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create the database")
    init.set_defaults(handler=cmd_init)

    user = sub.add_parser("user", help="manage people").add_subparsers(
        dest="subcommand", required=True
    )
    user_add = user.add_parser("add", help="add a user")
    user_add.add_argument("name")
    user_add.add_argument("--ntfy-topic", help="ntfy topic or full ntfy server URL")
    user_add.add_argument(
        "--webhook-url", help="per-user webhook that receives pages as JSON"
    )
    user_add.set_defaults(handler=cmd_user_add)

    schedule = sub.add_parser("schedule", help="manage rotations").add_subparsers(
        dest="subcommand", required=True
    )
    schedule_add = schedule.add_parser("add", help="create a rotation")
    schedule_add.add_argument("name")
    schedule_add.add_argument(
        "--period-minutes",
        type=int,
        required=True,
        help="how long each stint lasts, in minutes",
    )
    schedule_add.add_argument(
        "--users", help="comma separated rotation order, e.g. alice,bob"
    )
    schedule_add.add_argument(
        "--start", help="ISO-8601 time the first stint begins (default: now)"
    )
    schedule_add.set_defaults(handler=cmd_schedule_add)

    schedule_assign = schedule.add_parser(
        "assign", help="replace a rotation's order"
    )
    schedule_assign.add_argument("name")
    schedule_assign.add_argument("--users", required=True)
    schedule_assign.set_defaults(handler=cmd_schedule_assign)

    override = sub.add_parser("override", help="one-off coverage").add_subparsers(
        dest="subcommand", required=True
    )
    override_add = override.add_parser("add", help="swap someone in for a window")
    override_add.add_argument("--schedule", required=True)
    override_add.add_argument("--user", required=True)
    override_add.add_argument("--start", required=True, help="ISO-8601, UTC if naive")
    override_add.add_argument("--end", required=True, help="ISO-8601, UTC if naive")
    override_add.set_defaults(handler=cmd_override_add)

    policy = sub.add_parser("policy", help="manage escalation policies")
    policy_sub = policy.add_subparsers(dest="subcommand", required=True)
    policy_add = policy_sub.add_parser("add", help="create a one-step policy")
    policy_add.add_argument("name")
    policy_add.add_argument(
        "--delay", type=int, required=True, help="minutes before escalating onward"
    )
    policy_add.add_argument(
        "--target",
        action="append",
        required=True,
        metavar="KIND:VALUE",
        help="user:NAME, schedule:NAME, ntfy:TOPIC or webhook:URL (repeatable)",
    )
    policy_add.set_defaults(handler=cmd_policy_add)

    step_add = policy_sub.add_parser("step", help="add a step to a policy")
    step_sub = step_add.add_subparsers(dest="step_action", required=True)
    step_append = step_sub.add_parser("add", help="append an escalation step")
    step_append.add_argument("name")
    step_append.add_argument("--delay", type=int, required=True)
    step_append.add_argument("--target", action="append", required=True)
    step_append.set_defaults(handler=cmd_policy_step_add)

    oncall = sub.add_parser("oncall", help="show who is on call now")
    oncall.add_argument("--schedule", help="limit to one schedule")
    oncall.set_defaults(handler=cmd_oncall)

    ack = sub.add_parser("ack", help="acknowledge an alert and stop escalation")
    ack.add_argument("alert_id", type=int)
    ack.set_defaults(handler=cmd_ack)

    serve_cmd = sub.add_parser("serve", help="run the HTTP server and ticker")
    serve_cmd.add_argument("--host", default=_env("PAGEKEEPER_HOST", "127.0.0.1"))
    serve_cmd.add_argument(
        "--port",
        type=int,
        default=_env_number("PAGEKEEPER_PORT", "8080", int),
    )
    serve_cmd.add_argument(
        "--tick",
        type=float,
        default=_env_number("PAGEKEEPER_TICK", "15", float),
        help="seconds between escalation checks",
    )
    serve_cmd.add_argument(
        "--policy",
        default=os.environ.get("PAGEKEEPER_POLICY") or None,
        help="policy used for webhooks that do not name one",
    )
    serve_cmd.add_argument(
        "--ntfy-base",
        default=_env("PAGEKEEPER_NTFY_BASE", DEFAULT_NTFY_BASE),
        help="base URL for bare ntfy topics",
    )
    serve_cmd.set_defaults(handler=cmd_serve)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        db = Database(args.db)
        db.init()
        return args.handler(args, db)
    except PagekeeperError as exc:
        print(f"pagekeeper: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
