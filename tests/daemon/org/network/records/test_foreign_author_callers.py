# Decisions:
# - Phase D T1: these ran on ``LocalOrgFs``, whose local write path let a
#   second member co-write a protected record so the callers' author checks
#   could be seen dropping it.  On the state host that record cannot exist:
#   the co-writer's local write is ``protected-path`` (design §3.4) and the
#   same change from a peer is ``Rejected("protected-path")`` on every
#   receiver (§3.6).  Each test now drives both entries (local write, and a
#   C2 forgery through ``SyncHost.apply_remote``) and pins that the caller
#   keeps serving the record exactly as its owner wrote it, with a single
#   author.  The callers' foreign-author branches are unreachable through the
#   state host for these sections (T1 report).
# - The nodes are the in-memory org of ``orgfs/mesh/device_rows/support``.
"""Callers of ``document_authors`` never see a record someone else also wrote."""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.daemon.impl.org.network.directory import (
    OrgFsDirectoryStore,
    leave_request_admitted,
)
from hyprial.daemon.impl.org.network.member_agents import MemberAgentDirectory
from hyprial.daemon.impl.orgfs.membership import Rejected
from hyprial.identity.impl.org_directory import DirectoryDevice

from tests.daemon.orgfs.mesh.device_rows.support import BOB, ORG, OWNER, Org, refused


@pytest.fixture
def org():
    value = Org(members=(BOB,))
    try:
        yield value
    finally:
        value.close()


def _co_write_refused(org: Org, writer: str, receiver: str, path: str) -> None:
    space_id = org.space_id
    text, _version = org.fs(writer).read_text(space_id, path)
    assert refused(lambda: org.fs(writer).write_text(space_id, path, text + " ")) == "protected-path"
    doc_id = org.fs(writer).stat(space_id, path).doc_id
    assert doc_id is not None
    forged = org.forge_content(writer, doc_id, text + " ", author=writer)
    outcome = org.apply(receiver, forged, peer=f"node-{writer.split('@', 1)[0]}")
    assert isinstance(outcome, Rejected) and outcome.code == "protected-path"


def test_device_records_co_written_by_another_member_are_dropped(org: Org) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    store = OrgFsDirectoryStore(
        org.fs(OWNER), logger=lambda _level, _area, event, **fields: events.append((event, fields))
    )
    device = DirectoryDevice(
        device_id="node-a", owner=OWNER, server_public="nodekey:server-a",
        client_public="nodekey:client-a", address="tcp://10.0.0.2:7447#a",
        key_generation=1, updated_at="2026-10-03T11:00:00+00:00",
    )
    store.put_device(ORG, device)
    org.deliver()
    path = f"directory/devices/{OWNER}/node-a.json"

    _co_write_refused(org, BOB, OWNER, path)

    assert store.list_devices(ORG) == [device]
    assert org.directory(BOB).list_devices(ORG) == [device]
    assert org.fs(OWNER).document_authors(org.space_id, path) == (f"user:{OWNER}",)
    assert events == []


def test_member_agent_inventories_co_written_by_another_member_are_dropped(org: Org) -> None:
    agent_id = "a." + "o" * 26
    row = {
        "agentId": agent_id,
        "agent": f"agent:{OWNER}:node-a:{agent_id}",
        "name": "eng",
        "owner": f"user:{OWNER}",
        "deviceId": "node-a",
    }
    directory = MemberAgentDirectory(org.fs(OWNER))
    assert directory.publish(ORG, OWNER, "node-a", [row]) is True
    org.deliver()
    path = next(
        f"id:{node.node_id}"
        for node in org.fs(OWNER).listdir(org.space_id, "directory/people", recursive=True)
        if node.name.startswith("agents-")
    )

    _co_write_refused(org, BOB, OWNER, path)

    assert directory.list(ORG)["agents"] == [row]
    assert MemberAgentDirectory(org.fs(BOB)).list(ORG)["agents"] == [row]


def test_leave_requests_co_written_by_another_member_are_not_fulfilled(org: Org) -> None:
    space_id = org.space_id
    added_at = next(m.added_at for m in org.fs(OWNER).members(space_id) if m.user == f"user:{BOB}")
    org.directory(BOB).put_leave_request(ORG, BOB, {
        "org": ORG, "user": BOB, "deviceId": "node-b", "requestedAt": "t1",
        "memberAddedAt": added_at,
    })
    org.deliver()
    events: list[tuple[str, dict[str, Any]]] = []

    def log(_level: str, _area: str, event: str, **fields: Any) -> None:
        events.append((event, fields))

    store = org.directory(OWNER)
    assert leave_request_admitted(store, log, ORG, BOB) is True

    _co_write_refused(org, OWNER, BOB, f"directory/leaves/{BOB}.json")

    assert leave_request_admitted(store, log, ORG, BOB) is True
    assert leave_request_admitted(org.directory(BOB), log, ORG, BOB) is True
    assert org.fs(BOB).document_authors(space_id, f"directory/leaves/{BOB}.json") == (f"user:{BOB}",)
    assert events == []
