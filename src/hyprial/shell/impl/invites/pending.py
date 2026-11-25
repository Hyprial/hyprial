"""``hyprial org pending`` / login pickup — the request adapter (§3 O2c).

This module no longer decides anything.  Three facts used to live here and
now have one owner each:

* **whether an invite still needs an answer** — the daemon
  (``org.invites.list``, ``daemon/impl/org/membership/invites``);
* **joining one confirmed invite** — the daemon (``org.invites.accept``,
  which settles exactly like ``org.join``);
* **reading the account** — here, because the credential does: the access
  token comes from ``secrets/login.json`` and the daemon must never read it
  (the T11 anchor in ``shell/impl/login/flow.py``; the daemon's Casdoor
  service user cannot read user properties either, only group membership).

What is left is a transport adapter: read the device record, refresh the
token, read ``hyprialInvites``, hand the rows over, ask the operator, and
report what came back.  The payload — a bearer secret — never enters a
result object, and a decline never touches the account property: declining
is *not* withdrawing the invite.

D6 is gone with the old code: nothing accepts every invite silently.  Only
an explicit ``--accept-all`` (or one "yes" per invite on a TTY) joins.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyprial.identity import invite_link, invite_view, read_device_record

from hyprial.shell.impl.invites.casdoor import (
    CasdoorAccountClient,
    InvitePickupError,
)
from hyprial.shell.impl.login.flow import (
    LoginError,
    read_login_credential,
    refresh_access_token,
)

__all__ = ["InvitePickupError", "pickup_after_login", "run_pending"]

_UNKNOWN = "unknown"

#: ``request(method, params)`` -> the daemon's answer.  The login path and
#: ``org pending`` both pass ``services.runtime.request``.
Request = Callable[[str, dict[str, Any]], Any]

_DISPLAY_FIELDS = ("inviteId", "org", "inviter", "acceptedAt")

_DECLINED_NOTE = (
    "a declined invite is declined for this device only; the invite stays "
    "on the account until it is withdrawn there"
)


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


def _display(row: Mapping[str, Any], *, progress: bool = True) -> dict[str, Any]:
    """The fields a result may carry: never the payload."""

    return {field: row.get(field) for field in _DISPLAY_FIELDS} | (
        _progress(row) if progress else {}
    )


def _progress(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or value.get("status") != "partial":
        return {}
    step = value.get("missingStep")
    if step not in {
        "tailnet-redeemed", "tailnet-announced", "connect-inviter", "join-org",
        "directory-committed", "acl-applied", "space-bound", "register-device",
        "connect-members", "accept-in-flight", "join", "started",
    }:
        return {"status": "partial"}
    return {"status": "partial", "missingStep": step}


def _account_rows(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """(daemon rows, unusable invites, invite id -> payload) for one snapshot."""

    rows: list[dict[str, Any]] = []
    unusable: list[dict[str, Any]] = []
    payloads: dict[str, str] = {}
    for item in items:
        payload = item.get("payload")
        accepted_at = item.get("acceptedAt")
        stamp = accepted_at if isinstance(accepted_at, str) and accepted_at else None
        if not isinstance(payload, str) or not payload:
            # No payload, no link: report the row instead of sending the
            # daemon something it must refuse for the whole snapshot.
            view = invite_view(payload, stamp)
            unusable.append(_display(view.row()))
            continue
        view = invite_view(payload, stamp)
        payloads.setdefault(view.invite_id, payload)
        rows.append({"payload": payload, "acceptedAt": stamp})
    return rows, unusable, payloads


def run_pending(
    home: Path,
    *,
    accept_all: bool,
    confirm: Callable[[str, str], bool] | None,
    request: Request,
    now: Callable[[], datetime] | None = None,
    client_factory: Callable[[str, str], CasdoorAccountClient] | None = None,
    token_source: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Read this account's invites and let the daemon act on the answered ones.

    ``accept_all`` joins every pending invite; ``confirm(org, inviter)``
    decides one at a time; with neither, nothing is joined and every pending
    invite is reported back (A2: a non-TTY or ``--json`` run joins nothing).
    ``client_factory`` and ``token_source`` are the test seams for the
    Casdoor read and the U3 refresh.
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

    rows, unusable, payloads = _account_rows(items)
    listed = request("org.invites.list", {"invites": rows})
    pending_rows = [
        row for row in listed.get("pending", []) if isinstance(row, Mapping)
    ]
    skipped = _count(listed.get("settled")) + _count(listed.get("duplicates"))
    warnings = [item for item in listed.get("warnings", []) if isinstance(item, str)]

    joined: list[dict[str, Any]] = []
    declined: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for row in pending_rows:
        org = str(row.get("org", _UNKNOWN))
        inviter = str(row.get("inviter", _UNKNOWN))
        if accept_all:
            decision: bool | None = True
        elif confirm is None:
            decision = None
        else:
            decision = confirm(org, inviter)
        if decision is None:
            pending.append(_display(row))
            continue
        if not decision:
            declined.append(_display(row))
            continue
        invite_id = str(row.get("inviteId", ""))
        payload = payloads.get(invite_id)
        if payload is None:  # unreachable in practice: the id came from a row
            failed.append({**_display(row), "code": "INVITE_MALFORMED"})
            continue
        try:
            accepted = request(
                "org.invites.accept",
                {"inviteId": invite_id, "link": invite_link(payload)},
            )
        except Exception as error:  # noqa: BLE001 - one bad invite stops nothing
            code = getattr(error, "code", None)
            failed.append(
                {
                    **_display(row),
                    "code": code if isinstance(code, str) else "JOIN_FAILED",
                    **_progress(getattr(error, "data", None)),
                }
            )
            continue
        joined.append(
            {
                **_display(row, progress=False),
                "alreadyMember": bool(accepted.get("alreadyMember")),
            }
        )

    # No payload and no token: display rows and codes only, so this object
    # is safe to print, hand to Desktop, or write into the login result.
    return {
        "ok": True,
        "joined": joined,
        "declined": declined,
        "pending": pending,
        "failed": failed,
        "unusable": unusable,
        "skipped": skipped,
        "warnings": warnings,
        "declinedNote": _DECLINED_NOTE,
    }


def pickup_after_login(
    home: Path,
    *,
    request: Request,
    confirm: Callable[[str, str], bool] | None,
) -> dict[str, Any]:
    """One best-effort pickup after a verified login (§5.3, D12, A2).

    Any failure — no device record, a refused refresh, an unreachable
    account server, an unreachable daemon — is reported in the login
    result's ``pending`` field and never changes the login or its exit code.
    """

    try:
        outcome = run_pending(
            home,
            accept_all=False,
            confirm=confirm,
            request=request,
        )
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - post-commit only
        return {
            "status": "failed",
            "code": str(getattr(error, "code", "INVITE_PICKUP_FAILED")),
            # Transport errors can contain URLs, bearer payloads or response bodies.
            "message": "Invite pickup did not complete; retry with hyprial org pending.",
        }
    return {
        "status": "ok",
        "joined": outcome["joined"],
        "declined": outcome["declined"],
        "pending": outcome["pending"],
        "failed": outcome["failed"],
        "unusable": outcome["unusable"],
        "skipped": outcome["skipped"],
        "warnings": outcome["warnings"],
    }


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
