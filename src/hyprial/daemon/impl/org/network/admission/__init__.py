"""Derive inbound Tailcat admission from authenticated organization directories."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..binding import resolve_org_directory_space

_CLIENT_KEY = re.compile(r"nodekey:[0-9a-f]{64}\Z")


def _directory_keys(service: Any) -> tuple[str, ...]:
    """Only current members' validated device rows confer admission.

    The directory store verifies protected document IDs, authors and key
    conflicts. Bootstrap addresses and mesh presence are not authorities.
    Any unreadable organization makes this snapshot empty (fail closed).
    """
    claims: dict[str, set[tuple[str, str]]] = {}
    own = (service._owner.removeprefix("user:"), service._node_id)
    local_server_public = service._require_device_record().server_public
    for org in service._orgs():
        space, reason = resolve_org_directory_space(service._fs, org)
        if space is None:
            raise ValueError(reason or "unavailable organization directory")
        members = {
            member.user.removeprefix("user:")
            for member in service._fs.members(space.space_id)
        }
        if own[0] not in members:
            continue
        for device in service._store.list_devices(org):
            identity = (device.owner.removeprefix("user:"), device.device_id)
            derived = device.client_public_by_server
            if not isinstance(derived, Mapping):
                continue
            values = tuple(derived.values())
            if (
                not all(isinstance(value, str) and _CLIENT_KEY.fullmatch(value)
                        for value in values)
                or len(set(values)) != len(values)
                or device.client_public in values
            ):
                continue
            key = derived.get(local_server_public)
            if (
                identity[0] not in members
                or not isinstance(key, str)
                or not _CLIENT_KEY.fullmatch(key)
            ):
                continue
            claims.setdefault(key, set()).add(identity)
    return tuple(sorted(
        key for key, identities in claims.items()
        if len(identities) == 1 and own not in identities
    ))


def sync_directory_admission_for_app(app: Any) -> None:
    """Replace, never broaden, the running sidecar's inbound allow-set.

    Cache only acknowledged sets, scoped to the child PID. A relaunched child
    starts empty and must receive the same set again. A failed replacement
    closes the child through its owner so stale admission cannot survive;
    the existing bounded supervisor recovery owns the subsequent relaunch.
    """
    supervisor = getattr(app, "_forwarding_supervisor", None)
    if supervisor is None or supervisor.current_pid is None:
        return
    from ..ipc import _build_service

    try:
        keys = _directory_keys(_build_service(app))
        app._org_admission_directory_error = None
    except Exception as error:  # directory failures must revoke, not retain
        keys = ()
        error_type = type(error).__name__
        if getattr(app, "_org_admission_directory_error", None) != error_type:
            app._log("warn", "org", "org.admission.directory_failed",
                     errorType=error_type)
        app._org_admission_directory_error = error_type
    snapshot = (supervisor.current_pid, keys)
    if getattr(app, "_org_admission_snapshot", None) == snapshot:
        return
    try:
        result = supervisor.allow(keys, False)
        if result.get("allowAny") is not False or result.get("count") != len(keys):
            raise ValueError("sidecar did not acknowledge the exact allow-set")
    except Exception as error:
        app._org_admission_snapshot = None
        app._log("error", "org", "org.admission.apply_failed",
                 errorType=type(error).__name__)
        backend = supervisor.endpoints()
        if backend is not None:
            backend.controller.close()
        return
    app._org_admission_snapshot = snapshot
    app._log("info", "org", "org.admission.synced",
             peerKeys=len(keys), sidecarPid=snapshot[0])
