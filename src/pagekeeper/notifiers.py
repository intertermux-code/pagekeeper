"""Outbound paging.

Two channels, both plain HTTP and both stdlib only:

* ``ntfy``    - a topic on ntfy.sh or a self-hosted server.
* ``webhook`` - a JSON POST.  The body carries a top-level ``text`` field,
  which is what Slack incoming webhooks look for, so Slack works without a
  dedicated integration.
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
        elif channel == "webhook":
            builder = webhook_request
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
