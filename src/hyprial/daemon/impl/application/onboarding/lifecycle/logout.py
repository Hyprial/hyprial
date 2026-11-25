"""The logout artifact port and the secret-free logout receipt projections.

Decision: identity-http-a5-logout-custody-20261106 (split out for the 1000-line
module budget; behaviour is unchanged).
Decision: daemon-owned logout, 2026-11-09 (intg/gui-setting-fix-109): the A5
supervisor registration above is superseded on the daemon side — the serving
gateway completes a logout itself and never consults a capability again.  This
module keeps the receipt projections; the shell supervisor seam
(``operations.custody``) and its coordinator were deleted in Z-7 A2.
Decision: L1 (owner 2026-11-21): the unreferenced stop-proof and custody
vocabulary (``LogoutStopUnproven``, ``LogoutStopProof``,
``LogoutCustodyCapability``) is deleted with ``DAEMON_STOP_UNPROVEN``.

The projections below stay read-only: no journal write, no credential read and
no invented failure detail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from hyprial.identity import (
    LogoutArtifacts,
    inspect_logout_artifacts,
    remove_logout_artifacts,
)

from hyprial.daemon.impl.request_catalog.onboarding.state import (
    LOGOUT_KIND,
    OperationRecord,
)


class LogoutArtifactPort(Protocol):
    """Secret-free removal port from the identity domain."""

    def inspect(self, home: Path) -> LogoutArtifacts: ...

    def remove(self, home: Path) -> tuple[str, ...]: ...


class _IdentityLogoutArtifacts:
    """Default port: the identity-owned primitive, which never reads the token."""

    def inspect(self, home: Path) -> LogoutArtifacts:
        return inspect_logout_artifacts(home)

    def remove(self, home: Path) -> tuple[str, ...]:
        return remove_logout_artifacts(home)


def _logout_operation(record: OperationRecord) -> dict[str, Any]:
    """Project one logout receipt into the declared polling variant.

    Read-only: no journal write, no credential read, and no invented failure
    detail — the receipt persists no reason, so ``failure`` stays null.
    """

    return {
        "operationId": record.operation_id,
        "kind": LOGOUT_KIND,
        "state": record.state,
        "owner": record.account_owner,
        "receiptId": record.receipt_id,
        "loggedOut": record.state == "completed",
        "removed": list(record.removed),
        "failure": None,
    }


def _logout_result(
    record: OperationRecord, *, removed: tuple[str, ...] | None = None
) -> dict[str, Any]:
    """Project one logout receipt into the declared ``LOGOUT_RESULT`` shape.

    ``removed`` stays empty for ``accepted``; a terminal receipt carries the
    labels the removal primitive actually produced.  ``issuerRevocation`` is
    always ``not-attempted``: account logout is not a browser-session logout and
    no issuer token is revoked or inspected here.
    """

    if record.state == "completed":
        logged_out = True
        next_action = "login"
    elif record.state == "accepted":
        logged_out = False
        next_action = "poll"
    else:
        logged_out = False
        next_action = "inspect"
    labels = record.removed if removed is None else removed
    return {
        "ok": True,
        "operationId": record.operation_id,
        "receiptId": record.receipt_id,
        "state": record.state,
        "loggedOut": logged_out,
        "removed": list(labels),
        "issuerRevocation": "not-attempted",
        "nextAction": next_action,
    }


__all__ = [
    "LogoutArtifactPort",
]
