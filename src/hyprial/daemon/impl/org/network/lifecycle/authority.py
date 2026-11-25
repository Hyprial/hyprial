"""Tailnet authority projection and the OC7 M2 roster-write authority gate.

Decision notes (contract OC0 §3.1–§3.3, M2):
- ``authority_devices`` is the read-only per-tailnet authority projection used
  by ``org.list`` (moved from the network service): ``True``/``False`` name an
  observed local/non-local authority, ``None`` degrades per tailnet when the
  sidecar status is unavailable.
- M2 write-authority transfer follows the tailnet authority, which is pinned
  in settings and moved atomically by the fenced offline ``authority-repin``
  (``forwarding/membership/maintenance.py`` with its durable plan, backup and
  ``REVISION_CONFLICT`` handling). The org cell therefore carries no authority
  param of its own; ``require_roster_write_authority`` refuses roster writes
  on any device that is not the current tailnet authority
  (``ORG_ACTION_NOT_AUTHORIZED``), so after a repin the new authority device
  takes over roster writes and the old device is refused, while org-cell
  updates keep their existing ``expect_revision`` fail-closed conflict
  handling (``stale-revision`` surfaces without a write). Orgs without a
  tailnet row (network-isolated homes) have no authority to check and stay
  writable; a sidecar failure fails closed by propagating its named error —
  the roster freezes exactly like an unreachable owner (M2 known limitation).
- A sub-org (OC6) has no tailnet row of its own: the gate resolves its
  effective (root) tailnet and gates on that row; a missing inherited row
  fails closed, since a sub-org always has a real authority. The same gate
  runs for owner-side sub-org member removal (preview, confirm, and
  leave-request processing); a member's own leave is not gated.
- The gate trusts the sidecar's reported ``role`` for the row, not an id
  comparison with the daemon's node id.
"""

from __future__ import annotations

from collections.abc import Callable

from hyprial.daemon.impl.org.network.lifecycle.deletion import MIGRATION_DOC

_Logger = Callable[..., None]


def authority_devices(
    tailnet_call: Callable[[str, dict], dict],
    log: _Logger,
    node_id: str,
) -> dict[str, bool | None]:
    """Project each tailnet's authority onto the local device (read-only)."""

    try:
        status = tailnet_call("membership-status", {})
    except Exception as error:  # noqa: BLE001
        log(
            "warn",
            "org",
            "org.list.authority-unavailable",
            code=getattr(error, "code", type(error).__name__),
        )
        return {}
    rows = status.get("tailnets") if isinstance(status, dict) else None
    if not isinstance(rows, list):
        log("warn", "org", "org.list.authority-unavailable", code="INVALID_STATUS")
        return {}
    authority: dict[str, bool | None] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        tailnet_id = row.get("tailnetId")
        if not isinstance(tailnet_id, str) or not tailnet_id:
            continue
        authority_device_id = row.get("authorityDeviceId")
        authority[tailnet_id] = (
            authority_device_id == node_id
            if isinstance(authority_device_id, str) and authority_device_id
            else None
        )
    return authority


def require_roster_write_authority(
    tailnet_call: Callable[[str, dict], dict],
    org: str,
    tailnet_id: str,
    *,
    inherited: bool = False,
) -> None:
    """Refuse org-cell roster writes unless this device is the tailnet authority."""

    from ..service import OrgNetworkError

    try:
        status = tailnet_call("membership-status", {"tailnetId": tailnet_id})
    except OrgNetworkError as error:
        if error.code != "MEMBERSHIP_NOT_INITIALIZED":
            raise
        status = None
    rows = status.get("tailnets") if isinstance(status, dict) else None
    table = next(
        (
            row
            for row in rows or []
            if isinstance(row, dict) and row.get("tailnetId") == tailnet_id
        ),
        None,
    )
    if table is None:
        if not inherited:
            return
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            f"sub-org {org!r} roster writes need its inherited tailnet "
            f"{tailnet_id!r}, which this device does not hold (see {MIGRATION_DOC})",
        )
    authority_device = table.get("authorityDeviceId")
    if table.get("role") != "authority":
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            f"org {org!r} roster writes stay on the pinned tailnet authority "
            f"device {authority_device!r}; move it with `hyprial org repin` "
            f"(see {MIGRATION_DOC})",
        )


__all__ = [
    "authority_devices",
    "require_roster_write_authority",
]
