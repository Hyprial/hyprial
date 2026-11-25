"""Cell builders for the param resolver tests (no OrgFS)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.daemon.impl.cells.params import CellParamSnapshot, ParamSource
from hyprial.daemon.impl.cells.ports import CellIndex
from hyprial.kernel import keyed_cell_id, subject_cell_id
from tests.daemon.cells.runtime.cell_runtime_support import ALICE, MALLORY, uid_for

OWNER = ALICE
STRANGER = MALLORY
AGENT_ID = "a.x6qg5s2n46p7qli5msgy5ko6by"
AGENT_URI = "agent:alice:mac:worker"
OTHER_AGENT_ID = "a.b6qg5s2n46p7qli5msgy5ko6by"
OTHER_AGENT_URI = "agent:alice:mac:other"
THIRD_AGENT_ID = "a.c6qg5s2n46p7qli5msgy5ko6by"


def org_cell(
    space_id: str = "sp-acme",
    *,
    org: str = "group-acme",
    authority: str = OWNER,
    retired: bool = False,
) -> Cell:
    uid = authority.removeprefix("user:")
    return Cell(
        cell_id=keyed_cell_id("org", authority, space_id),
        kind="org",
        authority=authority,
        scope=CellScope.of_org(org),
        binding=CellBinding.keyed(space_id),
        name=org,
        members=(CellMember(uid, "owner"),),
        params={
            "org.name": org,
            "org.issuer": "https://login.example.com",
            "org.directorySpace": space_id,
            "org.tailnet": space_id,
            "org.parent": None,
            "org.members": {
                uid: {
                    "username": f"{uid_for('user:alice')[:8]}@example.com",
                    "joinedAtMs": 1_763_700_000_000,
                    "invitedBy": uid,
                    "devices": [
                        {
                            "deviceUid": "d." + "a" * 32,
                            "keyId": None,
                            "publicKey": None,
                            "verifiedAtMs": 1_763_700_000_000,
                        }
                    ],
                }
            },
        },
        retired=retired,
    )


def agent(
    params: Mapping[str, Any] | None = None,
    *,
    agent_id: str = AGENT_ID,
    uri: str = AGENT_URI,
    authority: str = OWNER,
    extra_members: Iterable[str] = (),
    retired: bool = False,
) -> Cell:
    return Cell(
        cell_id=subject_cell_id("agent", agent_id),
        kind="agent",
        authority=authority,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(agent_id),
        members=(
            CellMember(uri, "harness"),
            *(CellMember(ref) for ref in extra_members),
        ),
        params=params or {},
        retired=retired,
    )


def keyed(
    kind: str, key: str, params: Mapping[str, Any], *, authority: str = OWNER
) -> Cell:
    return Cell(
        cell_id=keyed_cell_id(kind, authority, key),
        kind=kind,
        authority=authority,
        scope=CellScope.of_org(),
        binding=CellBinding.keyed(key),
        params=params,
    )


def snapshot(
    *cells: Cell, generation: int = 3, owner: str = OWNER
) -> CellParamSnapshot:
    index = CellIndex(generation, {cell.cell_id: cell for cell in cells})
    return CellParamSnapshot.from_index(index, owner)


def source_snapshot(
    *sources: ParamSource,
    aliases: Mapping[str, frozenset[str]] | None = None,
    generation: int = 3,
    owner: str = OWNER,
) -> CellParamSnapshot:
    ordered = tuple(sorted(sources, key=lambda item: item.cell_id))
    return CellParamSnapshot(
        generation=generation,
        owner=owner,
        sources=ordered,
        aliases=aliases or {},
    )
