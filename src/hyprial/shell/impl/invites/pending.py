"""``hyprial org pending`` — device-side pickup of account-bound invites (§5.3).

The account (Casdoor ``properties.hyprialInvites``) records only the
user's intent — "I accepted this invite on the landing page".  Whether
*this device* has acted on it is a device fact and stays on the device:
the org this node already belongs to (``org.list``) and the orgs it chose
to leave (``leftOrgs``).  The CLI therefore never writes to Casdoor; the
landing page is the property's only writer (Allen, 2026-10-04).

The flow, in order:

1. this device's record (``read_device_record``) — joining needs the
   device key (``DEVICE_NOT_READY``);
2. the login credential + a fresh access token (``NOT_LOGGED_IN`` when
   there is no credential — T11: all of this stays in the shell domain,
   the daemon never sees ``secrets/login.json``);
3. the account's ``hyprialInvites`` list, read-only;
4. this node's org state from the daemon (``org.list``): an invite whose
   space this node already holds, or whose org this node has left, is
   skipped — leaving is never undone by an old accepted invite, only by
   an explicit ``hyprial org join``;
5. every remaining invite the caller accepts (``--accept-all``, or a
   per-invite ``confirm(org, inviter)``) is joined through the daemon
   with the bare ``v1.<payload>`` form;
6. the result carries no payload and no token, only org/inviter/code rows.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyprial.identity import read_device_record

from hyprial.shell.impl.invites.casdoor import (
    CasdoorAccountClient,
    InvitePickupError,
)
from hyprial.shell.impl.login.flow import (
    LoginError,
    read_login_credential,
    refresh_access_token,
)

__all__ = ["InvitePickupError", "run_pending"]

_UNKNOWN = "unknown"
_UNKNOWN_VIEW = {"org": _UNKNOWN, "inviter": _UNKNOWN, "spaceId": _UNKNOWN}


def _invite_view(payload: object) -> dict[str, str]:
    """Org/inviter/space for display and the settled check, decoded leniently.

    The daemon's ``decode_invite`` is the authority (digest, expiry); a
    pickup must still render a row for a payload it cannot read, so
    anything undecodable degrades to ``"unknown"`` instead of failing the
    whole run — the join below lets the daemon give the final verdict.
    """

    if not isinstance(payload, str) or not payload:
        return dict(_UNKNOWN_VIEW)
    try:
        padded = payload + "=" * (-len(payload) % 4)
        record = json.loads(
            base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        )
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return dict(_UNKNOWN_VIEW)
    if not isinstance(record, dict):
        return dict(_UNKNOWN_VIEW)
    org = record.get("org")
    inviter = record.get("inviter")
    space_id = record.get("spaceId")
    return {
        "org": org if isinstance(org, str) and org else _UNKNOWN,
        "inviter": inviter if isinstance(inviter, str) and inviter else _UNKNOWN,
        "spaceId": space_id if isinstance(space_id, str) and space_id else _UNKNOWN,
    }


def _access_token(home: Path, issuer: str, now: Callable[[], datetime]) -> str:
    """A fresh access token for the credential's issuer (U3's refresh)."""

    from hyprial.daemon import resolve_profile  # cross-domain: public face only

    profile, _source = resolve_profile(hyprial_home=home)
    if profile.issuer != issuer:
        raise InvitePickupError(
            "CREDENTIAL_ISSUER_MISMATCH",
            f"the stored credential belongs to issuer {issuer!r} but this "
            f"home's profile issuer is {profile.issuer!r}; re-run hyprial login",
        )
    return refresh_access_token(profile, hyprial_home=home, now=now)


def run_pending(
    home: Path,
    *,
    accept_all: bool,
    confirm: Callable[[str, str], bool] | None,
    ipc_join: Callable[[str], Any],
    ipc_list: Callable[[], Any],
    now: Callable[[], datetime] | None = None,
    client_factory: Callable[[str, str], CasdoorAccountClient] | None = None,
    token_source: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Pick up this account's invites and join each accepted one (§5.3).

    ``ipc_join`` and ``ipc_list`` are the daemon seams: ``ipc_join``
    receives the bare ``v1.<payload>`` form and returns on a completed
    join (any exception it raises is a per-invite failure whose string
    ``code`` is passed through); ``ipc_list`` returns the ``org.list``
    result.  ``confirm``/``accept_all`` decide acceptance: with neither,
    nothing is joined and every invite not already settled on this node
    is listed under ``pending``.  ``client_factory`` and
    ``token_source`` are the test seams — the defaults are the real
    Casdoor client against the credential's issuer and the real U3
    refresh.
    """

    now = now or (lambda: datetime.now(UTC))
    home = Path(home)
    if read_device_record(home) is None:
        raise InvitePickupError(
            "DEVICE_NOT_READY",
            "no device record under the hyprial home; run hyprial login to "
            "provision the device key first",
        )
    try:
        credential = read_login_credential(hyprial_home=home)
        if token_source is None:
            access_token = _access_token(home, credential.issuer, now)
        else:
            access_token = token_source(credential.issuer)
    except LoginError as error:
        raise InvitePickupError(error.code, str(error)) from error
    factory = client_factory or CasdoorAccountClient
    items = factory(credential.issuer, access_token).get_invites()

    held_spaces: set[str] = set()
    left_orgs: set[str] = set()
    warnings: list[str] = []
    if items:
        held_spaces, left_orgs, warnings = _node_org_state(ipc_list)
    joined: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    skipped = 0
    for item in items:
        payload = item.get("payload")
        view = _invite_view(payload)
        if _settled(view, held_spaces, left_orgs):
            skipped += 1
            continue
        accepted = accept_all or (
            confirm is not None and confirm(view["org"], view["inviter"])
        )
        if not accepted or not isinstance(payload, str) or not payload:
            pending.append(
                {
                    "org": view["org"],
                    "inviter": view["inviter"],
                    "acceptedAt": item.get("acceptedAt"),
                }
            )
            continue
        try:
            ipc_join(f"v1.{payload}")
        except Exception as error:  # noqa: BLE001 - one bad invite stops nothing
            code = getattr(error, "code", None)
            failed.append(
                {
                    "org": view["org"],
                    "code": code if isinstance(code, str) else "JOIN_FAILED",
                }
            )
            continue
        if view["spaceId"] != _UNKNOWN:  # one space, accepted twice: join once
            held_spaces.add(view["spaceId"])
        joined.append({"org": view["org"], "inviter": view["inviter"]})

    # The payload and the tokens stay out of the result: org/inviter/code
    # rows only, so this object is safe to print or hand to Desktop.
    return {
        "ok": True,
        "joined": joined,
        "failed": failed,
        "skipped": skipped,
        "pending": pending,
        "warnings": warnings,
    }


def _settled(view: dict[str, str], held: set[str], left: set[str]) -> bool:
    """Already held here, or deliberately left — never auto-joined again."""

    if view["spaceId"] != _UNKNOWN and view["spaceId"] in held:
        return True
    return view["org"] != _UNKNOWN and view["org"] in left


def _node_org_state(
    ipc_list: Callable[[], Any],
) -> tuple[set[str], set[str], list[str]]:
    """(space ids this node holds, orgs it has left, warnings) via ``org.list``.

    An unreachable daemon is not fatal here: nothing is known to be
    settled, so every invite stays a candidate and the join itself reports
    the daemon failure per invite.
    """

    try:
        result = ipc_list()
    except Exception as error:  # noqa: BLE001 - the joins report the real failure
        code = getattr(error, "code", None)
        return set(), set(), [
            f"org state unavailable ({code if isinstance(code, str) else 'ERROR'})"
        ]
    if not isinstance(result, dict):
        return set(), set(), ["org state unavailable (malformed org.list result)"]
    held = {
        row["spaceId"]
        for row in result.get("orgs", [])
        if isinstance(row, dict) and isinstance(row.get("spaceId"), str)
    }
    left = {org for org in result.get("leftOrgs", []) if isinstance(org, str)}
    return held, left, []
