"""Casdoor account-property client for the invite pickup path (§5.3).

The client's whole job is reading the ``hyprialInvites`` key inside the
signed-in user's ``properties`` (``get_invites``).  It never writes: the
landing page (hyprial.ai/join) is that property's only writer, and the
real deployment refuses property updates from the CLI's device-flow
client (verified 2026-10-04).  Whether this device already acted on an
invite is a device fact kept on the device (see ``pending``).  The access
token is an opaque bearer here — it is sent in the ``Authorization``
header and never printed, logged, or echoed in an error message.

# LAX(tailnet-cutover): ``/api/get-account`` is used as observed on the
# real deployment (2026-10-04) rather than through a client generated
# from the Casdoor OpenAPI.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from hyprial import __version__

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "INVITES_PROPERTY",
    "CasdoorAccountClient",
    "InvitePickupError",
    "Transport",
    "USER_AGENT",
]

INVITES_PROPERTY = "hyprialInvites"
"""The Casdoor user-property key that carries account-bound invites (§5.2)."""

DEFAULT_TIMEOUT_SECONDS = 15.0

USER_AGENT = f"hyprial-cli/{__version__}"
"""Sent on every call: the Cloudflare edge in front of the issuer rejects
urllib's default ``Python-urllib`` agent with 403 / error code 1010."""
"""Whole-request ceiling for one Casdoor call (§C: default urllib, 15s)."""

#: transport(method, url, headers, body) -> (http status, parsed JSON or None).
#: The bearer token rides in ``headers``; a transport must treat it as a
#: secret exactly like the default one does.
Transport = Callable[[str, str, Mapping[str, str], "bytes | None"], "tuple[int, Any]"]


class InvitePickupError(RuntimeError):
    """A typed pickup failure; ``code`` is the CLI-facing error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, Any]:
    """Default stdlib transport: one request, system CA store, 15s ceiling."""

    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method=method
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - issuer URL is profile-owned
            request, timeout=DEFAULT_TIMEOUT_SECONDS
        ) as response:
            status = response.status
            payload = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        payload = error.read()
    except OSError as error:
        raise InvitePickupError(
            "CASDOOR_UNREACHABLE", f"cannot reach the account server: {error}"
        ) from error
    try:
        parsed: Any = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = None
    return status, parsed


def parse_invites_property(account: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The invite list inside one account object; loud on a malformed value."""

    properties = account.get("properties")
    if not isinstance(properties, Mapping):
        return []
    raw = properties.get(INVITES_PROPERTY)
    if raw is None or raw == "":
        return []
    if not isinstance(raw, str):
        raise InvitePickupError(
            "INVITES_MALFORMED",
            f"the {INVITES_PROPERTY} property must be a JSON string; "
            f"got {type(raw).__name__}",
        )
    try:
        items = json.loads(raw)
    except ValueError as error:
        raise InvitePickupError(
            "INVITES_MALFORMED",
            f"the {INVITES_PROPERTY} property is not valid JSON: {error}",
        ) from error
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise InvitePickupError(
            "INVITES_MALFORMED",
            f"the {INVITES_PROPERTY} property must be a JSON array of objects",
        )
    return items


class CasdoorAccountClient:
    """Reads the signed-in user's ``hyprialInvites`` property (never writes)."""

    def __init__(
        self,
        issuer: str,
        access_token: str,
        transport: Transport | None = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._access_token = access_token
        self._transport = transport or _urllib_transport

    def _request(self, method: str, path: str) -> dict[str, Any]:
        headers = {
            # The token never lands in an error message: failures below
            # name only the method, path and status.
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        status, parsed = self._transport(method, f"{self._issuer}{path}", headers, None)
        if status != 200 or not isinstance(parsed, dict):
            raise InvitePickupError(
                "CASDOOR_REQUEST_FAILED",
                f"{method} {path} failed with HTTP status {status}",
            )
        refused = parsed.get("status")
        if refused not in (None, "ok"):
            raise InvitePickupError(
                "CASDOOR_REQUEST_FAILED",
                f"{method} {path} was refused: {parsed.get('msg') or refused}",
            )
        return parsed

    def get_account(self) -> dict[str, Any]:
        """GET ``/api/get-account``; returns the ``data`` account object."""

        parsed = self._request("GET", "/api/get-account")
        data = parsed.get("data")
        if not isinstance(data, dict):
            raise InvitePickupError(
                "ACCOUNT_INVALID", "get-account returned no account object"
            )
        return data

    def get_invites(self) -> list[dict[str, Any]]:
        """The account's invite list (``[]`` when the property is absent)."""

        return parse_invites_property(self.get_account())
