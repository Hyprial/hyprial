"""agent_entity: recovery scenarios."""

from __future__ import annotations
from pathlib import Path
import pytest
import hyprial.identity.impl.agents.home.provisioner as agent_home_module
from hyprial.identity.impl.agents import (
    AgentRegistry,
)
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.configuration.identity import normalize_agent_recipient
from hyprial.daemon.impl.inbox import (
    InboxService,
    MemoryDeliveryTransport,
)

from .support import (
    FakeInbox,
    NODE,
    OWNER,
    URI,
    _close_test_applications as _close_test_applications,
    application,
    enable_routine_coordinator,
    enable_routines,
    message_for,
    register_interactive,
    start_headless,
)


# --------------------------------------------------------------------------
# A6 -- destroy is real deletion
# --------------------------------------------------------------------------


def test_destroy_removes_the_record_connectors_and_pending_messages(
    tmp_path: Path,
) -> None:
    inbox = FakeInbox((message_for(URI),))
    app = application(tmp_path, inbox=inbox)
    enable_routines(app, tmp_path)
    enable_routine_coordinator(app, tmp_path)
    start_headless(app, "pi", "foo")
    assert app.agents.exists("foo")

    result = app.handle("agent.destroy", {"name": "foo"})

    assert result["destroyed"] is True
    assert result["irreversible"] is True
    assert result["stopped"] == ["pi:foo"]
    assert result["destroyedMessages"] == 1
    # The row is gone -- no tombstone, nothing to revive.  A fresh registry
    # over the same database agrees, so it is really the storage saying so.
    assert app.agents.get("foo") is None
    assert (
        AgentRegistry(
            tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE
        ).get("foo")
        is None
    )
    assert app.agents.local_actors() == ()
    # The connector is gone with it, and so are its undelivered messages.
    assert app.desired_state.load().harnesses == ()
    assert app.handle("routine.list", {"actor": "user:op"})["routines"] == []
    assert inbox.pending_messages(URI) == ()
    # A destroyed agent is no longer a target.
    assert app.handle("targets", {"kind": "agent"})["targets"] == []


def test_destroy_drains_canonical_and_pre_fix_legacy_bare_messages(
    tmp_path: Path,
) -> None:
    """Verify the drain against a real InboxService, not a double.

    A fake inbox stores whatever key the test hands it, so it can only confirm
    the assumption it was built from. New sends to a registered agent always
    resolve to its four-segment URI, but an upgraded database can still hold a
    pre-fix bare recipient. Destroy must drain both without reintroducing bare
    registration or send semantics.
    """

    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        app = application(tmp_path)
        app._inbox = inbox  # type: ignore[assignment]
        app.handle("agent.create", {"name": "foo"})
        # Both accepted entry-point spellings now resolve to one stored key.
        assert app._resolve_agent_alias(  # type: ignore[attr-defined]
            normalize_agent_recipient(URI)
        ) == URI
        assert app._resolve_agent_alias(  # type: ignore[attr-defined]
            normalize_agent_recipient("foo")
        ) == URI
        # Arrival is what fills the inbox (the zenoh endpoint calls receive);
        # a local send goes to the outbox, which is a different table.
        inbox.receive(message_for(URI, "message-uri"))
        # Seed one legacy row directly: this is upgrade cleanup evidence, not
        # a spelling the post-fix send path can create.
        inbox.receive(message_for("foo", "message-bare"))
        assert len(inbox.pending_messages(URI)) == 1
        assert len(inbox.pending_messages("foo")) == 1

        result = app.handle("agent.destroy", {"name": "foo"})

        assert result["destroyedMessages"] == 2
        assert inbox.pending_messages(URI) == ()
        assert inbox.pending_messages("foo") == ()
    finally:
        inbox.close()


def test_destroy_of_an_unknown_agent_fails_loudly(tmp_path: Path) -> None:
    app = application(tmp_path)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("agent.destroy", {"name": "nobody"})

    assert failure.value.code == "AGENT_NOT_FOUND"


def test_destroyed_name_rebuilds_without_inheriting_old_home_credentials(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    start_headless(app, "pi", "foo")
    receipt = app._agent_session_domains._registry.home_receipt("foo")
    old = app.agents.require("foo")
    root = Path(receipt.path)
    (root / "secrets").mkdir(mode=0o700, exist_ok=True)
    sentinels = (
        root / "config" / "old-personality.json",
        root / "secrets" / "native" / "codex" / "auth.json",
        root / "secrets" / "tools" / "login.json",
        root / "state" / "old-session.json",
    )
    for sentinel in sentinels:
        sentinel.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        sentinel.write_text("NON_PRODUCTION_SENTINEL\n", encoding="utf-8")

    app.handle("agent.destroy", {"name": "foo"})
    assert not root.exists()

    recreated = app.handle("agent.create", {"name": "foo"})
    assert recreated["created"] is True
    rebuilt = app.agents.require("foo")
    assert rebuilt.entity_token != old.entity_token
    rebuilt_root = Path(
        app._agent_session_domains._registry.home_receipt("foo").path
    )
    assert rebuilt_root == root
    assert all(not sentinel.exists() for sentinel in sentinels)


def test_default_start_workspace_is_destroyed_and_rebuilt_with_git_skeleton(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    created = app.handle(
        "agent.create",
        {"name": "foo", "harness": "codex", "runtime": "headless"},
    )
    workspace = Path(created["agent"]["cwd"])
    root = workspace.parent
    assert workspace == Path(
        app._agent_session_domains._registry.home_receipt("foo").path
    ) / "workspace"
    assert workspace.is_dir()
    assert workspace.stat().st_mode & 0o777 == 0o700
    baseline = app.handle("agent.destroy.preview", {"name": "foo"})["workspace"]
    assert baseline["path"] == str(workspace)
    assert baseline["exists"] is True
    assert baseline["files"] > 0
    assert baseline["bytes"] > 0
    assert baseline["unreadableEntries"] == 0

    (workspace / "one.txt").write_bytes(b"one\n")
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "two.txt").write_bytes(b"two!!")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "must-survive.txt").write_text("outside", encoding="utf-8")
    link = workspace / "outside-link"
    link.symlink_to(outside, target_is_directory=True)
    expected_bytes = 4 + 5 + link.lstat().st_size

    preview = app.handle("agent.destroy.preview", {"name": "foo"})
    assert preview["workspace"] == {
        "path": str(workspace),
        "exists": True,
        "files": baseline["files"] + 3,
        "bytes": baseline["bytes"] + expected_bytes,
        "unreadableEntries": 0,
    }

    destroyed = app.handle("agent.destroy", {"name": "foo"})
    assert destroyed["workspace"] == {
        **preview["workspace"],
        "deleted": True,
    }
    assert not root.exists()
    assert (outside / "must-survive.txt").read_text(encoding="utf-8") == "outside"

    rebuilt = app.handle(
        "agent.create",
        {"name": "foo", "harness": "codex", "runtime": "headless"},
    )
    rebuilt_workspace = Path(rebuilt["agent"]["cwd"])
    assert rebuilt_workspace == workspace
    assert rebuilt_workspace.is_dir()
    # The initialized repository is the default workspace's empty skeleton.
    assert {item.name for item in rebuilt_workspace.iterdir()} == {".git"}


def test_destroy_preview_counts_only_visible_entries_in_unreadable_subtrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = application(tmp_path)
    created = app.handle(
        "agent.create",
        {"name": "foo", "harness": "codex", "runtime": "headless"},
    )
    workspace = Path(created["agent"]["cwd"])
    baseline = app.handle("agent.destroy.preview", {"name": "foo"})["workspace"]
    (workspace / "visible.txt").write_bytes(b"visible")
    unreadable = workspace / "unreadable"
    unreadable.mkdir()
    (unreadable / "hidden.txt").write_bytes(b"must-not-be-counted")
    scandir = agent_home_module.os.scandir

    def visible_only(path: object):  # type: ignore[no-untyped-def]
        if isinstance(path, (str, Path)) and Path(path) == unreadable:
            raise PermissionError("synthetic unreadable subtree")
        return scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(agent_home_module.os, "scandir", visible_only)

    preview = app.handle("agent.destroy.preview", {"name": "foo"})
    assert preview["workspace"]["files"] == baseline["files"] + 1
    assert preview["workspace"]["bytes"] == baseline["bytes"] + len(b"visible")
    assert preview["workspace"]["unreadableEntries"] == 1


def test_repeated_destroy_resumes_cleanup_after_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})
    registry = app._agent_session_domains._registry
    receipt = registry.home_receipt("foo")
    sentinel = Path(receipt.path) / "secrets" / "old-auth.json"
    sentinel.parent.mkdir(mode=0o700, exist_ok=True)
    sentinel.write_text("NON_PRODUCTION_SENTINEL\n", encoding="utf-8")
    cleanup = registry.cleanup_home

    def crash_after_revoke(_actor: str, *, expected_token: str) -> object:
        assert expected_token == receipt.resource_token
        raise RuntimeError("simulated crash after durable revoke")

    monkeypatch.setattr(registry, "cleanup_home", crash_after_revoke)
    with pytest.raises(RuntimeError, match="simulated crash"):
        registry.destroy("foo")
    assert sentinel.exists()

    # A real crash discards the in-memory actor projection.  Reopen the
    # production composition over the same durable database and retry the
    # public daemon command.
    monkeypatch.setattr(registry, "cleanup_home", cleanup)
    restarted = application(tmp_path)
    assert restarted.agents.get("foo") is None
    resumed = restarted.handle("agent.destroy", {"name": "foo"})
    assert resumed["cleanupResumed"] is True
    assert resumed["destroyed"] is False
    assert not Path(receipt.path).exists()
    assert restarted.handle("agent.create", {"name": "foo"})["created"] is True


# --------------------------------------------------------------------------
# A7 -- one liveness model, and it can say offline
# --------------------------------------------------------------------------


def test_a_registered_agent_that_is_not_running_reports_offline(
    tmp_path: Path,
) -> None:
    """The regression the hard-coded literal made impossible.

    `targets` used to build its rows from the list of *online* actors and then
    stamp every row "online", so no caller could ever see an offline agent.
    """

    app = application(tmp_path)
    app.handle("agent.create", {"name": "foo"})

    targets = app.handle("targets", {"kind": "agent"})["targets"]

    assert targets == [
        {
            "targetKind": "agent",
            "targetUri": URI,
            "actor": URI,
            "status": "offline",
            "deliverable": True,
            "hosted": False,
            "hostedBy": None,
        }
    ]


def test_an_interactive_agent_goes_offline_when_its_heartbeat_goes_stale(
    tmp_path: Path,
) -> None:
    """A live verdict, not merely "nothing is bound"."""

    app = application(tmp_path)
    clock = {"now": 0.0}
    app._clock = lambda: clock["now"]  # type: ignore[assignment]
    register_interactive(app, URI)

    assert app.handle("targets", {"kind": "agent"})["targets"][0]["status"] == "online"

    clock["now"] += 1000.0

    assert app.handle("targets", {"kind": "agent"})["targets"][0]["status"] == "offline"


def test_a_headless_agent_goes_offline_when_its_process_stops(
    tmp_path: Path,
) -> None:
    """The supervisor's `running` flag feeds the same one model."""

    app = application(tmp_path)
    start_headless(app, "pi", "foo")

    assert app.handle("targets", {"kind": "agent"})["targets"][0]["status"] == "online"

    app._harnesses.running[("pi", "foo")] = False  # type: ignore[union-attr]

    assert app.handle("targets", {"kind": "agent"})["targets"][0]["status"] == "offline"


def test_ps_and_targets_share_one_verdict(tmp_path: Path) -> None:
    """The three models are one: ps's agents view and targets must agree."""

    app = application(tmp_path)
    clock = {"now": 0.0}
    app._clock = lambda: clock["now"]  # type: ignore[assignment]
    register_interactive(app, URI)
    clock["now"] += 1000.0

    status = app.handle("ps", {})
    targets = app.handle("targets", {"kind": "agent"})["targets"]

    assert [item["actor"] for item in status["agents"]] == ["foo"]
    assert status["agents"][0]["status"] == "offline"
    assert status["interactiveSessions"][0]["online"] is False
    assert targets[0]["status"] == "offline"


def test_a_remote_peer_is_still_judged_by_presence(tmp_path: Path) -> None:
    """Convergence must not steal the verdict for somebody else's agent."""

    app = application(tmp_path, online=("agent:her:other-host:remote",))

    targets = app.handle("targets", {"kind": "agent"})["targets"]

    assert targets == [
        {
            "targetKind": "agent",
            "targetUri": "agent:her:other-host:remote",
            "actor": "agent:her:other-host:remote",
            "status": "online",
            "deliverable": True,
        }
    ]

def test_compensation_releases_the_binding_exactly(tmp_path: Path) -> None:
    app = application(tmp_path)
    start_headless(app, "pi", "foo")
    app._compensate_replace(URI, HarnessLaunchSpec("pi", "foo", True))
    assert app._agent_liveness.binding(URI) is None


def test_initial_unknown_carrier_state_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hyprial.daemon.impl.mcp.channel.ownership as ownership

    app = application(tmp_path)
    app.handle(
        "session.register",
        {
            "actor": URI,
            "sessionRef": "interactive-sid",
            "cwd": "/work",
            "command": ["pi"],
            "source": "pi-extension",
            "runtime": "pi_interactive",
            "processPid": 424242,
            "processIdentity": "carrier-birth",
            "channelConfirmed": True,
        },
    )
    class _Status:
        value = "unknown"

    old_status = ownership._owner_process_status
    ownership._owner_process_status = lambda *_a, **_k: _Status()  # type: ignore[assignment]
    monkeypatch.setattr(app, "_require_resumable_session", lambda *_args: None)
    try:
        with pytest.raises(DaemonRequestError) as raised:
            app.handle(
                "lifecycle.start",
                {
                    "provider": "pi",
                    "name": "foo",
                    "headless": True,
                    "cwd": "/work",
                    "sessionRef": "resume-sid",
                    "replaceInteractive": True,
                },
            )
    finally:
        ownership._owner_process_status = old_status
    assert raised.value.code == "INTERACTIVE_CARRIER_STOP_FAILED"
    binding = app._agent_liveness.binding(URI)
    assert binding is not None and binding.runtime == "interactive"


