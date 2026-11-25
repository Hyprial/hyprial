"""OC7 org deletion lifecycle (W3/W9/D1 + contract OC0 §2.2, §8.1).

Decision notes:
- ``org.delete`` is a two-step confirmed operation modelled on the member
  removal journal: the first call freezes an ``operationId`` bound to the exact
  device set and tailnet revision in a durable journal; only the confirmation
  carrying that id executes (``REMOVAL_CONFIRMATION_STALE`` otherwise).
- Deletion runs only on the pinned tailnet authority device: preview and
  confirmation both refuse any other device with ``AUTHORITY_REQUIRED`` before
  anything durable is written, so no device can tombstone an org whose tailnet
  it cannot shut down.
- Nothing irreversible happens on a stale binding: an unconfirmed preview is
  rebound whenever the current revision, authority or device set differs, and
  the confirmation re-reads the tailnet and compares revision, authority and
  device set before the OrgFS write. A mismatch records ``revision-conflict``
  and fails closed with ``REVISION_CONFLICT``; only a fresh preview rebinds.
- Execution is OrgFS-first: ``directory/org.json`` is tombstoned with the
  operation id (kept when the marker already exists) and OrgFS members and
  member-written leave requests are deliberately kept so the marker can reach
  every member; then the sidecar ``tailnet-shutdown`` closes the tailnet as a
  terminal revoked roster; pending removal journals are forgotten and a local
  deleted-org record is written.
- A conflict that still lands between the pre-check and the shutdown keeps
  the committed tombstone in the journal; the authority's fresh preview of an
  org whose marker is deleted but whose tailnet is open rebinds and finishes
  it, which also covers markers written by an older build on another device.
- The authority keeps the closed tailnet instead of forgetting it in the same
  call: OrgFS sync rides tailnet admission (contract §3.7), so after shutdown
  the closed roster is the only channel left for slow or offline members.
  ``roster-fetch`` on a closed tailnet stays open to former members. The
  owner forgets it later with ``org leave``.
- ``leave`` on an org whose marker is deleted or whose local tailnet is closed
  reduces to local cleanup plus ``tailnet-forget`` with the ``orgDeleted``
  proof, except on the authority of a still-open tailnet, which is refused
  with ``LAST_AUTHORITY_REFUSED`` so it finishes the deletion instead. Its
  result stays inside the published ``org.leave`` schema: ``phase:
  "complete"`` with ``reason: "org-deleted"``; a failed forget is logged.
- Resurrection is refused once a deletion journal holds a committed OrgFS
  tombstone, or a deleted-org record or the ``deleted`` marker exists
  (``ORG_ALREADY_DELETED``). Journals without a committed tombstone (a
  declined, refused or conflicting preview) are dropped by ``org create``.
- Errors carry no wire data: the org methods declare these codes without a
  data schema, so the message names the org and the migration guide.
- "No tailnet" is accepted only from the sidecar itself or a network-isolated
  home: the tailnet row comes from the unfiltered ``membership-status`` (a
  filtered status answers ``MEMBERSHIP_NOT_INITIALIZED`` for an unknown
  tailnet, the same code as a missing sidecar), and any sidecar failure
  propagates, so delete and the deleted-org leave fail closed before the
  tombstone or ``left-orgs`` write.
- Authority is the sidecar's reported ``role``, never an id comparison with
  the daemon's node id.
- A ``complete`` journal without a recorded shutdown is re-previewed while
  the local tailnet is still open (the tombstone receipt carries over), which
  finishes deletions an earlier build completed with a missing sidecar.
- Sub-orgs (OC6, contract §3.8) are deleted leaf-first: preview and
  confirmation of an org refuse with ``OPERATION_CONFLICT`` (message names the
  blockers) while any direct sub-org is not ``deleted``; a sub-org created
  after the preview is caught at confirmation, before the tombstone.
- A sub-org inherits the root tailnet, so its deletion never sends
  ``tailnet-shutdown`` or ``tailnet-forget``: it is gated on the inherited
  tailnet's authority, binds no revision or device set (the root tailnet
  moves with unrelated activity), tombstones the marker, then revokes every
  non-owner member from the sub-org's directory and ACL spaces (the J25
  isolation boundary) in place of the shutdown. Results report
  ``tailnetPhase: "inherited"`` and ``tailnetClosed: false``. A sub-org whose
  marker is deleted but which this home never finished deleting is previewed
  and finished rather than refused. Helpers and the blocker rules live in
  ``sub_orgs.py``.
"""

from __future__ import annotations

from hyprial.kernel import ipc_errors

import json
import uuid
from pathlib import Path
from typing import Any

from hyprial.kernel import atomic_json_write

from .sub_orgs import (
    clear_sub_org_removals,
    inherited_tailnet,
    revoke_sub_org_members,
)

DELETION_JOURNAL_RELPATH = "state/org/deletions/{org}.json"
DELETED_ORG_RECORD_RELPATH = "state/org/deleted-orgs/{org}.json"
MIGRATION_DOC = "docs/guides/migration-oc7-org-lifecycle.md"
_PREVIEW_PHASES = ("pending-confirmation", "revision-conflict")


def deletion_journal_path(home: Path, org: str) -> Path:
    return Path(home) / DELETION_JOURNAL_RELPATH.format(org=org)


def deleted_org_record_path(home: Path, org: str) -> Path:
    return Path(home) / DELETED_ORG_RECORD_RELPATH.format(org=org)


def _durable_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json_write(path, payload)


def _read_journal(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _tombstone_committed(operation: dict[str, Any] | None) -> bool:
    return operation is not None and operation.get("orgfsReceipt") == "committed"


def _refuse_live_sub_orgs(service: Any, org: str, space_id: str) -> None:
    from ..service import OrgNetworkError
    from .sub_orgs import live_sub_orgs

    blockers = live_sub_orgs(service, org, space_id)
    if blockers:
        raise OrgNetworkError(
            "OPERATION_CONFLICT",
            f"org {org!r} still has sub-orgs {', '.join(blockers)}; delete them "
            f"first (leaf-first, see {MIGRATION_DOC})",
        )


def _tailnet_table(service: Any, space_id: str) -> dict[str, Any] | None:
    if service._network_isolated:
        return None
    status = service._tailnet_call("membership-status", {})
    rows = status.get("tailnets") if isinstance(status, dict) else None
    for row in rows or []:
        if isinstance(row, dict) and row.get("tailnetId") == space_id:
            return row
    return None


def _binding(tailnet: dict[str, Any] | None) -> dict[str, Any]:
    if tailnet is None:
        return {"deviceIds": [], "expectedRevision": None, "authorityDeviceId": None}
    authority = tailnet.get("authorityDeviceId")
    return {
        "deviceIds": sorted(
            row["deviceId"]
            for row in tailnet.get("members") or []
            if isinstance(row, dict)
            and row.get("state") == "active"
            and row.get("deviceId") != authority
        ),
        "expectedRevision": tailnet.get("revision"),
        "authorityDeviceId": authority,
    }


def _same_binding(operation: dict[str, Any], binding: dict[str, Any]) -> bool:
    return all(operation.get(field) == value for field, value in binding.items())


def _require_local_authority(
    service: Any,
    org: str,
    tailnet: dict[str, Any] | None,
    inherited: str | None = None,
) -> None:
    from ..service import OrgNetworkError

    if tailnet is None and inherited is not None and not service._network_isolated:
        raise OrgNetworkError(
            "AUTHORITY_REQUIRED",
            f"sub-org {org!r} can only be deleted on the authority device of its "
            f"inherited tailnet {inherited!r}, which this device does not hold "
            f"(see {MIGRATION_DOC})",
        )
    if tailnet is None or tailnet.get("role") == "authority":
        return
    raise OrgNetworkError(
        "AUTHORITY_REQUIRED",
        f"org {org!r} can only be deleted on its tailnet authority device "
        f"{tailnet.get('authorityDeviceId')!r}; run `hyprial org delete` there "
        f"(see {MIGRATION_DOC})",
    )


def _public(operation: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "org": operation["org"],
        "spaceId": operation["spaceId"],
        "operationId": operation["operationId"],
        "phase": operation["phase"],
        "deviceIds": list(operation.get("deviceIds") or []),
        "confirmed": bool(operation.get("confirmed")),
        "irreversible": True,
        "migrationDoc": MIGRATION_DOC,
    }
    if operation.get("expectedRevision") is not None:
        result["expectedRevision"] = operation["expectedRevision"]
    if operation.get("authorityDeviceId") is not None:
        result["authorityDeviceId"] = operation["authorityDeviceId"]
    result["orgfsMarked"] = _tombstone_committed(operation)
    shutdown = operation.get("tailnetShutdown")
    if operation.get("inheritedTailnet") is not None:
        result["tailnetPhase"] = "inherited"
        result["tailnetClosed"] = False
    elif operation.get("expectedRevision") is None:
        result["tailnetPhase"] = "absent"
        result["tailnetClosed"] = False
    else:
        result["tailnetPhase"] = "shutdown" if shutdown else None
        result["tailnetClosed"] = bool(shutdown)
    result["tailnetForgotten"] = False
    record = operation.get("deletedRecord")
    if record is not None:
        result["deletedRecord"] = record
    return result


def _forget_tailnet(service: Any, space_id: str, operation_id: str) -> dict[str, Any]:
    from ..service import OrgNetworkError

    try:
        return service._tailnet_call(
            "tailnet-forget",
            {
                "operationId": f"forget-{operation_id}",
                "tailnetId": space_id,
                "orgDeleted": True,
            },
        )
    except OrgNetworkError as error:
        if error.code == "MEMBERSHIP_NOT_INITIALIZED":
            return {"tailnetId": space_id, "forgotten": True, "absent": True}
        raise


def _validate_confirmation(
    org: str, operation: dict[str, Any] | None, operation_id: str | None
) -> dict[str, Any]:
    from ..service import OrgNetworkError

    if (
        operation is None
        or operation.get("operationId") != operation_id
        or operation.get("org") != org
    ):
        raise OrgNetworkError(
            "REMOVAL_CONFIRMATION_STALE",
            f"Deletion confirmation for org {org!r} is stale; re-run the preview "
            "and confirm its exact operation.",
        )
    if operation.get("phase") == "complete":
        raise OrgNetworkError(
            "REMOVAL_CONFIRMATION_STALE",
            f"The deletion of org {org!r} already completed.",
        )
    if operation.get("phase") == "revision-conflict":
        raise OrgNetworkError(
            "REVISION_CONFLICT",
            f"The tailnet of org {org!r} moved after the preview; re-run the "
            "preview to rebind the current revision.",
        )
    return operation


def _record_conflict(path: Path, operation: dict[str, Any], org: str) -> None:
    from ..service import OrgNetworkError

    operation["phase"] = "revision-conflict"
    operation["confirmed"] = False
    _durable_write(path, operation)
    raise OrgNetworkError(
        "REVISION_CONFLICT",
        f"The tailnet of org {org!r} moved after the preview; re-run the "
        "preview to rebind the current revision.",
    )


def org_delete(
    service: Any,
    org: str,
    *,
    confirmed: bool = False,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Preview, or validate the confirmation of, the deletion of *org*."""

    from ..service import OrgNetworkError

    space = service._require_space(org)
    path = deletion_journal_path(service._home, org)
    operation = _read_journal(path)
    marker_deleted = service._org_state(org) == "deleted"
    if confirmed:
        operation = _validate_confirmation(org, operation, operation_id)
    elif operation is not None and operation.get("phase") == "complete":
        if not marker_deleted:
            operation = None
        elif operation.get("tailnetShutdown"):
            return _public(operation)
    if not _tombstone_committed(operation):
        _refuse_live_sub_orgs(service, org, space.space_id)
    inherited = inherited_tailnet(service, org, space.space_id)
    tailnet = _tailnet_table(service, inherited or space.space_id)
    tailnet_open = (
        inherited is None and tailnet is not None and not tailnet.get("closed")
    )
    if operation is not None and operation.get("phase") == "complete" and not confirmed:
        if not tailnet_open:
            return _public(operation)
        operation = {**operation, "phase": "revision-conflict"}
    _require_local_authority(service, org, tailnet, inherited)
    binding = _binding(tailnet if inherited is None else None)
    if inherited is not None and tailnet is not None:
        binding["authorityDeviceId"] = tailnet.get("authorityDeviceId")
    if confirmed:
        assert operation is not None
        if operation["phase"] in ("pending-confirmation", "orgfs-pending") and (
            not _same_binding(operation, binding)
        ):
            _record_conflict(path, operation, org)
        return operation
    if operation is not None and operation.get("phase") not in _PREVIEW_PHASES:
        return _public(operation)
    if (
        operation is not None
        and operation.get("phase") == "pending-confirmation"
        and _same_binding(operation, binding)
    ):
        return _public(operation)
    if (
        marker_deleted
        and not tailnet_open
        and not _tombstone_committed(operation)
        and (inherited is None or deleted_org_record_path(service._home, org).exists())
    ):
        raise OrgNetworkError(
            ipc_errors.ORG_ALREADY_DELETED,
            f"org {org!r} is already marked deleted; follow {MIGRATION_DOC}",
        )
    fresh: dict[str, Any] = {
        "operationId": uuid.uuid4().hex,
        "org": org,
        "spaceId": space.space_id,
        **binding,
        "role": tailnet.get("role") if tailnet else None,
        "phase": "pending-confirmation",
        "confirmed": False,
        "reason": "org-delete",
        "createdAt": service._now().isoformat(),
    }
    if inherited is not None:
        fresh["inheritedTailnet"] = inherited
    if _tombstone_committed(operation):
        fresh["orgfsReceipt"] = "committed"
    _durable_write(path, fresh)
    return _public(fresh)


def execute_confirmed_deletion(
    service: Any, org: str, operation: dict[str, Any]
) -> dict[str, Any]:
    """Run the journaled deletion phases for an already-confirmed operation."""

    from ..service import OrgNetworkError
    from ..state import finish_org_create
    from hyprial.daemon.impl.forwarding.membership.removal import RemovalJournal

    path = deletion_journal_path(service._home, org)
    space_id = operation["spaceId"]
    operation["confirmed"] = True
    if operation["phase"] == "pending-confirmation":
        operation["phase"] = "orgfs-pending"
        _durable_write(path, operation)
    if operation["phase"] == "orgfs-pending":
        if service._org_state(org) != "deleted":
            service._store.put_org_meta(
                org,
                {
                    "state": "deleted",
                    "deletedAt": service._now().isoformat(),
                    "deleteOperationId": operation["operationId"],
                },
            )
        finish_org_create(service._home, org)
        operation["orgfsReceipt"] = "committed"
        operation["phase"] = "tailnet-pending"
        _durable_write(path, operation)
    if operation["phase"] == "tailnet-pending":
        if operation.get("inheritedTailnet") is not None:
            operation["tailnetShutdown"] = None
            operation["revokedMembers"] = revoke_sub_org_members(
                service, org, space_id
            )
        elif operation.get("expectedRevision") is not None:
            try:
                operation["tailnetShutdown"] = service._tailnet_call(
                    "tailnet-shutdown",
                    {
                        "operationId": operation["operationId"],
                        "tailnetId": space_id,
                        "expectedRevision": operation["expectedRevision"],
                        "confirmed": True,
                        "reason": "org-delete",
                    },
                )
            except OrgNetworkError as error:
                if error.code == "REVISION_CONFLICT":
                    operation["phase"] = "revision-conflict"
                    operation["confirmed"] = False
                    _durable_write(path, operation)
                raise
        else:
            operation["tailnetShutdown"] = None
        operation["phase"] = "cleanup-pending"
        _durable_write(path, operation)
    if operation["phase"] == "cleanup-pending":
        if operation.get("inheritedTailnet") is not None:
            clear_sub_org_removals(service._home, org)
        else:
            RemovalJournal(
                service._home,
                service._tailnet_raw_call,
                local_device=service._node_id,
                operator=service._owner,
            ).forget_tailnet(space_id)
        record_path = deleted_org_record_path(service._home, org)
        _durable_write(
            record_path,
            {
                "org": org,
                "spaceId": space_id,
                "operationId": operation["operationId"],
                "deletedAt": service._now().isoformat(),
                "deviceIds": list(operation.get("deviceIds") or []),
            },
        )
        operation["deletedRecord"] = str(record_path)
        operation["phase"] = "complete"
        _durable_write(path, operation)
        service._log(
            "info",
            "org",
            "org.deleted",
            org=org,
            spaceId=space_id,
            operationId=operation["operationId"],
        )
    return _public(operation)


def delete_org(
    service: Any,
    org: str,
    *,
    confirmed: bool = False,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Entry point wired into ``OrgNetworkService.delete``."""

    if not confirmed:
        return org_delete(service, org, confirmed=False)
    operation = org_delete(
        service, org, confirmed=True, operation_id=operation_id or ""
    )
    return execute_confirmed_deletion(service, org, operation)


def depart_deleted_org(service: Any, org: str, space: Any) -> dict[str, Any] | None:
    """Local cleanup when the ``deleted`` marker or a closed tailnet is observed.

    Returns ``None`` when neither is observed, so the caller takes the normal
    removal path.
    """

    from ..service import OrgNetworkError
    from ..state import add_left_org

    marker_deleted = service._org_state(org) == "deleted"
    try:
        tailnet = _tailnet_table(service, space.space_id)
    except OrgNetworkError:
        if not marker_deleted:
            return None
        raise
    closed = bool(tailnet and tailnet.get("closed"))
    if not marker_deleted and not closed:
        return None
    if (
        tailnet is not None
        and not closed
        and tailnet.get("role") == "authority"
    ):
        raise OrgNetworkError(
            "LAST_AUTHORITY_REFUSED",
            f"this device is the authority of org {org!r}'s still-open tailnet; "
            f"finish `hyprial org delete {org}` here before leaving "
            f"(see {MIGRATION_DOC})",
        )
    add_left_org(service._home, org)
    if inherited_tailnet(service, org, space.space_id) is not None:
        return {
            "org": org,
            "spaceId": space.space_id,
            "phase": "complete",
            "reason": "org-deleted",
        }
    try:
        _forget_tailnet(service, space.space_id, f"leave-{uuid.uuid4().hex}")
    except OrgNetworkError as error:
        service._log(
            "warn",
            "org",
            "org.leave.forget-failed",
            org=org,
            code=error.code,
        )
    return {
        "org": org,
        "spaceId": space.space_id,
        "phase": "complete",
        "reason": "org-deleted",
    }


def refuse_resurrected_org(service: Any, org: str) -> None:
    """Refuse ``org create`` for a name this home already deleted (D1 §5.3.6)."""

    from ..service import OrgNetworkError

    home = service._home
    journal_path = deletion_journal_path(home, org)
    journal = _read_journal(journal_path)
    if journal is not None and not _tombstone_committed(journal):
        journal_path.unlink(missing_ok=True)
        journal = None
    if journal is not None:
        evidence = "deletion journal"
    elif deleted_org_record_path(home, org).exists():
        evidence = "deleted-org record"
    elif service._org_state(org) == "deleted":
        evidence = "OrgFS deleted marker"
    else:
        return
    raise OrgNetworkError(
        ipc_errors.ORG_ALREADY_DELETED,
        f"org {org!r} was deleted on this home ({evidence}); pick a new org "
        f"name or follow {MIGRATION_DOC}",
    )


__all__ = [
    "DELETED_ORG_RECORD_RELPATH",
    "DELETION_JOURNAL_RELPATH",
    "MIGRATION_DOC",
    "deletion_journal_path",
    "deleted_org_record_path",
    "delete_org",
    "depart_deleted_org",
    "refuse_resurrected_org",
]
