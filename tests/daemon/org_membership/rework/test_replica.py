# Decision record (file header; no inline comments by repository rule):
# - B2 (orgfs actor-host injection, 2026-11-10): the fixed UUID is rebound only
#   inside the `document/operations/spaces` module. Rebinding uuid4 on the
#   shared uuid module would give every actor on the single orgfs host the same
#   handle id, and the engine could then no longer resolve any of them.
"""Durable facade trees and replica admission, including forged tree authors."""
import json
from types import SimpleNamespace
from uuid import UUID

from hyprial.daemon.impl.org.network.member_agents import MemberAgentDirectory
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore
from hyprial.daemon.impl.orgfs.document.operations import spaces

OWNER = "owner@example"
MEMBER = "member@example"
SPACE = "00000000-0000-0000-0000-000000000001"
STAMP = "2026-10-31T00:00:00Z"


def test_durable_tree_roundtrip_and_replica_admission(tmp_path, monkeypatch):
    monkeypatch.setattr(
        spaces,
        "uuid",
        SimpleNamespace(uuid4=lambda: UUID(SPACE), UUID=UUID),
    )
    source = FileSpaceStore(tmp_path / "source", SPACE, node_id="source")
    target = FileSpaceStore(tmp_path / "target", SPACE, node_id="target")
    fs = LocalOrgFs(stores=source, author="user:" + OWNER)
    replica = LocalOrgFs(stores=target, author="user:" + OWNER)
    org = "group-work"
    agent_id = "a." + "a" * 26
    failure = None
    try:
        fs.create_space(org)
        fs.invite(SPACE, "user:" + MEMBER)
        directory = MemberAgentDirectory(fs)
        row = {
            "agentId": agent_id,
            "agent": f"agent:{OWNER}:node:{agent_id}",
            "name": "agent",
            "owner": f"user:{OWNER}",
            "deviceId": "node",
        }
        try:
            directory.publish(org, OWNER, "node", [row])
        except Exception as error:
            failure = str(error)
        assert failure is None
        envelopes = [bytes(row[0]) for row in source._db.execute("SELECT envelope_bytes FROM commits ORDER BY rowid")]
        protected_trees = 0
        for envelope in envelopes:
            value = json.loads(envelope)
            if value["docId"].startswith("tree-"):
                forged = json.loads(envelope)
                forged["origin"]["author"] = "user:" + MEMBER
                assert target.admission(json.dumps(forged).encode()) == "not-a-member"
                protected_trees += 1
            assert target.admission(envelope) == "ok"
            target.import_envelope(envelope, supplier="source")
        assert protected_trees >= 3
        replica.load_space(SPACE)
        assert MemberAgentDirectory(replica).list(org)["agents"] == [row]
    finally:
        fs.close_effects()
        replica.close_effects()
        source.close()
        target.close()
