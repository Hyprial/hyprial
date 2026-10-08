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

__all__ = [
    "BOOTSTRAP_PEERS_RELPATH",
    "LEFT_ORGS_RELPATH",
    "add_bootstrap_peer",
    "add_left_org",
    "clear_left_org",
    "left_orgs_path",
    "read_bootstrap_peers",
    "read_left_orgs",
    "remove_bootstrap_peers",
]

#: JSON array of org names, relative to the daemon's home.
LEFT_ORGS_RELPATH = "state/tailcat/left-orgs.json"

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
