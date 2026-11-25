# Decision record (file header; no inline comments by repository rule):
# - OC5 RED/GREEN contract tests for dynamic agent org labels, per-(agent, org)
#   exclusions in state/org-agent-exclusions.json, local OrgCellActor maintenance,
#   transfer carry-over, A2 delivery openness, and explicit record deletion.
"""OC5 dynamic agent org labels, per-(agent, org) exclusions, and deletion closure."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.application.messaging.status.views import _StatusViewsMixin
from hyprial.daemon.impl.composition.actors import ActorHostRegistry
from hyprial.daemon.impl.dispatch.admission import dispatch_gate
from hyprial.daemon.impl.org.cell_actor import ensure_org_cell_actors
from hyprial.daemon.impl.org.membership import OrgMembershipStore
from hyprial.daemon.impl.org.network.member_agents import (
    MemberAgentDirectory,
    chat_targets_for_app,
    publish_for_app,
)
from hyprial.daemon.impl.orgfs.api import MemberInfo
from hyprial.daemon.impl.teleport.source import SourceAgent
from hyprial.daemon.impl.teleport.wiring.source_export import export_agent_stream
from hyprial.daemon.impl.teleport.stream import unpack_stream
from hyprial.daemon.impl.transfer.archive.envelope import (
    ensure_recipient_key,
    recipient_key_path,
)
from hyprial.daemon.impl.transfer.landing.registry import land_bundle_into_registry
from hyprial.daemon.impl.user_agent import USER_AGENT_NAME, user_agent_capabilities
from hyprial.identity import (
    AgentRegistry,
    OrgMembership,
)
from hyprial.kernel import canonical_user_uri, is_agent_id_segment
from tests.daemon.org_membership.storage.support import FakeFacade

OWNER = "alice@example.test"
NODE_ID = "node-1"
AGENT_A = "a." + "a" * 26
AGENT_B = "a." + "b" * 26
USER_AGENT_ID = "a." + "u" * 26


class _DirectoryFacade(FakeFacade):
    def __init__(self, *, owner: str, active_users: set[str] | None = None) -> None:
        super().__init__(owner=owner)
        self.active_users = set(active_users or ())
        self.authors: dict[tuple[str, str], str] = {}

    def members(self, space_id: str) -> tuple[MemberInfo, ...]:
        return tuple(
            MemberInfo(space_id, user, "write", self.owner, "2026-11-18T00:00:00Z")
            for user in sorted(self.active_users)
        )

    def write_text(self, space_id: str, path: str, content: str, **kwargs: Any) -> Any:
        node = super().write_text(space_id, path, content, **kwargs)
        self.authors[(space_id, path)] = self.owner
        return node

    def document_authors(self, space_id: str, node: str) -> tuple[str, ...]:
        path = self.stat(space_id, node).path
        return (self.authors[(space_id, path)],)


def _agent_obj(agent_id: str, actor: str, *, user_agent: bool = False) -> SimpleNamespace:
    capabilities = user_agent_capabilities() if user_agent else {}
    return SimpleNamespace(
        agent_id=agent_id,
        uri=f"agent:{OWNER}:{NODE_ID}:{agent_id}",
        actor=actor,
        owner=OWNER,
        machine=NODE_ID,
        capabilities=capabilities,
    )


def _app_for_orgs(
    tmp_path: Path,
    facade: _DirectoryFacade,
    agents: tuple[SimpleNamespace, ...],
    *,
    owner: str = OWNER,
    left_orgs: tuple[str, ...] = (),
) -> SimpleNamespace:
    tmp_path.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    (home / "state").mkdir(mode=0o700, exist_ok=True)
    if left_orgs:
        (home / "state" / "left-orgs.json").write_text(
            json.dumps(list(left_orgs)),
            encoding="utf-8",
        )
    return SimpleNamespace(
        owner=owner,
        user_id="aliceuid000000000000000000",
        node_id=NODE_ID,
        hyprial_home=home,
        state_dir=home / "state",
        agents=SimpleNamespace(list=lambda: agents),
        _require_orgfs_runtime=lambda: SimpleNamespace(
            facade=facade,
            node_id=NODE_ID,
            status=lambda _space_id: SimpleNamespace(holders_online=(NODE_ID,)),
        ),
        _actor_hosts=ActorHostRegistry(),
        _log=lambda *_args, **_kwargs: None,
    )


def _active_work_space(facade: _DirectoryFacade, org: str) -> Any:
    space = facade.create_space(org)
    facade.update_space_meta(space.space_id, {"org": org, "kind": "work"})
    facade.mkdir(space.space_id, "directory")
    facade.write_text(space.space_id, "directory/org.json", '{"state":"active"}')
    return space


def test_exclusion_store_persists_expected_schema_and_validates(tmp_path: Path) -> None:
    from hyprial.daemon.impl.org.network.member_agents.exclusions import (
        OrgAgentExclusion,
        OrgAgentExclusionStore,
        exclusions_path,
    )

    home = tmp_path / "hyprial-home"
    store = OrgAgentExclusionStore(home)
    assert store.list() == ()

    assert store.exclude(AGENT_A, "space-alpha") is True
    assert store.exclude(AGENT_A, "space-alpha") is False
    assert store.exclude(AGENT_B, "space-beta") is True

    path = exclusions_path(home)
    assert path == home / "state" / "org-agent-exclusions.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == [
        {"agentId": AGENT_A, "directorySpace": "space-alpha"},
        {"agentId": AGENT_B, "directorySpace": "space-beta"},
    ]
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.excluded_agent_ids("space-alpha") == frozenset({AGENT_A})
    assert store.excluded_spaces(AGENT_A) == frozenset({"space-alpha"})
    assert store.list() == (
        OrgAgentExclusion(agent_id=AGENT_A, directory_space="space-alpha"),
        OrgAgentExclusion(agent_id=AGENT_B, directory_space="space-beta"),
    )

    assert store.include(AGENT_A, "space-alpha") is True
    assert store.include(AGENT_A, "space-alpha") is False
    assert store.excluded_agent_ids("space-alpha") == frozenset()

    with pytest.raises(ValueError):
        OrgAgentExclusion.from_json({"agentId": "not-an-id", "directorySpace": "space-alpha"})
    with pytest.raises(ValueError):
        OrgAgentExclusion.from_json({"agentId": AGENT_A, "directorySpace": ""})
    with pytest.raises(ValueError):
        OrgAgentExclusion.from_json({"agentId": AGENT_A, "directorySpace": "space-alpha", "extra": 1})


def test_derive_agent_org_labels_follows_owner_membership_for_ordinary_and_user_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network import member_agents
    from hyprial.daemon.impl.org.network.member_agents.exclusions import (
        OrgAgentExclusionStore,
        derive_agent_org_labels,
    )

    facade = _DirectoryFacade(owner=OWNER)
    alpha = _active_work_space(facade, "group-alpha")
    beta = _active_work_space(facade, "group-beta")
    worker = _agent_obj(AGENT_A, "worker")
    user_agent = _agent_obj(USER_AGENT_ID, USER_AGENT_NAME, user_agent=True)
    app = _app_for_orgs(tmp_path, facade, (worker, user_agent))

    monkeypatch.setattr(
        member_agents.exclusions,
        "for_app",
        lambda _app, repair_current=False: SimpleNamespace(
            active={
                "group-alpha": {"org": "group-alpha", "spaceId": alpha.space_id},
                "group-beta": {"org": "group-beta", "spaceId": beta.space_id},
            }
        ),
    )

    assert derive_agent_org_labels(app, AGENT_A) == ("group-alpha", "group-beta")
    assert derive_agent_org_labels(app, USER_AGENT_ID) == ("group-alpha", "group-beta")

    store = OrgAgentExclusionStore(app.hyprial_home)
    store.exclude(AGENT_A, alpha.space_id)
    store.exclude(USER_AGENT_ID, beta.space_id)

    assert derive_agent_org_labels(app, AGENT_A) == ("group-beta",)
    assert derive_agent_org_labels(app, USER_AGENT_ID) == ("group-alpha",)

    monkeypatch.setattr(
        member_agents.exclusions,
        "for_app",
        lambda _app, repair_current=False: SimpleNamespace(active={}),
    )
    assert derive_agent_org_labels(app, AGENT_A) == ()
    assert derive_agent_org_labels(app, USER_AGENT_ID) == ()


@pytest.mark.parametrize("role_mode", ["owner", "member"])
def test_org_cell_actor_maintains_exclusions_and_republishes_member_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role_mode: str
) -> None:
    from hyprial.daemon.impl.org.network import member_agents

    org_owner = OWNER if role_mode == "owner" else "boss@example.test"
    facade = _DirectoryFacade(owner=org_owner, active_users={OWNER})
    space = _active_work_space(facade, "group-team")
    beta = _active_work_space(facade, "group-other")
    facade.owner = OWNER

    worker_a = _agent_obj(AGENT_A, "worker-a")
    worker_b = _agent_obj(AGENT_B, "worker-b")
    app = _app_for_orgs(tmp_path, facade, (worker_a, worker_b))

    monkeypatch.setattr(
        member_agents,
        "for_app",
        lambda _app, repair_current=False: SimpleNamespace(
            active={
                "group-team": {"org": "group-team", "spaceId": space.space_id},
                "group-other": {"org": "group-other", "spaceId": beta.space_id},
            }
        ),
    )
    monkeypatch.setattr(
        member_agents,
        "OrgFsDirectoryStore",
        lambda _fs, **_kwargs: SimpleNamespace(
            list_devices=lambda _org: (SimpleNamespace(device_id=NODE_ID),)
        ),
    )

    assert publish_for_app(app, ["group-team", "group-other"]) == {
        "published": ["group-team", "group-other"],
        "errors": [],
    }

    service = SimpleNamespace(
        _orgs=lambda: ["group-team", "group-other"],
        _store=None,
    )
    actors = ensure_org_cell_actors(app, service_factory=lambda: service)
    try:
        actor = actors.for_org("group-team")
        if role_mode == "member":
            assert actor.snapshot().writable is False
        excluded = actor.exclude_agent(AGENT_A)
        assert excluded["excluded"] is True
        assert excluded["changed"] is True
        assert actor.agent_exclusions() == (AGENT_A,)

        listed_team = MemberAgentDirectory(facade).list("group-team")
        assert [row["agentId"] for row in listed_team["agents"]] == [AGENT_B]
        listed_other = MemberAgentDirectory(facade).list("group-other")
        assert [row["agentId"] for row in listed_other["agents"]] == [AGENT_A, AGENT_B]

        targets = {row["agentId"]: row for row in chat_targets_for_app(app)}
        assert targets[AGENT_A]["orgs"] == ["group-other"]
        assert targets[AGENT_B]["orgs"] == ["group-other", "group-team"]

        included = actor.include_agent(AGENT_A)
        assert included["excluded"] is False
        assert included["changed"] is True
        assert actor.agent_exclusions() == ()
        listed_after = MemberAgentDirectory(facade).list("group-team")
        assert [row["agentId"] for row in listed_after["agents"]] == [AGENT_A, AGENT_B]
    finally:
        actors.close()


def test_a2_excluded_agent_still_appears_deliverable_in_targets_without_org_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network import member_agents
    from hyprial.daemon.impl.org.network.member_agents.exclusions import OrgAgentExclusionStore

    facade = _DirectoryFacade(owner=OWNER)
    space = _active_work_space(facade, "group-team")
    worker = _agent_obj(AGENT_A, "worker")
    app = _app_for_orgs(tmp_path, facade, (worker,))
    OrgAgentExclusionStore(app.hyprial_home).exclude(AGENT_A, space.space_id)

    monkeypatch.setattr(
        member_agents,
        "for_app",
        lambda _app, repair_current=False: SimpleNamespace(
            active={"group-team": {"org": "group-team", "spaceId": space.space_id}}
        ),
    )
    monkeypatch.setattr(
        member_agents,
        "OrgFsDirectoryStore",
        lambda _fs, **_kwargs: SimpleNamespace(
            list_devices=lambda _org: (SimpleNamespace(device_id=NODE_ID),)
        ),
    )
    publish_for_app(app, ["group-team"])
    assert MemberAgentDirectory(facade).list("group-team")["agents"] == []

    local_uri = worker.uri
    fake_app = SimpleNamespace(
        owner=OWNER,
        node_id=NODE_ID,
        hyprial_home=app.hyprial_home,
        agents=SimpleNamespace(
            list=lambda: (
                SimpleNamespace(
                    uri=local_uri,
                    actor="worker",
                    hosted_by=None,
                    capabilities={},
                    to_json=lambda: {"agentId": AGENT_A},
                    display="worker",
                    owner=OWNER,
                    machine=NODE_ID,
                    agent_id=AGENT_A,
                ),
            )
        ),
        _require_orgfs_runtime=app._require_orgfs_runtime,
        _worker_status_snapshot=lambda: contextlib.nullcontext(),
        _actor_status_snapshot=lambda: [{"actor": local_uri, "status": "online"}],
        load_persistent_configuration=lambda: SimpleNamespace(
            channels=SimpleNamespace(gateways=())
        ),
        _visibility_caller=lambda _params: None,
    )
    targets = _StatusViewsMixin._ipc_targets(fake_app, {"kind": "agent"})["targets"]
    assert len(targets) == 1
    assert targets[0]["targetUri"] == local_uri
    assert targets[0]["deliverable"] is True
    assert "orgs" not in targets[0]
    assert dispatch_gate(
        target=local_uri,
        capabilities={"interactive": False},
        role="execute",
        emit=lambda *_args, **_kwargs: None,
        source="test",
    ) == ()


def test_agent_migration_carries_exclusions_to_target_machine(tmp_path: Path) -> None:
    from hyprial.daemon.impl.org.network.member_agents.exclusions import (
        TRANSFER_EXCLUSIONS_RELPATH,
        OrgAgentExclusionStore,
    )

    source_home = tmp_path / "source-home"
    source_home.mkdir(mode=0o700, exist_ok=True)
    source_reg = AgentRegistry(
        source_home / "agents.sqlite3",
        owner=OWNER,
        machine="node-src",
        hyprial_home=source_home,
    )
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700, exist_ok=True)
    target_reg = AgentRegistry(
        target_home / "agents.sqlite3",
        owner=OWNER,
        machine="node-dst",
        hyprial_home=target_home,
    )
    try:
        created = source_reg.create("worker", cwd=str(source_home / "agents" / "worker" / "workspace"))
        (source_home / "agents" / "worker" / "workspace").mkdir(parents=True, exist_ok=True)
        source_exclusions = OrgAgentExclusionStore(source_home)
        source_exclusions.exclude(created.agent_id, "space-111")
        source_exclusions.exclude(created.agent_id, "space-222")

        _priv, recipient_pub = ensure_recipient_key(recipient_key_path(target_home))
        source_app = SimpleNamespace(
            owner=OWNER,
            node_id="node-src",
            hyprial_home=source_home,
            agents=source_reg,
            _agent_registry=source_reg,
        )
        exported = export_agent_stream(
            source_app,
            SourceAgent(
                name="worker",
                uri=created.uri,
                harness="codex",
                home=str(source_home / "agents" / "worker"),
                headless=True,
                args=(),
                session_ref=None,
                cwd=str(source_home / "agents" / "worker" / "workspace"),
                adapter_bindings=(),
            ),
            {
                "name": "worker",
                "harness": "codex",
                "cwd": str(source_home / "agents" / "worker" / "workspace"),
                "args": [],
                "headless": True,
            },
            recipient_public_key=recipient_pub,
            workdir=tmp_path / "export-work",
        )
        unpacked = tmp_path / "unpacked"
        unpack_stream(exported.path, unpacked, total=exported.total)

        landed = land_bundle_into_registry(
            unpacked,
            registry=target_reg,
            home_root=target_home / "agents",
            machine_home=target_home,
            staging=tmp_path / "staging",
            granted_by=canonical_user_uri(OWNER),
            expected_actor="worker",
            harness="codex",
        )
        target_row = target_reg.require("worker")
        assert is_agent_id_segment(target_row.agent_id)
        assert not (landed.destination / TRANSFER_EXCLUSIONS_RELPATH).exists()
        assert OrgAgentExclusionStore(target_home).excluded_spaces(target_row.agent_id) == frozenset(
            {"space-111", "space-222"}
        )
    finally:
        source_reg.close()
        target_reg.close()


def test_explicit_agent_org_membership_surface_is_deleted() -> None:
    for removed_identity in (
        "AgentMembership",
        "MembershipRecords",
        "agent_membership_path",
        "agent_membership_author_allowed",
        "agent_owner_allowed_in_org",
    ):
        assert not hasattr(OrgMembership, removed_identity), removed_identity

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hyprial.daemon.impl.org.membership.projection")

    for removed_store_attr in ("read_agents", "read_agent", "put_agent"):
        assert not hasattr(OrgMembershipStore, removed_store_attr), removed_store_attr



def _test_agent_id(tag: str, index: int) -> str:
    suffix = f"{index:04d}".translate(str.maketrans("0123456789", "abcdefghij"))
    body = (tag + suffix + "z" * 26)[:26]
    return f"a.{body}"


def test_exclusion_store_two_process_concurrent_add_remove_preserves_updates(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.org.network.member_agents.exclusions import (
        OrgAgentExclusion,
        OrgAgentExclusionStore,
        exclusions_path,
    )

    home = tmp_path / "mp-home"
    store = OrgAgentExclusionStore(home)
    shared_to_remove = [
        (_test_agent_id("rem", i), "space-shared") for i in range(6)
    ]
    shared_to_keep = [
        (_test_agent_id("keep", i), "space-shared") for i in range(6)
    ]
    for agent_id, space_id in shared_to_remove + shared_to_keep:
        store.exclude(agent_id, space_id)

    adds_one = [(_test_agent_id("pa", i), "space-p1") for i in range(25)]
    adds_two = [(_test_agent_id("pb", i), "space-p2") for i in range(25)]
    removes_one = shared_to_remove[:3]
    removes_two = shared_to_remove[3:]

    worker_code = (
        "import json, sys, time; "
        "from pathlib import Path; "
        "from hyprial.daemon.impl.org.network.member_agents.exclusions import OrgAgentExclusionStore; "
        "cfg = json.loads(sys.argv[1]); "
        "Path(cfg['ready']).write_text('1', encoding='utf-8'); "
        "go = Path(cfg['go']); "
        "deadline = time.monotonic() + 10.0; "
        "\nwhile not go.exists():\n"
        "    if time.monotonic() > deadline:\n"
        "        raise TimeoutError('go signal timeout')\n"
        "    time.sleep(0.001)\n"
        "store = OrgAgentExclusionStore(Path(cfg['home']));\n"
        "for a, s in cfg['adds']:\n"
        "    store.exclude(a, s)\n"
        "for a, s in cfg['removes']:\n"
        "    store.include(a, s)\n"
    )
    ready_one = tmp_path / "ready-1"
    ready_two = tmp_path / "ready-2"
    go_flag = tmp_path / "go"
    cfg_one = json.dumps(
        {
            "home": str(home),
            "ready": str(ready_one),
            "go": str(go_flag),
            "adds": adds_one,
            "removes": removes_one,
        }
    )
    cfg_two = json.dumps(
        {
            "home": str(home),
            "ready": str(ready_two),
            "go": str(go_flag),
            "adds": adds_two,
            "removes": removes_two,
        }
    )
    p1 = subprocess.Popen([sys.executable, "-c", worker_code, cfg_one])
    p2 = subprocess.Popen([sys.executable, "-c", worker_code, cfg_two])
    try:
        deadline = time.monotonic() + 10.0
        while not (ready_one.exists() and ready_two.exists()):
            assert time.monotonic() < deadline
            time.sleep(0.005)
        go_flag.write_text("go", encoding="utf-8")
        assert p1.wait(timeout=30.0) == 0
        assert p2.wait(timeout=30.0) == 0
    finally:
        for proc in (p1, p2):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)

    expected = sorted(
        [OrgAgentExclusion(a, s) for a, s in shared_to_keep + adds_one + adds_two],
        key=lambda row: (row.agent_id, row.directory_space),
    )
    assert list(store.list()) == expected
    lock_candidates = [
        item
        for item in exclusions_path(home).parent.iterdir()
        if item.name != exclusions_path(home).name and "lock" in item.name
    ]
    assert lock_candidates


def test_exclusion_store_rejects_file_and_directory_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network.member_agents import exclusions as mod

    storage_error = mod.OrgAgentExclusionStorageError
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(mode=0o700)
    outside_file = outside_dir / "org-agent-exclusions.json"
    outside_payload = json.dumps(
        [{"agentId": AGENT_A, "directorySpace": "space-external"}]
    )
    outside_file.write_text(outside_payload, encoding="utf-8")
    outside_file.chmod(0o600)

    home_file_link = tmp_path / "home-file-link"
    home_file_link.mkdir(mode=0o700)
    (home_file_link / "state").mkdir(mode=0o700)
    (home_file_link / "state" / "org-agent-exclusions.json").symlink_to(outside_file)
    file_store = mod.OrgAgentExclusionStore(home_file_link)

    with pytest.raises(storage_error):
        file_store.list()
    with pytest.raises(storage_error):
        file_store.excluded_spaces(AGENT_A)
    with pytest.raises(storage_error):
        file_store.exclude(AGENT_B, "space-local")
    with pytest.raises(storage_error):
        file_store.include(AGENT_A, "space-external")
    with pytest.raises(storage_error):
        file_store.adopt_for_agent(AGENT_B, ("space-local",))
    assert json.loads(outside_file.read_text(encoding="utf-8")) == json.loads(
        outside_payload
    )

    home_dir_link = tmp_path / "home-dir-link"
    home_dir_link.mkdir(mode=0o700)
    (home_dir_link / "state").symlink_to(outside_dir, target_is_directory=True)
    dir_store = mod.OrgAgentExclusionStore(home_dir_link)

    with pytest.raises(storage_error):
        dir_store.list()
    with pytest.raises(storage_error):
        dir_store.exclude(AGENT_B, "space-local")
    with pytest.raises(storage_error):
        dir_store.adopt_for_agent(AGENT_B, ("space-local",))

    home_root_link = tmp_path / "home-root-link"
    outside_root = tmp_path / "outside-root"
    outside_root.mkdir(mode=0o700)
    (outside_root / "state").mkdir(mode=0o700)
    (outside_root / "state" / "org-agent-exclusions.json").write_text(
        outside_payload, encoding="utf-8"
    )
    (outside_root / "state" / "org-agent-exclusions.json").chmod(0o600)
    home_root_link.symlink_to(outside_root, target_is_directory=True)
    root_store = mod.OrgAgentExclusionStore(home_root_link)

    with pytest.raises(storage_error):
        root_store.list()
    with pytest.raises(storage_error):
        root_store.exclude(AGENT_B, "space-local")

    facade = _DirectoryFacade(owner=OWNER)
    space = _active_work_space(facade, "group-team")
    app = _app_for_orgs(tmp_path / "app-wrap", facade, (_agent_obj(AGENT_A, "worker"),))
    (app.hyprial_home / "state" / "org-agent-exclusions.json").symlink_to(outside_file)
    monkeypatch.setattr(
        mod,
        "for_app",
        lambda _app, repair_current=False: SimpleNamespace(
            active={"group-team": {"org": "group-team", "spaceId": space.space_id}}
        ),
    )
    with pytest.raises(storage_error):
        mod.derive_agent_org_labels(app, AGENT_A)


def test_read_carried_exclusions_rejects_symlinks(tmp_path: Path) -> None:
    from hyprial.daemon.impl.org.network.member_agents import exclusions as mod

    storage_error = mod.OrgAgentExclusionStorageError
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    outside_file = outside / "org-agent-exclusions.json"
    outside_file.write_text(
        json.dumps([{"agentId": AGENT_A, "directorySpace": "space-external"}]),
        encoding="utf-8",
    )
    outside_file.chmod(0o600)

    carried_file_link_home = tmp_path / "carried-file-link"
    carried_file_link_home.mkdir(mode=0o700)
    (carried_file_link_home / "state").mkdir(mode=0o700)
    (carried_file_link_home / "state" / "org-agent-exclusions.json").symlink_to(
        outside_file
    )
    with pytest.raises(storage_error):
        mod.read_carried_exclusion_spaces(carried_file_link_home)

    carried_dir_link_home = tmp_path / "carried-dir-link"
    carried_dir_link_home.mkdir(mode=0o700)
    (carried_dir_link_home / "state").symlink_to(outside, target_is_directory=True)
    with pytest.raises(storage_error):
        mod.read_carried_exclusion_spaces(carried_dir_link_home)


def _patch_wrong_owner_for_existing_path(
    monkeypatch: pytest.MonkeyPatch, target: Path
) -> None:
    real = target.lstat()
    target_id = (real.st_dev, real.st_ino)
    fake_uid = os.getuid() + 1
    orig_lstat = Path.lstat
    orig_fstat = os.fstat

    def _with_fake_uid(st: os.stat_result) -> os.stat_result:
        if (st.st_dev, st.st_ino) != target_id:
            return st
        return os.stat_result((
            st.st_mode, st.st_ino, st.st_dev, st.st_nlink, fake_uid,
            st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime,
        ))

    monkeypatch.setattr(Path, "lstat", lambda self: _with_fake_uid(orig_lstat(self)))
    monkeypatch.setattr(os, "fstat", lambda fd: _with_fake_uid(orig_fstat(fd)))


def _patch_post_open_fstat_override(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    *,
    nlink: int | None = None,
    mode: int | None = None,
) -> None:
    target_resolved = os.path.normcase(os.path.abspath(os.fspath(target)))
    tracked_fds: set[int] = set()
    orig_open = os.open
    orig_close = os.close
    orig_fstat = os.fstat

    def spy_open(path: os.PathLike[str] | str, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = orig_open(path, flags, *args, **kwargs)
        if os.path.normcase(os.path.abspath(os.fspath(path))) == target_resolved:
            tracked_fds.add(fd)
        return fd

    def spy_close(fd: int) -> None:
        tracked_fds.discard(fd)
        orig_close(fd)

    def spy_fstat(fd: int) -> os.stat_result:
        real = orig_fstat(fd)
        if fd not in tracked_fds:
            return real
        patched_mode = real.st_mode if mode is None else (stat.S_IFMT(real.st_mode) | mode)
        patched_nlink = real.st_nlink if nlink is None else nlink
        return os.stat_result((
            patched_mode, real.st_ino, real.st_dev, patched_nlink, real.st_uid,
            real.st_gid, real.st_size, real.st_atime, real.st_mtime, real.st_ctime,
        ))

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", spy_close)
    monkeypatch.setattr(os, "fstat", spy_fstat)


def test_exclusion_store_rejects_broad_or_wrong_owner_metadata_on_read_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network.member_agents import exclusions as mod

    storage_error = mod.OrgAgentExclusionStorageError

    home_broad = tmp_path / "home-broad"
    home_broad.mkdir()
    home_broad.chmod(0o755)
    store_broad = mod.OrgAgentExclusionStore(home_broad)
    for op in (
        lambda: store_broad.list(),
        lambda: store_broad.excluded_spaces(AGENT_A),
        lambda: store_broad.exclude(AGENT_A, "space-1"),
        lambda: store_broad.adopt_for_agent(AGENT_A, ("space-1",)),
    ):
        with pytest.raises(storage_error):
            op()
    assert stat.S_IMODE(home_broad.stat().st_mode) == 0o755

    state_under_broad_home = home_broad / "state"
    state_under_broad_home.mkdir()
    state_under_broad_home.chmod(0o700)
    data_under_broad_home = state_under_broad_home / "org-agent-exclusions.json"
    data_under_broad_home.write_text("[]", encoding="utf-8")
    data_under_broad_home.chmod(0o600)
    for op in (
        lambda: store_broad.list(),
        lambda: store_broad.excluded_spaces(AGENT_A),
        lambda: store_broad.exclude(AGENT_A, "space-1"),
        lambda: store_broad.adopt_for_agent(AGENT_A, ("space-1",)),
    ):
        with pytest.raises(storage_error):
            op()
    assert stat.S_IMODE(home_broad.stat().st_mode) == 0o755

    home_broad_state = tmp_path / "home-broad-state"
    home_broad_state.mkdir()
    home_broad_state.chmod(0o700)
    broad_state = home_broad_state / "state"
    broad_state.mkdir()
    broad_state.chmod(0o755)
    data_in_broad_state = broad_state / "org-agent-exclusions.json"
    data_in_broad_state.write_text("[]", encoding="utf-8")
    data_in_broad_state.chmod(0o600)
    store_broad_state = mod.OrgAgentExclusionStore(home_broad_state)
    with pytest.raises(storage_error):
        store_broad_state.list()
    with pytest.raises(storage_error):
        store_broad_state.exclude(AGENT_A, "space-1")
    assert stat.S_IMODE(broad_state.stat().st_mode) == 0o755

    home_broad_file = tmp_path / "home-broad-file"
    home_broad_file.mkdir()
    home_broad_file.chmod(0o700)
    private_state = home_broad_file / "state"
    private_state.mkdir()
    private_state.chmod(0o700)
    broad_file = private_state / "org-agent-exclusions.json"
    broad_file.write_text(
        json.dumps([{"agentId": AGENT_A, "directorySpace": "space-1"}]),
        encoding="utf-8",
    )
    broad_file.chmod(0o644)
    store_broad_file = mod.OrgAgentExclusionStore(home_broad_file)
    with pytest.raises(storage_error):
        store_broad_file.list()
    with pytest.raises(storage_error):
        store_broad_file.exclude(AGENT_B, "space-2")
    with pytest.raises(storage_error):
        store_broad_file.adopt_for_agent(AGENT_B, ("space-2",))

    broad_file.chmod(0o600)
    hardlink = tmp_path / "data-hardlink"
    os.link(broad_file, hardlink)
    try:
        with pytest.raises(storage_error):
            store_broad_file.list()
        with pytest.raises(storage_error):
            store_broad_file.exclude(AGENT_B, "space-2")
    finally:
        hardlink.unlink()

    owner_home = tmp_path / "home-wrong-owner"
    owner_home.mkdir()
    owner_home.chmod(0o700)
    owner_store = mod.OrgAgentExclusionStore(owner_home)
    with monkeypatch.context() as mp:
        _patch_wrong_owner_for_existing_path(mp, owner_home)
        with pytest.raises(storage_error):
            owner_store.list()
        with pytest.raises(storage_error):
            owner_store.exclude(AGENT_A, "space-1")

    assert owner_store.exclude(AGENT_A, "space-1") is True
    owner_state = owner_home / "state"
    owner_file = owner_state / "org-agent-exclusions.json"
    for target in (owner_home, owner_state, owner_file):
        with monkeypatch.context() as mp:
            _patch_wrong_owner_for_existing_path(mp, target)
            with pytest.raises(storage_error):
                owner_store.list()
            with pytest.raises(storage_error):
                owner_store.exclude(AGENT_B, "space-2")


def test_exclusion_store_rejects_unsafe_lock_file_on_read_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network.member_agents import exclusions as mod

    storage_error = mod.OrgAgentExclusionStorageError
    home = tmp_path / "lock-home"
    home.mkdir()
    home.chmod(0o700)
    store = mod.OrgAgentExclusionStore(home)
    assert store.exclude(AGENT_A, "space-1") is True
    lock_path = home / "state" / f".{mod.ORG_AGENT_EXCLUSIONS_FILENAME}.lock"
    assert lock_path.exists()

    lock_path.chmod(0o644)
    for op in (
        lambda: store.list(),
        lambda: store.excluded_spaces(AGENT_A),
        lambda: store.exclude(AGENT_B, "space-2"),
        lambda: store.include(AGENT_A, "space-1"),
        lambda: store.adopt_for_agent(AGENT_B, ("space-2",)),
        lambda: store.remove_for_agent(AGENT_A, ("space-1",)),
    ):
        with pytest.raises(storage_error):
            op()
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o644

    lock_path.chmod(0o600)
    lock_hardlink = tmp_path / "lock-hardlink"
    os.link(lock_path, lock_hardlink)
    try:
        with pytest.raises(storage_error):
            store.list()
        with pytest.raises(storage_error):
            store.exclude(AGENT_B, "space-2")
    finally:
        lock_hardlink.unlink()

    outside_lock = tmp_path / "outside-lock"
    outside_lock.write_text("", encoding="utf-8")
    outside_lock.chmod(0o600)
    lock_path.unlink()
    lock_path.symlink_to(outside_lock)
    with pytest.raises(storage_error):
        store.list()
    with pytest.raises(storage_error):
        store.exclude(AGENT_B, "space-2")
    lock_path.unlink()

    assert store.exclude(AGENT_B, "space-2") is True
    assert store.include(AGENT_B, "space-2") is True
    with monkeypatch.context() as mp:
        _patch_wrong_owner_for_existing_path(mp, lock_path)
        with pytest.raises(storage_error):
            store.list()
        with pytest.raises(storage_error):
            store.exclude(AGENT_B, "space-2")

    with monkeypatch.context() as mp:
        _patch_post_open_fstat_override(mp, lock_path, nlink=2)
        with pytest.raises(storage_error):
            store.exclude(AGENT_B, "space-2")
    with monkeypatch.context() as mp:
        _patch_post_open_fstat_override(mp, lock_path, mode=0o644)
        with pytest.raises(storage_error):
            store.exclude(AGENT_B, "space-2")
    assert store.list() == (mod.OrgAgentExclusion(AGENT_A, "space-1"),)


def test_read_carried_exclusions_rejects_broad_wrong_owner_and_hardlinked_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network.member_agents import exclusions as mod

    storage_error = mod.OrgAgentExclusionStorageError
    home = tmp_path / "carried-home"
    home.mkdir()
    home.chmod(0o700)
    state_dir = home / "state"
    state_dir.mkdir()
    state_dir.chmod(0o700)
    carried = state_dir / mod.ORG_AGENT_EXCLUSIONS_FILENAME
    carried.write_text(
        json.dumps([{"agentId": AGENT_A, "directorySpace": "space-111"}]),
        encoding="utf-8",
    )
    carried.chmod(0o600)
    assert mod.read_carried_exclusion_spaces(home) == ("space-111",)

    home.chmod(0o755)
    with pytest.raises(storage_error):
        mod.read_carried_exclusion_spaces(home)
    home.chmod(0o700)

    state_dir.chmod(0o755)
    with pytest.raises(storage_error):
        mod.read_carried_exclusion_spaces(home)
    state_dir.chmod(0o700)

    carried.chmod(0o644)
    with pytest.raises(storage_error):
        mod.read_carried_exclusion_spaces(home)
    carried.chmod(0o600)

    hardlink = tmp_path / "carried-hardlink"
    os.link(carried, hardlink)
    try:
        with pytest.raises(storage_error):
            mod.read_carried_exclusion_spaces(home)
    finally:
        hardlink.unlink()

    for target in (home, state_dir, carried):
        with monkeypatch.context() as mp:
            _patch_wrong_owner_for_existing_path(mp, target)
            with pytest.raises(storage_error):
                mod.read_carried_exclusion_spaces(home)

    with monkeypatch.context() as mp:
        _patch_post_open_fstat_override(mp, carried, nlink=2)
        with pytest.raises(storage_error):
            mod.read_carried_exclusion_spaces(home)
    with monkeypatch.context() as mp:
        _patch_post_open_fstat_override(mp, carried, mode=0o644)
        with pytest.raises(storage_error):
            mod.read_carried_exclusion_spaces(home)
