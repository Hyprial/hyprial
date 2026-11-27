# Decision notes (service grant S3 fix, 2026-11-26, critic S5):
# - The owner-approved migration is "the org owner's daemon runs once after the
#   upgrade". The periodic device-row publish tick therefore creates missing
#   owner directories (directory/service-grants among them) for every org whose
#   space this daemon's owner owns; members cannot create them.
# - Each org is checked until one success in this process, then never again.
#   A failure is logged by error type only and retried on the next tick; it
#   never raises into the tick, so the device row is always published.
"""Backfill owner-created org directory roots on the publish tick."""

from __future__ import annotations

from typing import Any, Iterable

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.identity import directory_owner_principal, org_from_space_name

_READY_ATTRIBUTE = "_org_directory_roots_ready"


def ensure_owner_roots_for_app(app: Any, orgs: Iterable[str]) -> dict[str, list[str]]:
    """Create missing owner directories for owned orgs; never raises.

    Returns the orgs created in this call and the orgs that failed, for logs
    and tests. An org already confirmed in this process is skipped.
    """

    from hyprial.daemon.impl.org.network.ipc import invalid_device_diagnostics

    ensured: list[str] = []
    failed: list[str] = []
    try:
        ready: set[str] = getattr(app, _READY_ATTRIBUTE, None) or set()
        setattr(app, _READY_ATTRIBUTE, ready)
        pending = [org for org in orgs if org not in ready]
        if not pending:
            return {"ensured": ensured, "failed": failed}
        fs = app._require_orgfs_runtime().facade
        caller = directory_owner_principal(app.owner)
        owned = {
            org_from_space_name(space.name)
            for space in fs.spaces()
            if directory_owner_principal(space.owner) == caller
        }
        store = OrgFsDirectoryStore(fs, invalid_devices=invalid_device_diagnostics(app))
    except Exception as error:  # noqa: BLE001 - the publish tick must not depend on it
        _warn(app, None, error)
        return {"ensured": ensured, "failed": failed}
    for org in pending:
        if org not in owned:
            continue
        try:
            store.ensure_org_directory_roots(org)
        except Exception as error:  # noqa: BLE001 - one org must not stop the others
            failed.append(org)
            _warn(app, org, error)
            continue
        ready.add(org)
        ensured.append(org)
    return {"ensured": ensured, "failed": failed}


def _warn(app: Any, org: str | None, error: Exception) -> None:
    try:
        app._log(
            "warn",
            "org",
            "org.directory_roots.failed",
            org=org,
            errorType=type(error).__name__,
            code=getattr(error, "code", None),
        )
    except Exception:  # noqa: BLE001 - logging must never mask the tick
        pass


__all__ = ["ensure_owner_roots_for_app"]
