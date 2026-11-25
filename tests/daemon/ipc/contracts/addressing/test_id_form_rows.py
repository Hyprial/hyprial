"""UUID addressing output fields survive the registry's closed result schemas.

``targets`` agent rows (``agentId``/``displayName``), the ``org.network``
``self`` block and directory rows (``deviceUid``) and the agent.create resume
snapshots (``agentId``/``displayName``) are produced by real handlers and
validated through ``METHOD_REGISTRY.invoke``; an undeclared field there turns
the whole answer into DAEMON_ERROR.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.org.network.ipc import ipc_org_create
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.identity.impl.agents import addressing
from hyprial.identity.impl.device import DeviceRecord, address_path, write_device_record
from tests.daemon.org.network.test_org_network_ipc import NODE_ID, OWNER, _FakeApp
from tests.kernel.actors.identity.support import application
from tests.support.runtime._owned_applications import close_application
from tests.support.tailnet_membership import key

DEVICE_UID = uuid.UUID(int=0x5EED, version=4).hex


@pytest.fixture(params=[True, False], ids=["id-form", "name-form"])
def minting(request, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", request.param)
    return request.param


@pytest.fixture
def daemon(minting: bool, tmp_path: Path) -> Iterator[DaemonApplication]:
    instance = application(tmp_path)
    try:
        yield instance
    finally:
        close_application(instance)


def test_targets_agent_rows_carry_the_id_and_display_name(daemon, minting) -> None:
    daemon.handle("agent.create", {"name": "worker"})
    daemon.agents.rename("worker", "builder")
    daemon.handle("agent.create", {"name": "plain"})

    result = METHOD_REGISTRY.invoke(daemon, "targets", {"kind": "agent"})

    rows = {row["actor"]: row for row in result["targets"]}
    for name, shown in (("worker", "builder"), ("plain", "plain")):
        agent = daemon.agents.require(name)
        row = rows[agent.uri]
        assert row["agentId"] == agent.agent_id
        assert row["displayName"] == shown
    assert rows[daemon.agents.require("worker").uri]["targetUri"].endswith(
        daemon.agents.require("worker").agent_id if minting else ":worker"
    )


def test_existing_targets_returns_shared_org_agents(daemon, monkeypatch) -> None:
    from hyprial.daemon.impl.org.network import member_agents

    uri = "agent:peer@example:node-peer:a." + "p" * 26
    row = {
        "targetKind": "agent", "targetUri": uri, "actor": uri,
        "status": "unknown", "deliverable": True,
        "agentId": "a." + "p" * 26, "displayName": "peer",
        "orgs": ["group-team"],
    }
    monkeypatch.setattr(member_agents, "chat_directory_for_app", lambda _app: {
        "targets": [row], "orgDirectoryStatus": {"group-team": "partial"},
    })

    result = METHOD_REGISTRY.invoke(daemon, "targets", {"kind": "agent"})

    assert row in result["targets"]
    assert result["orgDirectoryStatus"] == {"group-team": "partial"}


def test_targets_merge_org_metadata_into_existing_actor_rows(daemon, monkeypatch) -> None:
    from hyprial.daemon.impl.org.network import member_agents

    remote_uri = "agent:peer@example:node-peer:a." + "p" * 26
    daemon.handle("agent.create", {"name": "worker"})
    daemon.agents.rename("worker", "local-builder")
    local = daemon.agents.require("worker")
    monkeypatch.setattr(
        daemon,
        "_actor_status_snapshot",
        lambda: [
            {"actor": remote_uri, "status": "online"},
            {"actor": local.uri, "status": "offline"},
        ],
    )
    monkeypatch.setattr(
        member_agents,
        "chat_directory_for_app",
        lambda _app: {
            "targets": [
                {
                    "targetKind": "agent", "targetUri": remote_uri, "actor": remote_uri,
                    "status": "unknown", "deliverable": True,
                    "agentId": "a." + "p" * 26, "displayName": "remote builder",
                    "orgs": ["group-team"],
                },
                {
                    "targetKind": "agent", "targetUri": local.uri, "actor": local.uri,
                    "status": "unknown", "deliverable": True,
                    "agentId": "a." + "q" * 26, "displayName": "directory name",
                    "orgs": ["group-team"],
                },
            ],
            "orgDirectoryStatus": {},
        },
    )

    result = METHOD_REGISTRY.invoke(daemon, "targets", {"kind": "agent"})
    rows = result["targets"]

    assert sum(row["targetUri"] == remote_uri for row in rows) == 1
    assert sum(row["targetUri"] == local.uri for row in rows) == 1
    by_uri = {row["targetUri"]: row for row in rows}
    assert by_uri[remote_uri]["status"] == "online"
    assert by_uri[remote_uri]["agentId"] == "a." + "p" * 26
    assert by_uri[remote_uri]["displayName"] == "remote builder"
    assert by_uri[remote_uri]["orgs"] == ["group-team"]
    assert by_uri[local.uri]["status"] == "offline"
    assert by_uri[local.uri]["agentId"] == local.agent_id
    assert by_uri[local.uri]["displayName"] == "local-builder"
    assert by_uri[local.uri]["orgs"] == ["group-team"]


def test_resume_snapshots_carry_the_id_and_display_name(daemon) -> None:
    result = METHOD_REGISTRY.invoke(
        daemon, "agent.create", {"name": "resumer", "includeResumeSnapshot": True}
    )

    agent = daemon.agents.require("resumer")
    assert result["resumePreviousSnapshot"] is None
    assert result["resumeSnapshot"]["agentId"] == agent.agent_id
    assert result["resumeSnapshot"]["displayName"] is None


class _OrgApp(_FakeApp):
    def _forwarding_status_json(self) -> dict[str, object]:
        # The real sidecar policy shape the closed org.network schema declares.
        return {"policy": {"mode": "auto", "source": "default"}, "peers": []}


@pytest.fixture
def org_app(tmp_path: Path, request) -> Iterator[_FakeApp]:
    provisioned = request.param
    runtime = OrgFsRuntime(tmp_path / "state", node_id=NODE_ID, author=f"user:{OWNER}")
    write_device_record(
        tmp_path,
        DeviceRecord(
            device_id=NODE_ID,
            owner=OWNER,
            server_public=key("server-node-a"),
            client_public=key("client-node-a"),
            device_uid=DEVICE_UID if provisioned else None,
            name="laptop" if provisioned else None,
        ),
    )
    address_path(tmp_path).write_text("tcp://10.0.0.9:7447#ipc-psk")
    try:
        yield _OrgApp(runtime, tmp_path)
    finally:
        runtime.close()


@pytest.mark.parametrize("org_app", [True, False], ids=["uid", "no-uid"], indirect=True)
def test_org_network_self_and_directory_rows_carry_the_device_uid(
    org_app, minting, request
) -> None:
    uid = DEVICE_UID if request.node.callspec.params["org_app"] else None
    ipc_org_create(org_app, {"org": "group-acme"})

    view = METHOD_REGISTRY.invoke(org_app, "org.network", {})

    assert view["self"]["deviceUid"] == uid
    (row,) = view["directory"]["group-acme"]
    assert row["deviceId"] == NODE_ID
    # This device publishes its uid only once id-form minting is on.
    if minting and uid is not None:
        assert row["deviceUid"] == uid
    else:
        assert "deviceUid" not in row
