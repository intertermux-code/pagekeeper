# pagekeeper

Lightweight on-call scheduling, escalation and paging — the parts small teams
actually use, in one Python package backed by SQLite.

`pagekeeper` knows who is on call, takes alerts over a webhook, pages the
right person on ntfy or any HTTP endpoint, escalates to the next step when
nobody acknowledges in time, and stops when somebody does. It is a single
process with no database server, no message broker and no third-party
dependencies: Python 3.11+ standard library only.

## Why this exists

Atlassian is winding OpsGenie down (support ends 2027-04-05) and PagerDuty
costs more than most small teams want to pay for rotation + escalation +
push notifications. The open-source alternatives that remain are multi-service
platforms — several containers, a Postgres, a queue — which is a lot of surface
to operate for a handful of people sharing a pager. `pagekeeper` is the
single-process answer: one command, one file, done.

## Quickstart

```bash
pip install -e .            # from a checkout; or: pipx install git+https://github.com/intertermux-code/pagekeeper
mkdir -p ~/oncall && cd ~/oncall
export PAGEKEEPER_DB=~/oncall/pagekeeper.db
```

Add the people, the rotation and the escalation policy:

```bash
pagekeeper user add alice --ntfy-topic alice-pager
pagekeeper user add bob   --ntfy-topic bob-pager

pagekeeper schedule add eu --period-minutes 10080 --users alice,bob

pagekeeper policy add ops --delay 5 --target schedule:eu
pagekeeper policy step add ops --delay 15 --target user:bob --target webhook:https://hooks.slack.com/services/T00/B00/XXXX
```

The rotation starts now, so alice holds the pager for the first week; pass
`--start 2026-01-05T00:00:00` to anchor it to a Monday instead. `schedule:eu`
means "whoever is on call in the `eu` rotation right now", so the first step
follows the rotation automatically.

Check the rotation, then start serving:

```bash
pagekeeper oncall
# eu: alice (rotation, until 2026-10-30 17:32:08 UTC)

pagekeeper serve --host 0.0.0.0 --port 8080 --policy ops
# pagekeeper serving http://0.0.0.0:8080 (database /home/dev/oncall/pagekeeper.db, ticking every 15s)
```

Fire a test alert from anywhere else:

```bash
curl -X POST http://localhost:8080/hook \
  -H 'content-type: application/json' \
  -d '{"fingerprint": "db1-disk", "alertname": "disk full", "description": "db-1 at 97%"}'
```

```json
{"alert_id": 1, "created": true, "deduplicated": false, "fingerprint": "db1-disk", "resolved": false, "state": "open"}
```

alice gets paged on ntfy immediately. If nothing acknowledges within 5 minutes,
the second step wakes bob and posts to Slack. Open `http://localhost:8080/` to
see the rotation and every open alert, and acknowledge from the CLI:

```bash
pagekeeper ack 1
# alert 1 acknowledged at 2026-10-23 18:04:12 UTC; escalation stopped
```

## Webhooks

`POST /hook` (or `POST /webhook`) accepts any JSON object. pagekeeper pulls
what it needs out of it, in this order:

| Piece | Where it looks |
| --- | --- |
| fingerprint | `fingerprint`, `dedup_key`, `routing_key`, `dedup`, `key`, `id` — also inside `labels`, `alert`, `attributes`, `metadata`, `event`, `data`. Missing? A hash is derived from `alertname`/`service`/`host`/`instance`/`cluster`/`severity`, and from the whole payload if none of those are present. |
| title | `title`, `subject`, `alertname`, `summary`, `name`, `message` |
| detail | `description`, `text`, `message`, `content`, otherwise the compact payload (capped at 4000 characters) |
| policy | `policy` in the body, `?policy=NAME` in the URL, or the server's `--policy` default |
| resolution | `state`/`status`/`event`/`type` equal to `resolved`, `closed`, `recovered`, `ok` or `restore` |

**Deduplication.** While an alert is `open` or `acked`, a webhook with the same
fingerprint returns that alert (`"deduplicated": true`, HTTP 200) instead of
creating a new one, and never restarts the escalation. A payload announcing a
resolution closes the alert, so the next firing of the same fingerprint starts a
fresh escalation. `GET /healthz` answers `ok` for liveness probes.

## Notifiers

Two channels, both stdlib HTTP. No phone calls, no SMS.

- **ntfy** — `--ntfy-topic oncall-team`, `https://ntfy.sh/oncall-team` or your
  own server URL. Priority rises with each escalation step (3 → 4 → 5) and the
  later steps get the `rotating_light` tag. Point `PAGEKEEPER_NTFY_BASE` at a
  self-hosted server to keep the topics private.
- **generic webhook** — a JSON `POST` with `text`, `title` and structured fields
  (`alert_id`, `fingerprint`, `policy`, `step`, `state`). Slack incoming
  webhooks read `text`, so Slack works without a dedicated integration.

Every attempt is recorded in the `pages` table, successes and failures alike,
and failures are logged to stderr — a broken webhook never stops the clock.

## Configuration

Everything is flags or environment variables; there is no config file and no
secret is ever baked in.

| Setting | Flag | Default |
| --- | --- | --- |
| database | `--db` / `PAGEKEEPER_DB` | `pagekeeper.db` |
| bind address | `serve --host` / `PAGEKEEPER_HOST` | `127.0.0.1` |
| port | `serve --port` / `PAGEKEEPER_PORT` | `8080` |
| tick interval (s) | `serve --tick` / `PAGEKEEPER_TICK` | `15` |
| default policy | `serve --policy` / `PAGEKEEPER_POLICY` | none |
| ntfy base URL | `serve --ntfy-base` / `PAGEKEEPER_NTFY_BASE` | `https://ntfy.sh` |

Times are ISO-8601; a timestamp without an offset is read as UTC.

The database file holds your webhook URLs and ntfy topics, so keep it as
sensitive as the endpoints it points at: `chmod 600 "$PAGEKEEPER_DB"`.
Backups are a file copy, and so is the whole migration story.

### Escalation semantics

A policy is an ordered list of steps. Firing pages step 1 straight away and
arms a deadline of `--delay` minutes. When the deadline passes with nobody
having acknowledged, the next step is paged and a new deadline is armed. The
final step has nobody further to wake, so the alert stays open without a
deadline. Acknowledging clears the deadline; the alert stays visible until it is
resolved. A policy with a single step pages once and never escalates — the
`--delay` you gave it is then unused.

`serve` runs the ticker in a thread inside the same process, roughly every
`--tick` seconds. Escalation timing therefore drifts by at most one tick, and
resumes where it left off if you restart the process — state lives in SQLite,
not in memory.

### Systemd

```ini
[Unit]
Description=pagekeeper
After=network.target

[Service]
ExecStart=/usr/local/bin/pagekeeper serve --host 0.0.0.0 --port 8080 --policy ops
Environment=PAGEKEEPER_DB=/var/lib/pagekeeper/pagekeeper.db
User=pagekeeper
DynamicUser=yes
StateDirectory=pagekeeper

[Install]
WantedBy=multi-user.target
```

## CLI

| Command | What it does |
| --- | --- |
| `init` | create the database (any command will also do it on demand) |
| `user add NAME [--ntfy-topic T] [--webhook-url U]` | register somebody and how to reach them |
| `schedule add NAME --period-minutes N [--users a,b] [--start ISO]` | create a rotation |
| `schedule assign NAME --users a,b` | replace a rotation's order |
| `override add --schedule NAME --user NAME --start ISO --end ISO` | hand the pager to someone for a window |
| `policy add NAME --delay MIN --target KIND:VALUE` | create a policy with one step |
| `policy step add NAME --delay MIN --target KIND:VALUE` | append an escalation step (repeat `--target`) |
| `oncall [--schedule NAME]` | who is holding the pager right now |
| `ack ALERT_ID` | acknowledge and stop escalation |
| `serve [--host H] [--port P] [--tick S] [--policy NAME]` | webhook ingress, status page and ticker |

Targets are `user:NAME`, `schedule:NAME`, `ntfy:TOPIC` or `webhook:URL`. `user`
and `schedule` targets are stored by id, so paging keeps working after a rename
of the row you referenced.

## Scope and limitations

Read this before adopting it; it is deliberately small.

- **No SMS, no phone calls.** Outbound paging is HTTP only. If your team needs
  a voice call, that is a different product.
- **Single process, single node.** One `serve` owns the ticker. Running two
  copies would double-page and they do not coordinate.
- **SQLite.** Great for one node and small teams; there is no replication, and
  writes are per-transaction, so a very high alert fan-out will bottleneck.
- **Not an incident platform.** No timelines, no postmortems, no on-call
  calendar UI, no chat bots, no scheduling of meetings, no mobile app. There is
  one status page and it is server-rendered HTML.
- **No authentication on the ingress endpoint.** `/hook` accepts JSON from
  whoever can reach it. Put it behind TLS and a reverse proxy with source
  limits or a shared secret path, and keep `GET /` behind basic auth if the
  alert titles are sensitive.
- **Ack is by CLI.** Acknowledging from a notification would need a signing
  scheme and a per-recipient link; v1 stops at `pagekeeper ack <id>`.
- **No users' email reminders, no notifications of schedule changes**, no
  multi-timezone working-hours rotations beyond a UTC start time.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
python -m pytest -q
```

The suite drives the escalation engine, the server and the CLI with an
injectable clock and a recording HTTP opener, so no test touches the network.

## License

MIT — see [LICENSE](LICENSE).
