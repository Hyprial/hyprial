"""Local leave bookkeeping: the orgs this node has left (§5, task D).

OrgFS spaces are shared state — a node that leaves cannot un-join the
space unilaterally, it can only stop counting it.  That local decision
lives in ``state/tailcat/left-orgs.json`` inside the daemon's home: a
JSON array of org names.  The service's org listing subtracts it, so a
left org disappears from ``orgs()``/``list()``/``network()`` on this node
while the space itself stays for the remaining members.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from collections.abc import Iterable

from hyprial.identity import org_space_name
from hyprial.kernel import atomic_json_write

__all__ = [
    "BOOTSTRAP_PEERS_RELPATH",
    "LEFT_ORGS_RELPATH",
    "ORG_ACL_GRANTS_RELPATH",
    "ORG_CREATE_INTENTS_RELPATH",
    "add_bootstrap_peer",
    "add_left_org",
    "add_org_acl_grant",
    "begin_org_create",
    "clear_left_org",
    "finish_org_create",
    "org_create_intent",
    "left_orgs_path",
    "org_acl_grants_path",
    "read_bootstrap_peers",
    "read_left_orgs",
    "read_org_acl_grants",
    "remove_bootstrap_peers",
    "remove_org_acl_grant",
    "update_org_create",
]

#: JSON array of org names, relative to the daemon's home.
LEFT_ORGS_RELPATH = "state/tailcat/left-orgs.json"

#: Owner-node ACL entry authorizations (design-org-orgfs-usage §8.3):
#: ``{org: {agent URI: {"grantedAtMs": int, "untilMs": int | null}}}``.
#: Local by design — the entry gate runs only on the owner node, so the
#: grants never enter orgfs and never synchronize.  They are org state,
#: not tailcat state, so they live under ``state/org/``.
ORG_ACL_GRANTS_RELPATH = "state/org/org-acl-grants.json"

#: Durable creator-owned recovery records for multi-step ``org.create``.
ORG_CREATE_INTENTS_RELPATH = "state/org/create-intents.json"

#: JSON object ``owner/deviceId -> {"inviter", "deviceId", ...}``: peers the
#: directory does not name yet but an executed invite link authorizes this
#: node to reach (§2.5 ``connect-inviter``).  Without it the directory-
#: driven forwarding reconcile unmaps the inviter in the very redial the
#: bootstrap step triggers -- the joining node's directory is still empty
#: at that point, and ``ForwardingEndpoints`` unmaps whatever the
#: directory does not name.
BOOTSTRAP_PEERS_RELPATH = "state/tailcat/bootstrap-peers.json"


def left_orgs_path(home: Path) -> Path:
    return Path(home) / LEFT_ORGS_RELPATH


def read_left_orgs(home: Path) -> list[str]:
    """The orgs this node has left; an absent or corrupt file means none.

    # LAX(tailnet-cutover): a corrupt file is treated as empty rather than
    # failing loudly; the proper shape is a typed state record with a
    # schema version, like the other files under ``state/tailcat/``.
    """

    try:
        raw = left_orgs_path(home).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return sorted({org for org in value if isinstance(org, str)})


def add_left_org(home: Path, org: str) -> list[str]:
    """Record ``org`` as left (idempotent) and return the new list."""

    org_space_name(org)  # the same name rule as every org-taking entry point
    orgs = read_left_orgs(home)
    if org in orgs:
        return orgs
    orgs = sorted([*orgs, org])
    _write_left_orgs(home, orgs)
    return orgs


def clear_left_org(home: Path, org: str) -> list[str]:
    """Forget that ``org`` was left (an explicit rejoin); return the new list."""

    orgs = read_left_orgs(home)
    if org not in orgs:
        return orgs
    orgs = [name for name in orgs if name != org]
    _write_left_orgs(home, orgs)
    return orgs


def _write_left_orgs(home: Path, orgs: list[str]) -> None:
    path = left_orgs_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(orgs, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def bootstrap_peers_path(home: Path) -> Path:
    return Path(home) / BOOTSTRAP_PEERS_RELPATH


def read_bootstrap_peers(home: Path) -> dict[str, dict[str, str]]:
    """Invite-authorized peers not yet in any directory; corrupt means none.

    # LAX(tailnet-cutover): a corrupt file is treated as empty rather than
    # failing loudly; the proper shape is a typed state record with a
    # schema version, like the other files under ``state/tailcat/``.
    """

    try:
        raw = bootstrap_peers_path(home).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    peers: dict[str, dict[str, str]] = {}
    for device_id, entry in value.items():
        if not isinstance(device_id, str) or not isinstance(entry, dict):
            continue
        address = entry.get("address")
        org = entry.get("org")
        inviter = entry.get("inviter")
        stored_device_id = entry.get("deviceId")
        if (
            not isinstance(address, str)
            or not address
            or not isinstance(inviter, str)
            or not inviter
            or not isinstance(stored_device_id, str)
            or not stored_device_id
            or device_id != f"{inviter.removeprefix('user:')}/{stored_device_id}"
        ):
            continue
        peers[device_id] = {
            "address": address,
            "org": org if isinstance(org, str) else "",
            "addedAt": str(entry.get("addedAt") or ""),
            "inviter": inviter.removeprefix("user:"),
            "deviceId": stored_device_id,
        }
    return peers


def _write_bootstrap_peers(home: Path, peers: dict[str, dict[str, str]]) -> None:
    # The addresses carry the Tailcat PSK: create the file 0600 from the
    # start (never write-then-chmod) and replace it atomically.
    path = bootstrap_peers_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(peers, indent=2) + "\n")
    os.replace(temporary, path)


def add_bootstrap_peer(
    home: Path,
    *,
    inviter: str,
    device_id: str,
    address: str,
    org: str,
    added_at: str,
) -> None:
    """Record one invite-authorized peer by ``(inviter, deviceId)``."""

    peers = read_bootstrap_peers(home)
    bare_inviter = inviter.removeprefix("user:")
    peer = f"{bare_inviter}/{device_id}"
    entry = {
        "address": address,
        "org": org,
        "addedAt": added_at,
        "inviter": bare_inviter,
        "deviceId": device_id,
    }
    if peers.get(peer) == entry:
        return
    peers[peer] = entry
    _write_bootstrap_peers(home, peers)


def remove_bootstrap_peers(home: Path, peers_to_remove: Iterable[str]) -> list[str]:
    """Drop the named owner-qualified bootstrap peers."""

    peers = read_bootstrap_peers(home)
    removed = [peer for peer in peers_to_remove if peer in peers]
    if not removed:
        return []
    for peer in removed:
        del peers[peer]
    _write_bootstrap_peers(home, peers)
    return sorted(removed)


def _org_create_intents_path(home: Path) -> Path:
    return Path(home) / ORG_CREATE_INTENTS_RELPATH


def _read_org_create_intents(home: Path) -> dict[str, dict[str, str | None]]:
    try:
        value = json.loads(_org_create_intents_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    intents: dict[str, dict[str, str | None]] = {}
    required = {"nodeId", "owner", "startedAt", "directorySpaceId", "aclSpaceId"}
    for org, record in value.items():
        if not isinstance(org, str) or not isinstance(record, dict):
            continue
        if set(record) != required:
            continue
        if not all(
            isinstance(record[key], str) and record[key]
            for key in ("nodeId", "owner", "startedAt")
        ):
            continue
        if any(
            record[key] is not None and not isinstance(record[key], str)
            for key in ("directorySpaceId", "aclSpaceId")
        ):
            continue
        intents[org] = dict(record)
    return intents


def _write_org_create_intents(
    home: Path, intents: dict[str, dict[str, str | None]]
) -> None:
    atomic_json_write(_org_create_intents_path(home), intents)


def org_create_intent(home: Path, org: str) -> dict[str, str | None] | None:
    record = _read_org_create_intents(home).get(org)
    return None if record is None else dict(record)


def begin_org_create(
    home: Path, *, org: str, node_id: str, owner: str, started_at: str
) -> dict[str, str | None]:
    org_space_name(org)
    intents = _read_org_create_intents(home)
    existing = intents.get(org)
    if existing is not None:
        return dict(existing)
    record: dict[str, str | None] = {
        "nodeId": node_id,
        "owner": owner,
        "startedAt": started_at,
        "directorySpaceId": None,
        "aclSpaceId": None,
    }
    intents[org] = record
    _write_org_create_intents(home, intents)
    return dict(record)


def update_org_create(
    home: Path, org: str, *, directory_space_id: str | None = None,
    acl_space_id: str | None = None,
) -> dict[str, str | None]:
    intents = _read_org_create_intents(home)
    record = intents.get(org)
    if record is None:
        raise ValueError(f"org create intent is absent for {org}")
    if directory_space_id is not None:
        record["directorySpaceId"] = directory_space_id
    if acl_space_id is not None:
        record["aclSpaceId"] = acl_space_id
    _write_org_create_intents(home, intents)
    return dict(record)


def finish_org_create(home: Path, org: str) -> None:
    intents = _read_org_create_intents(home)
    if intents.pop(org, None) is not None:
        _write_org_create_intents(home, intents)


def org_acl_grants_path(home: Path) -> Path:
    return Path(home) / ORG_ACL_GRANTS_RELPATH


def read_org_acl_grants(
    home: Path, *, now_ms: int | None = None
) -> dict[str, dict[str, dict[str, int | None]]]:
    """Live ACL entry grants per org; expired entries never come back.

    # LAX(tailnet-cutover): a corrupt file is treated as empty rather than
    # failing loudly; the proper shape is a typed state record with a
    # schema version, like the other files under ``state/tailcat/``.
    """

    try:
        raw = org_acl_grants_path(home).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    grants: dict[str, dict[str, dict[str, int | None]]] = {}
    for org, agents in value.items():
        if not isinstance(org, str) or not isinstance(agents, dict):
            continue
        live: dict[str, dict[str, int | None]] = {}
        for agent, entry in agents.items():
            if not isinstance(agent, str) or not isinstance(entry, dict):
                continue
            granted_at = entry.get("grantedAtMs")
            until = entry.get("untilMs")
            if isinstance(granted_at, bool) or not isinstance(granted_at, int):
                continue
            if until is not None and (
                isinstance(until, bool) or not isinstance(until, int)
            ):
                continue
            if now_ms is not None and until is not None and until <= now_ms:
                continue
            live[agent] = {"grantedAtMs": granted_at, "untilMs": until}
        if live:
            grants[org] = live
    return grants


def add_org_acl_grant(
    home: Path,
    org: str,
    agent: str,
    *,
    granted_at_ms: int,
    until_ms: int | None = None,
) -> dict[str, int | None]:
    """Record (or re-stamp) one agent's ACL entry grant and return the entry."""

    org_space_name(org)  # the same name rule as every org-taking entry point
    grants = read_org_acl_grants(home, now_ms=granted_at_ms)
    entry: dict[str, int | None] = {
        "grantedAtMs": granted_at_ms,
        "untilMs": until_ms,
    }
    grants.setdefault(org, {})[agent] = entry
    _write_org_acl_grants(home, grants)
    return entry


def remove_org_acl_grant(home: Path, org: str, agent: str) -> bool:
    """Forget one agent's ACL entry grant; False when nothing was recorded."""

    grants = read_org_acl_grants(home)
    if agent not in grants.get(org, {}):
        return False
    del grants[org][agent]
    if not grants[org]:
        del grants[org]
    _write_org_acl_grants(home, grants)
    return True


def _write_org_acl_grants(
    home: Path, grants: dict[str, dict[str, dict[str, int | None]]]
) -> None:
    path = org_acl_grants_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(grants, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
