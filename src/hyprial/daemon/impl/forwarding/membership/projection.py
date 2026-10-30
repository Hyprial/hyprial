"""Private sidecar roster projections. No directory or liveness fallback."""
from __future__ import annotations

from typing import Any
import time

from hyprial.identity import read_device_record


def active_members(status: dict[str, Any]):
    for tailnet in status.get("tailnets", []):
        if tailnet.get("memberReady") is not True:
            continue
        for member in tailnet.get("members", []):
            if member.get("state") == "active":
                yield member


def candidates(status: dict[str, Any], *, master_public: str, device_id: str) -> dict[str, str]:
    peers = {}
    for member in active_members(status):
        peer = member["deviceId"]
        if peer == device_id or not member.get("address"):
            continue
        if (master_public, device_id) > (member["masterPublic"], peer):
            peers[peer] = member["address"]
    return peers


def status_for_app(app: Any, *, timeout: float | None = None) -> dict[str, Any]:
    """``timeout`` is the request-path bound; background callers leave it None."""

    supervisor = getattr(app, "_forwarding_supervisor", None)
    if supervisor is None:
        return {"readiness": {"sidecar": False, "control": False, "member": False}, "tailnets": []}
    return supervisor.membership_request("membership-status", {}, timeout=timeout)


def candidates_for_app(app: Any) -> dict[str, str]:
    if getattr(app, "_forwarding_supervisor", None) is None:
        return {}
    try:
        record = read_device_record(app.hyprial_home)
        if record is None:
            return {}
        return candidates(status_for_app(app), master_public=record.client_public,
                          device_id=record.device_id)
    except Exception as error:
        log = getattr(app, "_log", None)
        if log:
            log("warn", "org", "tailnet.projection.failed", code=getattr(error, "code", "MEMBERSHIP_UNAVAILABLE"))
        return {}


def readiness_for_app(app: Any) -> dict[str, Any]:
    supervisor = getattr(app, '_forwarding_supervisor', None)
    observation = getattr(supervisor, 'last_membership_observation', {})
    current = (getattr(supervisor, 'state', None) == 'running'
               and observation.get('sidecarPid') == getattr(supervisor, 'current_pid', None))
    observed_tick = observation.get('observedMonotonic')
    elapsed = (max(0, time.monotonic() - observed_tick)
               if isinstance(observed_tick, (int, float)) else float('inf'))
    observed = observation.get('readiness', {}) if current else {}
    member = any(row.get('memberReady') is True and
                 (row.get('role') == 'authority' or
                  (row.get('leaseRemainingSeconds') or 0) > elapsed)
                 for row in observation.get('tailnets', [])) if current else False
    runtime = getattr(app, '_orgfs_runtime', None)
    return {'sidecar': observed.get('sidecar') is True,
            'control': observed.get('control') is True, 'member': member,
            'zenoh': getattr(app, '_transport', None) is not None,
            'orgfs': getattr(runtime, '_session', None) is not None,
            'zenohAcl': 'not-implemented-0.5.5.1',
            'completeInterOrgIsolation': False,
            'membershipObservedAtMs': observation.get('observedAtMs')}


def public_observation(status: dict[str, Any]) -> dict[str, Any]:
    """A diagnostic copy: never publish roster addresses, keys or invite tokens."""
    fields = ('tailnetId', 'role', 'revision', 'authorityDeviceId', 'memberReady',
              'leaseRemainingSeconds', 'activeCount', 'tombstoneCount', 'code')
    return {'readiness': dict(status.get('readiness', {})),
            'tailnets': [{key: row.get(key) for key in fields}
                        for row in status.get('tailnets', [])]}


def owner_for_key(status: dict[str, Any], key: str, server_public: str) -> str | None:
    members = list(active_members(status))
    # A malformed observation must not turn a master/server key into a business
    # credential, even though the owning Go store also rejects such aliases.
    if not key or any(key in (row.get('masterPublic'), row.get('serverPublic')) for row in members):
        return None
    identities = {(row["owner"], row["deviceId"]) for row in members
                  if row.get("clientPublicByServer", {}).get(server_public) == key}
    return next(iter(identities))[0] if len(identities) == 1 else None
