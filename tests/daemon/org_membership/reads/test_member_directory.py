"""An organization's people determine which published Agents are visible."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from hyprial.daemon.impl.org.network.member_agents import MemberAgentDirectory, publish_for_app, chat_targets_for_app
from hyprial.daemon.impl.orgfs.api import MemberInfo
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from tests.daemon.org_membership.storage.support import FakeFacade


def test_member_chat_directory_adds_no_org_ipc_methods() -> None:
    from hyprial.daemon import daemon_openapi
    from hyprial.daemon.impl.request_catalog.org import ORG_BINDINGS

    names = {binding[0].name for binding in ORG_BINDINGS}
    paths = daemon_openapi()["paths"]
    for name in ("org.agent.join", "org.agent.leave", "org.agent.orgs", "org.agents.list", "org.member-agents.list"):
        assert name not in names
        assert f"/daemon/request/{name}" not in paths


@dataclass
class DirectoryFacade(FakeFacade):
    active_users: set[str] = field(default_factory=set)
    authors: dict[tuple[str, str], str] = field(default_factory=dict)

    def members(self, space_id: str) -> tuple[MemberInfo, ...]:
        return tuple(
            MemberInfo(space_id, user, "write", self.owner, "2026-11-18T00:00:00Z")
            for user in sorted(self.active_users)
        )

    def write_text(self, space_id: str, path: str, content: str, **kwargs):
        node = super().write_text(space_id, path, content, **kwargs)
        self.authors[(space_id, path)] = self.owner
        return node

    def document_authors(self, space_id: str, node: str) -> tuple[str, ...]:
        path = self.stat(space_id, node).path
        return (self.authors[(space_id, path)],)


def agent(owner: str, node: str, marker: str, name: str) -> dict[str, str]:
    agent_id = "a." + marker * 26
    return {
        "agentId": agent_id,
        "agent": f"agent:{owner}:{node}:{agent_id}",
        "name": name,
        "owner": f"user:{owner}",
        "deviceId": node,
    }


def patch_live_devices(monkeypatch, facade: DirectoryFacade) -> None:
    from hyprial.daemon.impl.org.network import member_agents

    monkeypatch.setattr(member_agents, "OrgFsDirectoryStore", lambda _fs, **_kwargs: SimpleNamespace(
        list_devices=lambda _org: [SimpleNamespace(device_id="node-b"), *(
            [SimpleNamespace(device_id="node-a")] if "a@example" in facade.active_users else []
        )],
    ))


def test_member_directory_aggregates_agents_without_agent_org_memberships() -> None:
    facade = DirectoryFacade(owner="b@example", active_users={"a@example"})
    facade.create_space("group-team")
    directory = MemberAgentDirectory(facade)
    own = agent("b@example", "node-b", "b", "b-1")
    remote = agent("a@example", "node-a", "a", "a-1")

    directory.publish("group-team", "b@example", "node-b", [own])
    facade.owner = "a@example"
    directory.publish("group-team", "a@example", "node-a", [remote])

    listed = directory.list("group-team")
    assert listed["status"] == "ok"
    assert {row["agentId"] for row in listed["agents"]} == {own["agentId"], remote["agentId"]}

    facade.active_users.clear()
    listed_after_exit = directory.list("group-team")
    assert listed_after_exit["status"] == "ok"
    assert listed_after_exit["agents"] == [own]


def test_chat_targets_only_include_agents_owned_by_current_org_members(monkeypatch) -> None:
    from hyprial.daemon.impl.org.network import member_agents

    facade = DirectoryFacade(owner="b@example", active_users={"a@example"})
    facade.create_space("group-team")
    patch_live_devices(monkeypatch, facade)
    directory = MemberAgentDirectory(facade)
    remote = agent("a@example", "node-a", "a", "a-1")
    facade.owner = "a@example"
    directory.publish("group-team", "a@example", "node-a", [remote])
    facade.owner = "b@example"
    monkeypatch.setattr(member_agents, "for_app", lambda _app, repair_current: SimpleNamespace(active={"group-team": {}}))
    app = SimpleNamespace(owner="b@example", _require_orgfs_runtime=lambda: SimpleNamespace(facade=facade))

    assert chat_targets_for_app(app) == [{
        "targetKind": "agent", "targetUri": remote["agent"], "actor": remote["agent"],
        "status": "unknown", "deliverable": True, "agentId": remote["agentId"],
        "displayName": "a-1", "orgs": ["group-team"],
    }]
    facade.active_users.clear()
    assert chat_targets_for_app(app) == []


def test_chat_targets_exclude_stale_member_inventory_after_device_leaves(monkeypatch) -> None:
    from hyprial.daemon.impl.org.network import member_agents

    facade = DirectoryFacade(owner="b@example", active_users={"a@example"})
    facade.create_space("group-team")
    directory = MemberAgentDirectory(facade)
    local = agent("b@example", "node-b", "b", "b-1")
    remote = agent("a@example", "node-a", "a", "a-1")
    directory.publish("group-team", "b@example", "node-b", [local])
    facade.owner = "a@example"
    directory.publish("group-team", "a@example", "node-a", [remote])
    facade.owner = "b@example"
    monkeypatch.setattr(member_agents, "for_app", lambda _app, repair_current: SimpleNamespace(active={"group-team": {}}))
    monkeypatch.setattr(member_agents, "OrgFsDirectoryStore", lambda _fs, **_kwargs: SimpleNamespace(
        list_devices=lambda _org: [SimpleNamespace(device_id="node-b")],
    ), raising=False)
    app = SimpleNamespace(owner="b@example", _require_orgfs_runtime=lambda: SimpleNamespace(facade=facade))

    assert chat_targets_for_app(app) == [{
        "targetKind": "agent", "targetUri": local["agent"], "actor": local["agent"],
        "status": "unknown", "deliverable": True, "agentId": local["agentId"],
        "displayName": "b-1", "orgs": ["group-team"],
    }]


def test_chat_directory_reports_incomplete_org_inventory(monkeypatch) -> None:
    from hyprial.daemon.impl.org.network import member_agents

    facade = DirectoryFacade(owner="b@example", active_users={"a@example"})
    facade.create_space("group-team")
    patch_live_devices(monkeypatch, facade)
    MemberAgentDirectory(facade).publish("group-team", "b@example", "node-b", [])
    monkeypatch.setattr(member_agents, "for_app", lambda _app, repair_current: SimpleNamespace(active={"group-team": {}}))
    app = SimpleNamespace(owner="b@example", _require_orgfs_runtime=lambda: SimpleNamespace(facade=facade))

    assert member_agents.chat_directory_for_app(app)["orgDirectoryStatus"] == {"group-team": "partial"}


def test_chat_directory_does_not_start_orgfs_before_login() -> None:
    app = SimpleNamespace(owner=None, _require_orgfs_runtime=lambda: (_ for _ in ()).throw(AssertionError("must not open OrgFS")))
    assert chat_targets_for_app(app) == []


def test_chat_directory_marks_offline_member_inventory_partial(monkeypatch) -> None:
    from hyprial.daemon.impl.org.network import member_agents

    facade = DirectoryFacade(owner="b@example", active_users={"a@example"})
    facade.create_space("group-team")
    patch_live_devices(monkeypatch, facade)
    directory = MemberAgentDirectory(facade)
    directory.publish("group-team", "b@example", "node-b", [])
    facade.owner = "a@example"
    directory.publish("group-team", "a@example", "node-a", [agent("a@example", "node-a", "a", "a-1")])
    facade.owner = "b@example"
    monkeypatch.setattr(member_agents, "for_app", lambda _app, repair_current: SimpleNamespace(active={"group-team": {}}))
    runtime = SimpleNamespace(facade=facade, node_id="node-b", status=lambda _space: SimpleNamespace(holders_online=("node-b",)))
    app = SimpleNamespace(owner="b@example", _require_orgfs_runtime=lambda: runtime)

    assert member_agents.chat_directory_for_app(app)["orgDirectoryStatus"] == {"group-team": "partial"}


def test_member_directory_marks_missing_or_untrusted_inventory_incomplete() -> None:
    facade = DirectoryFacade(owner="b@example", active_users={"a@example"})
    facade.create_space("group-team")
    directory = MemberAgentDirectory(facade)
    own = agent("b@example", "node-b", "b", "b-1")
    directory.publish("group-team", "b@example", "node-b", [own])

    assert directory.list("group-team")["status"] == "partial"
    facade.owner = "a@example"
    directory.publish("group-team", "a@example", "node-a", [agent("a@example", "node-a", "a", "a-1")])
    facade.authors[("space-1", "directory/people/a-example/agents-node-a.json")] = "mallory@example"

    listed = directory.list("group-team")
    assert listed["status"] == "partial"
    assert listed["agents"] == [own]


def test_member_directory_marks_an_unpublished_second_device_incomplete() -> None:
    facade = DirectoryFacade(owner="b@example")
    facade.create_space("group-team")
    facade.mkdir("space-1", "directory/devices/b@example")
    facade.write_text("space-1", "directory/devices/b@example/node-b.json", "{}")
    facade.write_text("space-1", "directory/devices/b@example/node-b2.json", "{}")
    directory = MemberAgentDirectory(facade)
    directory.publish("group-team", "b@example", "node-b", [agent("b@example", "node-b", "b", "b-1")])

    assert directory.list("group-team")["status"] == "partial"


def test_publish_for_app_uses_local_registry_without_agent_org_membership() -> None:
    facade = DirectoryFacade(owner="b@example")
    facade.create_space("group-team")
    local = SimpleNamespace(
        agent_id="a." + "b" * 26,
        uri="agent:b@example:node-b:a." + "b" * 26,
        actor="b-1",
        owner="b@example",
        machine="node-b",
    )
    app = SimpleNamespace(
        owner="b@example",
        node_id="node-b",
        agents=SimpleNamespace(list=lambda: (local,)),
        _require_orgfs_runtime=lambda: SimpleNamespace(facade=facade),
    )

    assert publish_for_app(app, ["group-team"]) == {"published": ["group-team"], "errors": []}
    assert MemberAgentDirectory(facade).list("group-team")["agents"][0]["name"] == "b-1"


def test_existing_directory_refresh_also_publishes_member_agents(monkeypatch) -> None:
    from hyprial.daemon.impl.org.network import ipc
    from hyprial.daemon.impl.org.network import member_agents

    service = SimpleNamespace(
        publish_self=lambda: {"published": [], "skipped": [], "errors": []},
        _orgs=lambda: ["group-team"],
    )
    observed = []
    monkeypatch.setattr(ipc, "_build_service", lambda _app: service)
    monkeypatch.setattr(member_agents, "publish_for_app", lambda app, orgs: observed.append((app, orgs)) or {"published": ["group-team"], "errors": []})
    app = SimpleNamespace()

    ipc.publish_self_for_app(app)

    assert observed == [(app, ["group-team"])]


def test_real_orgfs_accepts_owner_published_inventory(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "state", node_id="node-b", author="user:b@example")
    try:
        space = runtime.facade.create_space("group-team")
        runtime.facade.mkdir(space.space_id, "directory")
        directory = MemberAgentDirectory(runtime.facade)
        own = agent("b@example", "node-b", "b", "b-1")

        assert directory.publish("group-team", "b@example", "node-b", [own]) is True
        assert directory.list("group-team") == {
            "org": "group-team", "status": "ok", "agents": [own],
        }
    finally:
        runtime.close()
