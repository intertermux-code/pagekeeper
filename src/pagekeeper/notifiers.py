"""Outbound paging.

Four channels, all plain HTTP and all stdlib only:

* ``ntfy``    - a topic on ntfy.sh or a self-hosted server.
* ``webhook`` - a JSON POST.  The body carries a top-level ``text`` field,
  which is what Slack incoming webhooks look for, so Slack works without a
  dedicated integration.
* ``slack``   - a Block Kit message with an Ack button, posted to a workflow
  or app webhook.  The button only comes back to us if the owning app has
  interactivity wired to ``POST /slack/actions``; see ``server``.
* ``discord`` - an embed posted to a channel webhook.  One-way: Discord
  channel webhooks cannot carry a button that calls back into pagekeeper.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable

from . import __version__
from .errors import UsageError
from .models import PageMessage

DEFAULT_NTFY_BASE = "https://ntfy.sh"
USER_AGENT = f"pagekeeper/{__version__}"

#: The button the interactive endpoint looks for, carrying the alert id.
ACK_ACTION_ID = "ack"
ACK_BUTTON_LABEL = "Acknowledge"

#: Metadata every chat channel shows, in this order.
FIELD_LABELS = {
    "alert_id": "alert",
    "fingerprint": "fingerprint",
    "policy": "policy",
    "step": "step",
    "state": "state",
}

# Platform limits we must stay inside of, or the POST is rejected outright.
SLACK_TITLE_LIMIT = 150
SLACK_TEXT_LIMIT = 2900
SLACK_VALUE_LIMIT = 250
DISCORD_TITLE_LIMIT = 256
DISCORD_DESCRIPTION_LIMIT = 3900
FIELD_VALUE_LIMIT = 1000
#: A step past the first one wakes more people, which is worth seeing at a glance.
DISCORD_COLOR_URGENT = 0xE8483F
DISCORD_COLOR_NORMAL = 0xF2B41A

_TOPIC = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)

Opener = Callable[..., object]


def _header_value(text: str, limit: int = 200) -> str:
    """ntfy reads Title from a header, so strip everything unsafe for one."""
    cleaned = "".join(char for char in text if 32 <= ord(char) < 127)
    return cleaned[:limit].strip()


def _checked_url(url: str) -> str:
    if not _URL.match(url):
        raise UsageError(f"not a valid http(s) URL: {url!r}")
    return url


def _clip(text: str, limit: int) -> str:
    """Chat platforms reject empty and over-long strings, not just unsafe ones."""
    clipped = text.strip()
    if len(clipped) > limit:
        return clipped[: limit - 1] + "…"
    return clipped or "-"


def _one_line(text: str, limit: int) -> str:
    return _clip(" ".join(text.split()), limit)


def _json_post(url: str, payload: dict) -> urllib.request.Request:
    body = json.dumps(payload, default=str).encode("utf-8")
    return urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )


def alert_fields(message: PageMessage) -> list[tuple[str, str]]:
    """The alert metadata both chat channels show, coerced to strings."""
    return [
        (label, _one_line(str(message.fields.get(key, "")), FIELD_VALUE_LIMIT))
        for key, label in FIELD_LABELS.items()
    ]


def ntfy_destination(destination: str, base: str = DEFAULT_NTFY_BASE) -> str:
    """Accept either a bare topic name or a full server URL."""
    destination = destination.strip()
    if _URL.match(destination):
        return _checked_url(destination)
    if not _TOPIC.match(destination):
        raise UsageError(
            f"invalid ntfy topic {destination!r}: use letters, digits, "
            ". _ - or a full server URL"
        )
    return f"{base.rstrip('/')}/{destination}"


def ntfy_request(
    destination: str, message: PageMessage, base: str = DEFAULT_NTFY_BASE
) -> urllib.request.Request:
    url = ntfy_destination(destination, base)
    headers = {
        "Title": _header_value(message.title),
        "Priority": str(max(1, min(5, message.priority))),
        "Content-Type": "text/plain; charset=utf-8",
        "User-Agent": USER_AGENT,
    }
    if message.priority >= 4:
        headers["Tags"] = "rotating_light"
    body = f"{message.text}\n".encode("utf-8")
    return urllib.request.Request(url, data=body, headers=headers, method="POST")


def webhook_request(destination: str, message: PageMessage) -> urllib.request.Request:
    url = _checked_url(destination)
    payload = {"text": message.text, "title": message.title}
    payload.update(message.fields)
    return _json_post(url, payload)


def slack_payload(message: PageMessage) -> dict:
    """A Block Kit message: header, detail, the alert fields, an Ack button."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": _one_line(message.title, SLACK_TITLE_LIMIT),
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _clip(message.text, SLACK_TEXT_LIMIT)},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*{label}*\n{value}"}
                for label, value in alert_fields(message)
            ],
        },
    ]
    alert_id = message.fields.get("alert_id")
    if alert_id is not None:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": ACK_ACTION_ID,
                        "style": "primary",
                        "text": {
                            "type": "plain_text",
                            "text": ACK_BUTTON_LABEL,
                            "emoji": True,
                        },
                        "value": _one_line(str(alert_id), SLACK_VALUE_LIMIT),
                    }
                ],
            }
        )
    # Notifications only show the top-level text, so it has to stand alone.
    return {"text": _one_line(message.title, SLACK_TITLE_LIMIT), "blocks": blocks}


def slack_request(destination: str, message: PageMessage) -> urllib.request.Request:
    return _json_post(_checked_url(destination), slack_payload(message))


def discord_payload(message: PageMessage) -> dict:
    return {
        "embeds": [
            {
                "title": _one_line(message.title, DISCORD_TITLE_LIMIT),
                "description": _clip(message.text, DISCORD_DESCRIPTION_LIMIT),
                "color": (
                    DISCORD_COLOR_URGENT
                    if message.priority >= 4
                    else DISCORD_COLOR_NORMAL
                ),
                "fields": [
                    {"name": label, "value": value, "inline": True}
                    for label, value in alert_fields(message)
                ],
            }
        ]
    }


def discord_request(destination: str, message: PageMessage) -> urllib.request.Request:
    return _json_post(_checked_url(destination), discord_payload(message))


#: Channels whose destination is a URL we POST JSON to.
JSON_BUILDERS = {
    "webhook": webhook_request,
    "slack": slack_request,
    "discord": discord_request,
}


class Notifier:
    """Sends prepared messages.  `opener` is injectable for tests."""

    def __init__(
        self,
        ntfy_base: str = DEFAULT_NTFY_BASE,
        opener: Opener = urllib.request.urlopen,
        timeout: float = 10.0,
    ) -> None:
        self.ntfy_base = ntfy_base
        self.opener = opener
        self.timeout = timeout

    def send(self, channel: str, destination: str, message: PageMessage) -> tuple[bool, str]:
        if channel == "ntfy":
            builder: Callable[[str, PageMessage], urllib.request.Request] = (
                lambda url, msg: ntfy_request(url, msg, self.ntfy_base)
            )
        elif channel in JSON_BUILDERS:
            builder = JSON_BUILDERS[channel]
        else:
            return False, f"unknown channel: {channel}"

        try:
            request = builder(destination, message)
            response = self.opener(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except (urllib.error.URLError, UsageError, OSError, ValueError) as exc:
            return False, f"{type(exc).__name__}: {exc}"

        try:
            status = int(response.status)  # type: ignore[union-attr]
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
        return 200 <= status < 300, f"HTTP {status}"
