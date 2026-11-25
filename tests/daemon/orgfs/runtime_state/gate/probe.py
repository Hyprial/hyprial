# Decisions (phase F1, item 1, 2026-11-25):
# - Two nodes on the contract ``FakeNetwork``, the real device-owner resolver
#   of each runtime (no factory stand-in), an org directory ``group-acme``
#   built through ``OrgFsDirectoryStore`` as the org layer builds it.
# - ``cold``: trust off from the first byte, the production configuration
#   since ``trust_upper_layers`` became ``False``; each node writes only its
#   own device row, as ``OrgNetworkService._publish_into`` does.  ``warm``:
#   everything under trust, then both nodes reopen with trust off (a daemon
#   restart into the production configuration).
# - Node gate ruling (F1): the state runtime gets a fake ``device_owner_hint``
#   standing in for the tailnet roster both nodes hold once the invite is
#   redeemed (owner and member devices, nothing else).
# - Package S (2026-11-25): the journal runtime is deleted, so only the
#   ``state`` implementation is probed.
# - Every step records an outcome instead of asserting, so one run yields the
#   whole row; the test pins the rows.
"""Two-node production-config probe shared by the F1 gate tests."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.identity import DirectoryDevice

from ..support import Peer, Peers

ORG = "group-acme"
OWNER = "user:owner@example"
MEMBER = "user:member@example"
OWNER_NODE = "node-owner"
MEMBER_NODE = "node-member"
ROSTER = {OWNER_NODE: OWNER, MEMBER_NODE: MEMBER}


def _state_runtime(state_dir: Any, **kwargs: Any) -> OrgFsRuntime:
    return OrgFsRuntime(state_dir, device_owner_hint=ROSTER.get, **kwargs)


IMPLS = {"state": _state_runtime}
KINDS = ("plain", "directory", "acl")
SPACE_NAMES = {"plain": "notes", "directory": ORG, "acl": f"{ORG}-acl"}


@dataclass
class Cell:
    """What one two-node run observed."""

    impl: str
    kind: str
    bootstrap: str
    steps: dict[str, str] = field(default_factory=dict)

    def row(self) -> dict[str, str]:
        return {"impl": self.impl, "kind": self.kind, "bootstrap": self.bootstrap, **self.steps}


def device(owner: str, node_id: str) -> DirectoryDevice:
    return DirectoryDevice(
        device_id=node_id,
        owner=owner.removeprefix("user:"),
        server_public=f"nodekey:server-{node_id}",
        client_public=f"nodekey:client-{node_id}",
        address=f"tcp://127.0.0.1:7447#{node_id}",
        key_generation=1,
        updated_at="2026-11-25T00:00:00+00:00",
    )


def attempt(action: Any) -> str:
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - the probe records every outcome
        code = getattr(exc, "code", type(exc).__name__)
        details = getattr(exc, "details", None)
        reason = details.get("reason") if isinstance(details, dict) else None
        return f"err:{code}" + (f"/{reason}" if reason else "")
    return "ok"


def gate(peer: Peer, space_id: str, node: str) -> str:
    runtime = peer.runtime
    assert runtime is not None
    mesh = runtime._mesh(space_id)  # noqa: SLF001 - the space's sync object
    if mesh is None:
        return "no-sync"
    if isinstance(runtime, OrgFsRuntime):
        return "pass" if mesh.host.supplier_allowed(node) else "refuse"
    return "pass" if mesh._supplier_member_allowed(node) else "refuse"  # noqa: SLF001


def pull(requester: Peer, supplier: Peer, space_id: str) -> str:
    runtime = requester.runtime
    assert runtime is not None
    mesh = runtime._mesh(space_id)  # noqa: SLF001
    if mesh is None:
        return "no-sync"
    return attempt(lambda: mesh.sync_from(supplier.node_id))


def names(peer: Peer, space_id: str) -> set[str]:
    try:
        return {node.name for node in peer.fs.listdir(space_id, "")}
    except Exception:  # noqa: BLE001 - an unopened space has no names
        return set()


def arrived(peer: Peer, space_id: str, name: str, *, timeout: float = 2.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if name in names(peer, space_id):
            return "yes"
        time.sleep(0.05)
    return "no"


def joined(peer: Peer, space_id: str) -> bool:
    return space_id in {info.space_id for info in peer.fs.spaces()}


def run_cell(peers: Peers, impl: str, kind: str, bootstrap: str) -> Cell:
    """``bootstrap``: ``cold`` (trust off from the start, each node writes its
    own device row once it can) or ``warm`` (every step under trust, device
    rows of both nodes replicated, then both nodes reopen with trust off)."""

    cell = Cell(impl, kind, bootstrap)
    trust = bootstrap == "warm"
    runtime_class = IMPLS[impl]
    owner = peers.add(OWNER_NODE, OWNER, trust=trust, runtime_class=runtime_class)
    member = peers.add(MEMBER_NODE, MEMBER, trust=trust, runtime_class=runtime_class)
    directory = OrgFsDirectoryStore(owner.fs)
    dir_id = owner.fs.create_space(ORG).space_id
    directory.ensure_org_directory_roots(ORG)
    directory.put_device(ORG, device(OWNER, OWNER_NODE))
    cell.steps["owner_writes_member_row"] = attempt(
        lambda: OrgFsDirectoryStore(owner.fs).put_device(ORG, device(MEMBER, MEMBER_NODE))
    ) if bootstrap == "cold" else "n/a"
    owner.fs.invite(dir_id, MEMBER, "rw")
    if kind == "directory":
        target = dir_id
    else:
        target = owner.fs.create_space(SPACE_NAMES[kind]).space_id
        owner.fs.invite(target, MEMBER, "rw")
    cell.steps["join_directory"] = attempt(lambda: member.fs.join(dir_id))
    if joined(member, dir_id):
        cell.steps["member_writes_own_row"] = attempt(
            lambda: OrgFsDirectoryStore(member.fs).put_device(ORG, device(MEMBER, MEMBER_NODE))
        )
        cell.steps["member_row_at_owner"] = _row_at(owner, dir_id)
    else:
        cell.steps["member_writes_own_row"] = "not-joined"
        cell.steps["member_row_at_owner"] = "no"
    if kind != "directory":
        cell.steps["join_target"] = attempt(lambda: member.fs.join(target))
    else:
        cell.steps["join_target"] = cell.steps["join_directory"]
    if bootstrap == "warm":
        owner.fs.write_text(target, "warm.md", "warm")
        arrived(member, target, "warm.md")
        for peer in (owner, member):
            peer.trust = False
            peer.reopen()
    cell.steps["gate_owner_sees_member"] = gate(owner, target, MEMBER_NODE)
    cell.steps["gate_member_sees_owner"] = gate(member, target, OWNER_NODE) if joined(member, target) else "not-joined"
    if not joined(member, target):
        cell.steps.update({"owner_to_member": "n/a", "member_to_owner": "n/a",
                           "pull_member_from_owner": "n/a", "pull_owner_from_member": "n/a"})
        return cell
    owner.fs.write_text(target, "from-owner.md", "o")
    member.fs.write_text(target, "from-member.md", "m")
    broadcast_o = arrived(member, target, "from-owner.md", timeout=1.0)
    broadcast_m = arrived(owner, target, "from-member.md", timeout=1.0)
    cell.steps["pull_member_from_owner"] = pull(member, owner, target)
    cell.steps["pull_owner_from_member"] = pull(owner, member, target)
    cell.steps["owner_to_member"] = (
        "broadcast" if broadcast_o == "yes" else ("pull" if arrived(member, target, "from-owner.md") == "yes" else "no")
    )
    cell.steps["member_to_owner"] = (
        "broadcast" if broadcast_m == "yes" else ("pull" if arrived(owner, target, "from-member.md") == "yes" else "no")
    )
    return cell


def _row_at(owner: Peer, dir_id: str) -> str:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            rows = {node.name for node in owner.fs.listdir(dir_id, "directory/devices", recursive=True)}
        except Exception:  # noqa: BLE001
            rows = set()
        if f"{MEMBER_NODE}.json" in rows:
            return "yes"
        time.sleep(0.05)
    return "no"
