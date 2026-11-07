"""Daemon-owned work-item writes, identity fences, and invalid projections."""

from __future__ import annotations

import threading
from functools import wraps
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.messaging import work_items as work_commands
from hyprial.daemon.impl.org.network.ipc import ipc_org_create
from hyprial.daemon.impl.pac.views import work_items
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec, ipc_errors
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)

OWNER = "agent:allen:node-1:owner"
OTHER = "agent:allen:node-1:other"
INTERACTIVE = "agent:allen:node-1:interactive"
GRAPH = "wf-1234567890abcdef1234567890abcdef"
LEASE = "work-item-channel-lease-token-0001"


class _FakeInbox:
    def pending_messages(self, _actor: str) -> tuple[()]:
        return ()


class _FakePresence:
    def online_actors(self) -> tuple[()]:
        return ()


class _FakeHarnesses:
    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str | None] = {}
        self.identities: dict[tuple[str, str], tuple[int, str]] = {}

    def projected_worker_session_refs(self) -> dict[tuple[str, str], str | None]:
        return dict(self.refs)

    def projected_worker_process_identities(
        self,
    ) -> dict[tuple[str, str], tuple[int, str]]:
        return dict(self.identities)

    def status(self) -> tuple[()]:
        return ()


@pytest.fixture
def work_daemon(
    tmp_path: Path,
    own_daemon_application,
    monkeypatch: pytest.MonkeyPatch,
):  # type: ignore[no-untyped-def]
    app = own_daemon_application(
        DaemonApplication(
            state_dir=tmp_path / "state",
            socket_path=tmp_path / "daemon.sock",
            hyprial_home=tmp_path / "home",
            node_id="node-1",
            owner="allen",
            **_deep_module_daemon_dependencies(),
        )
    )
    app._inbox = _FakeInbox()  # type: ignore[assignment]
    app._harnesses = _FakeHarnesses()  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]
    app._restore_done.set()
    app._ipc_peer.pid = 9000
    monkeypatch.setattr(
        work_commands,
        "_read_process_parent",
        lambda pid: 1 if pid == 9000 else 0,
        raising=False,
    )
    monkeypatch.setattr(
        work_commands,
        "process_birth_identity",
        lambda pid: f"birth:{pid}",
        raising=False,
    )
    space_id = app._handle_orgfs("orgfs.create", {"name": "work"})["space_id"]
    app._handle_orgfs("orgfs.mkdir", {"spaceId": space_id, "path": "work"})
    return app, space_id


def _register_worker(app: DaemonApplication, actor: str, session_ref: str) -> None:
    name = actor.rsplit(":", 1)[-1]
    app.desired_state.upsert_harness(
        HarnessLaunchSpec(
            harness="claude", name=name, headless=True, session_ref=session_ref
        )
    )
    assert isinstance(app._harnesses, _FakeHarnesses)
    app._harnesses.refs[("claude", name)] = session_ref


def _session(actor: str, session_ref: str) -> dict[str, str]:
    return {"actor": actor, "sessionRef": session_ref}


def _add(
    app: DaemonApplication,
    space_id: str,
    *,
    identity: dict[str, str],
    owner: str | None = None,
    checklist: list[str] | None = None,
    acceptance: str = "",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "spaceId": space_id,
        "title": "Daemon-owned work",
        "checklist": checklist or [],
        "depends": [],
        "acceptance": acceptance,
        **identity,
    }
    if owner is not None:
        params["owner"] = owner
    return app.handle("work.add", params)


def test_done_uses_verified_daemon_session_not_spoofable_environment(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = work_daemon
    _register_worker(app, OWNER, "owner-session")
    added = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
        checklist=["ship"],
    )
    item_id = added["item"]["id"]
    app.handle(
        "work.check",
        {"spaceId": space_id, "itemId": item_id, "number": 1, **_session(OWNER, "owner-session")},
    )

    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", OWNER)
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", "forged")
    with pytest.raises(DaemonRequestError) as spoofed:
        app.handle(
            "work.done",
            {"spaceId": space_id, "itemId": item_id, **_session(OWNER, "forged")},
        )
    assert spoofed.value.code == "PAC_PRINCIPAL_UNVERIFIED"

    _register_worker(app, OTHER, "other-session")
    with pytest.raises(DaemonRequestError) as wrong_owner:
        app.handle(
            "work.done",
            {
                "spaceId": space_id,
                "itemId": item_id,
                **_session(OTHER, "other-session"),
            },
        )
    assert wrong_owner.value.code == "WORK_ITEM_OWNER_REQUIRED"

    done = app.handle(
        "work.done",
        {"spaceId": space_id, "itemId": item_id, **_session(OWNER, "owner-session")},
    )
    assert done["item"]["status"] == "done"
    assert done["item"]["unverifiedOperator"] is False
    assert done["item"]["ownerActions"][-1]["verified"] is True


def test_workflow_user_with_a_session_never_becomes_the_operator(work_daemon) -> None:
    app, _space_id = work_daemon

    with pytest.raises(DaemonRequestError) as refused:
        app._workflow_caller({"actor": "user:allen", "sessionRef": "bogus"})

    assert refused.value.code == "PAC_PRINCIPAL_UNVERIFIED"


def test_work_add_refuses_acl_space_and_work_reads_enforce_org_context(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = work_daemon
    from tests.support.tailnet_daemon import membership_services

    # Provision the real ACL space through explicit membership-init, not a
    # permissive org.create replacement. Work/ACL denial assertions stay real.
    with membership_services():
        acl_space_id = ipc_org_create(app, {"org": "group-demo"})["aclSpaceId"]

    with pytest.raises(DaemonRequestError) as refused:
        _add(app, acl_space_id, identity={"actor": "user:allen"})
    assert refused.value.code == ipc_errors.ORGFS_ACL_SPACE_EXTERNAL_MUTATION

    added = _add(app, space_id, identity={"actor": "user:allen"})
    item_id = added["item"]["id"]
    _register_worker(app, OWNER, "owner-session")
    callers: list[str] = []

    def refuse(caller: str) -> None:
        callers.append(caller)
        raise DaemonRequestError("ORG_CONTEXT_NOT_AUTHORIZED", "denied")

    monkeypatch.setattr(app, "_capability_caller", lambda params: params["actor"])
    monkeypatch.setattr(app, "_enforce_org_context", refuse)
    for method, extra in (
        ("work.show", {"itemId": item_id}),
        ("work.ls", {}),
    ):
        with pytest.raises(DaemonRequestError) as denied:
            app.handle(
                method,
                {"spaceId": space_id, **extra, **_session(OWNER, "owner-session")},
            )
        assert denied.value.code == "ORG_CONTEXT_NOT_AUTHORIZED"
    assert callers == [OWNER, OWNER]


def test_a_note_cannot_forge_unverified_operator_state(work_daemon) -> None:
    app, space_id = work_daemon
    added = _add(app, space_id, identity={"actor": "user:allen"})
    noted = app.handle(
        "work.note",
        {
            "spaceId": space_id,
            "itemId": added["item"]["id"],
            "text": "done (unverified operator)",
            "actor": "user:allen",
        },
    )

    assert noted["item"]["unverifiedOperator"] is False


def test_operator_acceptance_at_add_is_structured_logged_and_flagged(work_daemon) -> None:
    app, space_id = work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
        acceptance="owner approves the result",
    )

    assert added["item"]["unverifiedOperator"] is True
    assert added["item"]["ownerActions"] == [
        {
            "action": "acceptance",
            "actor": "user:allen",
            "verified": False,
            "verification": "unverified",
        }
    ]
    assert added["item"]["log"][-1].endswith(
        "user:allen: acceptance set (unverified operator)"
    )


def test_session_present_never_falls_back_to_user_operator(work_daemon) -> None:
    app, space_id = work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
    )
    item_id = added["item"]["id"]

    with pytest.raises(DaemonRequestError) as unregistered:
        app.handle(
            "work.done",
            {
                "spaceId": space_id,
                "itemId": item_id,
                **_session(OWNER, "not-registered"),
            },
        )
    assert unregistered.value.code == "PAC_PRINCIPAL_UNVERIFIED"

    app.handle(
        "session.register",
        {
            "actor": INTERACTIVE,
            "sessionRef": "interactive-session",
            "cwd": "/work/interactive",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
            "channelProtocolVersion": 2,
            "channelLeaseToken": LEASE,
        },
    )
    with pytest.raises(DaemonRequestError) as interactive:
        app.handle(
            "work.done",
            {
                "spaceId": space_id,
                "itemId": item_id,
                **_session(INTERACTIVE, "interactive-session"),
            },
        )
    assert interactive.value.code == "WORK_ITEM_OWNER_REQUIRED"

    _register_worker(app, OWNER, "registered")
    with pytest.raises(DaemonRequestError) as registered_agent:
        app.handle(
            "work.done",
            {
                "spaceId": space_id,
                "itemId": item_id,
                **_session(OWNER, "registered"),
            },
        )
    assert registered_agent.value.code == "WORK_ITEM_OWNER_REQUIRED"

    completed = app.handle(
        "work.done",
        {"spaceId": space_id, "itemId": item_id, "actor": "user:allen"},
    )
    assert completed["item"]["unverifiedOperator"] is True
    assert completed["item"]["log"][-1].endswith(
        "user:allen: done (unverified operator)"
    )


def test_child_of_registered_interactive_session_is_that_agent(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
    )
    app.handle(
        "session.register",
        {
            "actor": INTERACTIVE,
            "sessionRef": "interactive-process",
            "cwd": "/work/interactive",
            "command": ["codex"],
            "source": "codex-app-server",
            "processPid": 200,
            "processIdentity": "birth:200",
        },
    )
    app._ipc_peer.pid = 300
    monkeypatch.setattr(
        work_commands, "_read_process_parent", {300: 200, 200: 1}.get
    )

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "work.done",
            {
                "spaceId": space_id,
                "itemId": added["item"]["id"],
                "actor": "user:allen",
            },
        )

    assert refused.value.code == "WORK_ITEM_OWNER_REQUIRED"


def test_unreadable_process_ancestry_refuses_operator_only_action(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
    )
    app._ipc_peer.pid = 300
    monkeypatch.setattr(work_commands, "_read_process_parent", lambda _pid: None)

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "work.done",
            {
                "spaceId": space_id,
                "itemId": added["item"]["id"],
                "actor": "user:allen",
            },
        )

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert "ancestry" in str(refused.value).lower()


def test_unreadable_process_ancestry_keeps_non_owner_actions_lax(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = work_daemon
    app._ipc_peer.pid = 300
    monkeypatch.setattr(work_commands, "_read_process_parent", lambda _pid: None)

    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
        checklist=["ship"],
    )
    item_id = added["item"]["id"]
    checked = app.handle(
        "work.check",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "number": 1,
            "actor": "user:allen",
        },
    )
    noted = app.handle(
        "work.note",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "text": "ancestry is unavailable",
            "actor": "user:allen",
        },
    )

    assert added["item"]["status"] == "todo"
    assert checked["item"]["checklist"] == [{"text": "ship", "done": True}]
    assert noted["item"]["log"][-1].endswith(
        "user:allen: ancestry is unavailable (unverified operator)"
    )


def test_unreadable_process_ancestry_refuses_owner_acceptance_at_add(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = work_daemon
    app._ipc_peer.pid = 300
    monkeypatch.setattr(work_commands, "_read_process_parent", lambda _pid: None)

    with pytest.raises(DaemonRequestError) as refused:
        _add(
            app,
            space_id,
            identity={"actor": "user:allen"},
            owner="user:allen",
            acceptance="owner approves the result",
        )

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert "ancestry" in str(refused.value).lower()


def test_known_agent_pid_with_incomparable_identity_refuses_owner_only_action(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
    )
    app.handle(
        "session.register",
        {
            "actor": INTERACTIVE,
            "sessionRef": "interactive-process",
            "cwd": "/work/interactive",
            "command": ["codex"],
            "source": "codex-app-server",
            "processPid": 200,
            "processIdentity": "proc-starttime:1234",
        },
    )
    app._ipc_peer.pid = 300
    monkeypatch.setattr(work_commands, "_read_process_parent", {300: 200, 200: 1}.get)
    monkeypatch.setattr(
        work_commands,
        "process_birth_identity",
        lambda _pid: "darwin-starttime:5678",
    )

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "work.done",
            {
                "spaceId": space_id,
                "itemId": added["item"]["id"],
                "actor": "user:allen",
            },
        )

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert "identity" in str(refused.value).lower()


def test_request_pid_is_ignored_in_favor_of_kernel_peer_pid(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
    )
    app.handle(
        "session.register",
        {
            "actor": INTERACTIVE,
            "sessionRef": "interactive-process",
            "cwd": "/work/interactive",
            "command": ["codex"],
            "source": "codex-app-server",
            "processPid": 200,
            "processIdentity": "birth:200",
        },
    )
    app._ipc_peer.pid = 300
    monkeypatch.setattr(work_commands, "_read_process_parent", {300: 1}.get)

    completed = app.handle(
        "work.done",
        {
            "spaceId": space_id,
            "itemId": added["item"]["id"],
            "actor": "user:allen",
            "pid": 200,
        },
    )

    assert completed["item"]["status"] == "done"
    assert completed["item"]["unverifiedOperator"] is True


def test_reused_agent_pid_with_a_different_birth_identity_does_not_match(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
    )
    app.handle(
        "session.register",
        {
            "actor": INTERACTIVE,
            "sessionRef": "interactive-old-process",
            "cwd": "/work/interactive",
            "command": ["codex"],
            "source": "codex-app-server",
            "processPid": 200,
            "processIdentity": "proc-starttime:old",
        },
    )
    app._ipc_peer.pid = 300
    monkeypatch.setattr(work_commands, "_read_process_parent", {300: 200, 200: 1}.get)
    monkeypatch.setattr(
        work_commands,
        "process_birth_identity",
        lambda pid: f"proc-starttime:{pid}",
    )

    completed = app.handle(
        "work.done",
        {
            "spaceId": space_id,
            "itemId": added["item"]["id"],
            "actor": "user:allen",
        },
    )

    assert completed["item"]["status"] == "done"
    assert completed["item"]["unverifiedOperator"] is True


def test_child_of_managed_worker_is_that_agent(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _space_id = work_daemon
    _register_worker(app, OWNER, "worker-session")
    assert isinstance(app._harnesses, _FakeHarnesses)
    app._harnesses.identities[("claude", "owner")] = (200, "birth:200")
    app._ipc_peer.pid = 300
    monkeypatch.setattr(work_commands, "_read_process_parent", {300: 200, 200: 1}.get)

    caller, unverified = app._work_caller({"actor": "user:allen"})

    assert caller == OWNER
    assert unverified is False


@pytest.mark.parametrize("mutation", ["untick", "delete"])
def test_raw_todo_checklist_rewrite_is_invalid(
    work_daemon, mutation: str
) -> None:
    app, space_id = work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        checklist=["first", "second"],
    )
    item_id = added["item"]["id"]
    checked = app.handle(
        "work.check",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "number": 1,
            "actor": "user:allen",
        },
    )
    current = work_items.parse_work_item(
        app._require_orgfs_runtime().facade.read_text(space_id, f"work/{item_id}.md")[0]
    )
    checklist = [dict(entry) for entry in current.head["checklist"]]
    if mutation == "untick":
        checklist[0]["done"] = False
    else:
        del checklist[0]
    rewritten = work_items.replace_head(current, checklist=checklist)
    app._handle_orgfs(
        "orgfs.write",
        {
            "spaceId": space_id,
            "path": f"work/{item_id}.md",
            "text": work_items.serialize_work_item(rewritten),
            "expectVersion": checked["version"],
        },
    )

    shown = app.handle(
        "work.show", {"spaceId": space_id, "itemId": item_id, "actor": "user:allen"}
    )
    assert shown["item"]["invalid"] is True
    assert "checklist" in shown["item"]["reason"]


def test_second_raw_write_does_not_launder_an_earlier_rewrite(work_daemon) -> None:
    app, space_id = work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        checklist=["ship"],
    )
    item_id = added["item"]["id"]
    checked = app.handle(
        "work.check",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "number": 1,
            "actor": "user:allen",
        },
    )
    current = work_items.parse_work_item(
        app._require_orgfs_runtime().facade.read_text(
            space_id, f"work/{item_id}.md"
        )[0]
    )
    first_raw = work_items.replace_head(
        current, checklist=[{"text": "ship", "done": False}]
    )
    first_written = app._handle_orgfs(
        "orgfs.write",
        {
            "spaceId": space_id,
            "path": f"work/{item_id}.md",
            "text": work_items.serialize_work_item(first_raw),
            "expectVersion": checked["version"],
        },
    )
    second_raw = work_items.replace_head(first_raw, assignee="user:other")
    app._handle_orgfs(
        "orgfs.write",
        {
            "spaceId": space_id,
            "path": f"work/{item_id}.md",
            "text": work_items.serialize_work_item(second_raw),
            "expectVersion": first_written["version"],
        },
    )

    shown = app.handle(
        "work.show", {"spaceId": space_id, "itemId": item_id, "actor": "user:allen"}
    )

    assert shown["item"]["invalid"] is True
    assert "checklist" in shown["item"]["reason"]


def test_raw_done_with_all_items_ticked_is_valid_but_unverified(work_daemon) -> None:
    app, space_id = work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        checklist=["ship"],
    )
    item_id = added["item"]["id"]
    checked = app.handle(
        "work.check",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "number": 1,
            "actor": "user:allen",
        },
    )
    current = work_items.parse_work_item(
        app._require_orgfs_runtime().facade.read_text(space_id, f"work/{item_id}.md")[0]
    )
    rewritten = work_items.replace_head(current, status="done", closed=123)
    app._handle_orgfs(
        "orgfs.write",
        {
            "spaceId": space_id,
            "path": f"work/{item_id}.md",
            "text": work_items.serialize_work_item(rewritten),
            "expectVersion": checked["version"],
        },
    )

    shown = app.handle(
        "work.show", {"spaceId": space_id, "itemId": item_id, "actor": "user:allen"}
    )
    assert "invalid" not in shown["item"]
    assert shown["item"]["unverifiedOperator"] is True
    assert shown["item"]["ownerActions"][-1]["verification"] == (
        "unverified (legacy)"
    )


def test_raw_write_cannot_silently_delete_owner_action_record(work_daemon) -> None:
    app, space_id = work_daemon
    _register_worker(app, OWNER, "owner-session")
    added = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
    )
    item_id = added["item"]["id"]
    completed = app.handle(
        "work.done",
        {"spaceId": space_id, "itemId": item_id, **_session(OWNER, "owner-session")},
    )
    current = work_items.parse_work_item(
        app._require_orgfs_runtime().facade.read_text(space_id, f"work/{item_id}.md")[0]
    )
    rewritten = work_items.replace_head(current, ownerActions=[])
    app._handle_orgfs(
        "orgfs.write",
        {
            "spaceId": space_id,
            "path": f"work/{item_id}.md",
            "text": work_items.serialize_work_item(rewritten),
            "expectVersion": completed["version"],
        },
    )

    shown = app.handle(
        "work.show", {"spaceId": space_id, "itemId": item_id, **_session(OWNER, "owner-session")}
    )
    assert shown["item"]["invalid"] is True
    assert shown["item"]["unverifiedOperator"] is True


@pytest.mark.parametrize(
    "body",
    [
        "not a work item",
        """## M-allen-0001 Bad status\n\n```yaml\nid: M-allen-0001\nstatus: bogus\nowner: user:allen\n```\n""",
        """## M-allen-0001 Bad checklist\n\n```yaml\nid: M-allen-0001\nstatus: todo\nowner: user:allen\nchecklist: nope\n```\n""",
        """## M-allen-0001 Incomplete\n\n```yaml\nid: M-allen-0001\nstatus: done\nowner: user:allen\nchecklist:\n  - {text: ship, done: false}\n```\n""",
        """## M-allen-0001 Bad log\n\n```yaml\nid: M-allen-0001\nstatus: todo\nowner: user:allen\n```\n\n## Log\nrewritten\n""",
    ],
)
def test_raw_written_invalid_file_is_flagged_in_show_and_list(
    work_daemon, body: str
) -> None:
    app, space_id = work_daemon
    app._handle_orgfs(
        "orgfs.write",
        {"spaceId": space_id, "path": "work/M-allen-0001.md", "text": body},
    )

    shown = app.handle(
        "work.show", {"spaceId": space_id, "itemId": "M-allen-0001"}
    )
    listed = app.handle("work.ls", {"spaceId": space_id})

    assert shown["item"]["invalid"] is True
    assert shown["item"]["reason"]
    assert listed["items"] == [shown["item"]]


def test_same_id_add_race_has_one_clean_loser(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = work_daemon
    facade = app._require_orgfs_runtime().facade
    facade_type = type(facade)
    original = facade_type.write_text
    barrier = threading.Barrier(2)
    _register_worker(app, OWNER, "race-session")

    @wraps(original)
    def collide(self, space_id, node, content, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("create_only"):
            barrier.wait(timeout=5)
        return original(self, space_id, node, content, **kwargs)

    monkeypatch.setattr(facade_type, "write_text", collide)
    outcomes: list[dict[str, Any] | Exception] = []

    def create() -> None:
        try:
            outcomes.append(
                _add(app, space_id, identity=_session(OWNER, "race-session"))
            )
        except Exception as error:  # noqa: BLE001 - the loser is asserted below
            outcomes.append(error)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    winners = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(winners) == 1, outcomes
    assert len(losers) == 1
    assert isinstance(losers[0], DaemonRequestError)
    assert losers[0].code == "WORK_ITEM_ALREADY_EXISTS"


def test_link_appends_and_logs_once_then_repeats_as_a_noop(work_daemon) -> None:
    app, space_id = work_daemon
    added = _add(app, space_id, identity={"actor": "user:allen"})
    item_id = added["item"]["id"]

    linked = app.handle(
        "work.link",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "graphId": GRAPH,
            "actor": "user:allen",
        },
    )
    repeated = app.handle(
        "work.link",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "graphId": GRAPH,
            "actor": "user:allen",
        },
    )

    assert linked["item"]["pacs"] == [GRAPH]
    assert linked["item"]["log"][-1].endswith(
        f"user:allen: linked {GRAPH} (unverified operator)"
    )
    assert repeated == linked


def test_link_refuses_a_malformed_workflow_id(work_daemon) -> None:
    app, space_id = work_daemon
    added = _add(app, space_id, identity={"actor": "user:allen"})

    with pytest.raises(DaemonRequestError) as malformed:
        app.handle(
            "work.link",
            {
                "spaceId": space_id,
                "itemId": added["item"]["id"],
                "graphId": "not-a-workflow",
                "actor": "user:allen",
            },
        )

    assert malformed.value.code == "WORK_ITEM_INVALID_GRAPH_ID"


def test_keywords_add_remove_round_trips_and_is_logged(work_daemon) -> None:
    app, space_id = work_daemon
    added = _add(app, space_id, identity={"actor": "user:allen"})
    item_id = added["item"]["id"]
    seeded = app.handle(
        "work.keywords",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "add": ["login", "urgent"],
            "remove": [],
            "actor": "user:allen",
        },
    )
    changed = app.handle(
        "work.keywords",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "add": ["security"],
            "remove": ["urgent"],
            "actor": "user:allen",
        },
    )

    assert seeded["item"]["keywords"] == ["login", "urgent"]
    assert changed["item"]["keywords"] == ["login", "security"]
    assert changed["item"]["log"][-1].endswith(
        "user:allen: keywords add=security remove=urgent (unverified operator)"
    )
