# Decision record (file header; no inline comments by repository rule):
# - B2 (orgfs actor-host injection, 2026-11-10): the fixed UUID is rebound only
#   inside the `document/operations/spaces` module. Rebinding uuid4 on the
#   shared uuid module would give every actor on the single orgfs host the same
#   handle id, and the engine could then no longer resolve any of them.
# - Phase D T1: rewritten onto two file-backed ``OrgFsRuntime`` nodes on
#   the contract suite's in-process network (``runtime_state/support.Peers``,
#   trust on as in the old pair of stores).  The member joins and pulls the
#   owner's tree; every protected row of the agent inventory, re-signed by the
#   member, is ``Rejected("protected-path")`` by the owner through
#   ``SyncHost.apply_remote`` (design §3.6), where the journal admission said
#   ``not-a-member``.  The durable round trip is a reopen of both nodes.  The
#   fixed space id is no longer needed: protected ids are checked against the
#   space they were made for.
"""Durable facade trees and replica admission, including forged tree authors."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pycrdt import Doc, Map

from hyprial.daemon.impl.org.network.member_agents import MemberAgentDirectory
from hyprial.daemon.impl.orgfs.membership import Rejected, tree_row_wire
from hyprial.identity import parse_protected_directory_node_id

from tests.daemon.orgfs.coordinator.support import EMPTY
from tests.daemon.orgfs.membership.support import forge_update
from tests.daemon.orgfs.runtime_state.support import Peers, eventually

OWNER = "owner@example"
MEMBER = "member@example"


@pytest.fixture
def peers(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    group = Peers(tmp_path / "nodes")
    try:
        yield group
    finally:
        group.close()


def _protected_rows(fs: Any, space_id: str) -> dict[str, dict[str, Any]]:
    found = fs.sync_host(space_id).read_update("tree", EMPTY)
    assert found is not None
    doc = Doc()
    doc.apply_update(found[0])
    rows = {key: json.loads(str(value)) for key, value in doc.get("nodes", type=Map).items()}
    return {
        node_id: row
        for node_id, row in rows.items()
        if parse_protected_directory_node_id(node_id) is not None
    }


def test_durable_tree_roundtrip_and_replica_admission(peers: Peers) -> None:
    owner = peers.add("source", "user:" + OWNER)
    member = peers.add("target", "user:" + MEMBER)
    org = "group-work"
    agent_id = "a." + "a" * 26
    row = {
        "agentId": agent_id,
        "agent": f"agent:{OWNER}:node:{agent_id}",
        "name": "agent",
        "owner": f"user:{OWNER}",
        "deviceId": "node",
    }
    space_id = owner.fs.create_space(org).space_id
    owner.fs.invite(space_id, "user:" + MEMBER)
    assert MemberAgentDirectory(owner.fs).publish(org, OWNER, "node", [row]) is True
    member.fs.join(space_id)
    peers.pull_all(space_id)
    eventually(lambda: _agents(member.fs, org, [row]))

    protected = _protected_rows(member.fs, space_id)
    assert len(protected) >= 3
    base = member.fs.sync_host(space_id).read_update("tree", EMPTY)
    assert base is not None
    host = owner.fs.sync_host(space_id)
    for client_id, (node_id, value) in enumerate(sorted(protected.items()), start=5101):
        forged_row = dict(value)

        def mutate(doc: Doc, author: str, node_id: str = node_id, forged_row: dict = forged_row) -> None:
            doc.get("nodes", type=Map)[node_id] = tree_row_wire(forged_row, author=author)

        frame = forge_update("tree", mutate, author="user:" + MEMBER, client_id=client_id, base=base[0])
        outcome = host.apply_remote(frame.doc_id, frame.update, frame.vector, "target")
        assert isinstance(outcome, Rejected) and outcome.code == "protected-path", node_id
    assert MemberAgentDirectory(owner.fs).list(org)["agents"] == [row]

    owner.reopen()
    member.reopen()
    assert MemberAgentDirectory(owner.fs).list(org)["agents"] == [row]
    assert MemberAgentDirectory(member.fs).list(org)["agents"] == [row]


def _agents(fs: Any, org: str, expected: list[dict[str, str]]) -> None:
    assert MemberAgentDirectory(fs).list(org)["agents"] == expected
