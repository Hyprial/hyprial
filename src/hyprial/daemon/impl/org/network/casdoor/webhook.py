"""Casdoor webhook receiver: a loopback signal that one user may have changed.

Casdoor (in a container on the owner host) posts its record of each
update-user / delete-user / update-group call to
``http://host.docker.internal:18790/casdoor-webhook/<path>`` with the header
``X-Hyprial-Webhook-Token``.  Casdoor 4.2 cannot sign webhooks
(``LAX(webhook-auth)``): the compensations are the loopback-only bind, the
token compared in constant time, and that the payload is only a signal, never
content.  The worst a forged request can do is make the membership actor
re-read one user (or run one coalesced full pass).

Payloads can carry secrets (request bodies are recorded verbatim), so no body,
path or token ever reaches a log line or the disk; only the action and what
was triggered are logged.  Which user changed (infra-ops, production image
8847a6e, 2026-10-08):
- update-user: ``id`` in ``requestUri`` (owner/name; the old name on a rename);
  an update by ``userId`` has no name, so a full pass runs instead;
- delete-user: ``owner``/``name`` in ``object`` (the request body, a string);
- update-group: ``id`` in ``requestUri`` names the group; ``object.users`` is
  not a member list, so that org gets a full pass.
Anything unparseable runs a full pass; other actions are ignored.

Reachability (deployment): the receiver binds 127.0.0.1:18790 and Casdoor
reaches it as ``host.docker.internal``.  That works under Docker Desktop and
OrbStack (production is OrbStack, verified by infra-ops); on a Linux bridge
network it does not, and the webhook then never arrives, leaving only the
6-hour backstop.  Under Docker Desktop / OrbStack every container on the host
can reach this port, so the token, not loopback, is the real control.  The
port is fixed: a process that takes it first would receive the token header
and Casdoor's request bodies, so the daemon logs ``webhook-unavailable`` and
the port must be kept free on the owner host.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any
import urllib.parse

from hyprial.daemon.impl.org.network.casdoor.client import CASDOOR_ORGANIZATION, read_credential_values

WEBHOOK_HOST = "127.0.0.1"
WEBHOOK_PORT = 18790
TOKEN_HEADER = "X-Hyprial-Webhook-Token"
_MAX_BODY_BYTES = 256 * 1024
_KNOWN_ACTIONS = ("update-user", "delete-user", "update-group")
HANDLER_TIMEOUT_SECONDS = 10.0

_Logger = Callable[..., None]


@dataclass(frozen=True, slots=True)
class WebhookSettings:
    path: str
    token: str


@dataclass(frozen=True, slots=True)
class Trigger:
    """``("user", "<name>")``, ``("org", "<org>")`` or ``("all", None)``."""

    kind: str
    name: str | None = None


def webhook_settings(credential_file: Path) -> WebhookSettings | None:
    """The served path and token, or None when the file names no webhook."""

    values = read_credential_values(credential_file)
    url = values.get("CASDOOR_GROUP_SYNC_WEBHOOK_URL")
    token = values.get("CASDOOR_WEBHOOK_TOKEN")
    if not url or not token:
        return None
    path = urllib.parse.urlsplit(url).path
    return WebhookSettings(path=path, token=token) if path.startswith("/") else None


def _id_from_uri(request_uri: object) -> str | None:
    if not isinstance(request_uri, str):
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request_uri).query)
    values = query.get("id")
    return values[0] if values else None


def _split_owner_name(identifier: str | None) -> tuple[str, str] | None:
    if not identifier or "/" not in identifier:
        return None
    owner, _, name = identifier.partition("/")
    return (owner, name) if owner and name else None


def triggers_for(record: object) -> list[Trigger]:
    """What one webhook record asks the actor to re-read (never its content)."""

    if not isinstance(record, dict):
        return [Trigger("all")]
    action = record.get("action")
    if action == "update-user":
        target = _split_owner_name(_id_from_uri(record.get("requestUri")))
        if target is None:
            return [Trigger("all")]
        owner, name = target
        return [Trigger("user", name)] if owner == CASDOOR_ORGANIZATION else []
    if action == "delete-user":
        try:
            body: Any = json.loads(record.get("object") or "")
        except (TypeError, ValueError):
            return [Trigger("all")]
        if not isinstance(body, dict) or not isinstance(body.get("name"), str) or not body["name"]:
            return [Trigger("all")]
        return [Trigger("user", body["name"])] if body.get("owner") == CASDOOR_ORGANIZATION else []
    if action == "update-group":
        target = _split_owner_name(_id_from_uri(record.get("requestUri")))
        if target is None:
            return [Trigger("all")]
        owner, group = target
        return [Trigger("org", group)] if owner == CASDOOR_ORGANIZATION else []
    return []


class CasdoorWebhookReceiver:
    """``POST <path>`` on 127.0.0.1 -> ``on_trigger(Trigger)`` per trigger."""

    def __init__(
        self,
        settings: WebhookSettings,
        on_trigger: Callable[[Trigger], object],
        *,
        log: _Logger | None = None,
        host: str = WEBHOOK_HOST,
        port: int = WEBHOOK_PORT,
    ) -> None:
        self._settings = settings
        self._on_trigger = on_trigger
        self._log = log or (lambda *_args, **_fields: None)
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            # A slow or stalled client may not hold a handler thread forever.
            timeout = HANDLER_TIMEOUT_SECONDS

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                receiver._handle(self)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                self._reply(404)

            def _reply(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args: Any) -> None:
                return  # the request line carries the secret path

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="org-casdoor-webhook",
            daemon=True,
        )
        self._thread.start()

    def close(self, timeout: float = 2.0) -> bool:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout)
        self._server.server_close()
        return self._thread is None or not self._thread.is_alive()

    def _handle(self, request: Any) -> None:
        path = urllib.parse.urlsplit(request.path).path
        if not hmac.compare_digest(path.encode("utf-8"), self._settings.path.encode("utf-8")):
            request._reply(404)
            return
        token = request.headers.get(TOKEN_HEADER) or ""
        if not hmac.compare_digest(token.encode("utf-8"), self._settings.token.encode("utf-8")):
            self._log("warn", "org", "org.sync.webhook-refused", reason="bad-token")
            request._reply(401)
            return
        try:
            length = int(request.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > _MAX_BODY_BYTES:
            request._reply(413)
            return
        raw = request.rfile.read(length)
        try:
            record: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            record = None
        triggers = triggers_for(record)
        action = record.get("action") if isinstance(record, dict) else None
        self._log(
            "info",
            "org",
            "org.sync.webhook",
            action=action if action in _KNOWN_ACTIONS else "other",
            triggers=[trigger.kind for trigger in triggers],
        )
        try:
            for trigger in triggers:
                self._on_trigger(trigger)
        except ValueError:
            # A name that is not a user identity (e.g. contains ":").
            request._reply(400)
            return
        request._reply(200)
