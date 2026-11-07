from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.daemon.startup.test_startup_event_reconciliation import _native_application_source


APPLICATION_ROOT = Path(__file__).resolve().parents[3] / 'src/hyprial'


def _native_route_tree() -> ast.Module:
    """Inventory native methods, with the exact split startup phases in call order."""
    if not Path(DaemonApplication._start_runtime.__code__.co_filename).is_relative_to(APPLICATION_ROOT):
        raise RuntimeError("Native route source resolved outside the product tree")
    tree = ast.parse(_native_application_source())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    methods = {node.name: node for node in owner.body if isinstance(node, ast.FunctionDef)}
    entry = methods["_start_runtime"]
    phases = [node.func.attr for node in ast.walk(entry) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
              and node.func.value.id == "self"]
    expected = ["_startup_prepare_state", "_startup_open_transport", "_startup_compose_delivery_plane",
                "_startup_compose_runtime_bridge", "_startup_adopt_domain_services", "_startup_finalize_runtime"]
    if phases != expected:
        raise AssertionError(f"Unexpected native startup phases: {phases}")
    entry.body = [statement for name in phases for statement in methods[name].body]
    owner.body = [node for node in owner.body if not isinstance(node, ast.FunctionDef) or node.name not in phases]
    return tree


def test_application_has_no_parallel_persona_or_interactive_route_maps() -> None:
    tree = _native_route_tree()
    forbidden = {"_persona_routes", "_interactive_routes"}
    found = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    }
    assert found == set()


def test_agent_and_session_helpers_only_call_typed_route_client() -> None:
    tree = _native_route_tree()
    protected = {
        "_declare_persona_route",
        "_drop_persona_route",
        "_ensure_interactive_route",
        "_ensure_interactive_route_effect",
        "_close_interactive_route",
        "_close_interactive_route_effect",
        "_restore_persona_routes",
        "_restore_interactive_routes",
        "_expire_stale_channel_routes",
    }
    forbidden_calls = {"declare_liveliness", "ZenohInboxEndpoint", "subscribe"}
    bypasses: list[str] = []
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in protected
    ):
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            target = call.func
            name = (
                target.attr
                if isinstance(target, ast.Attribute)
                else target.id
                if isinstance(target, ast.Name)
                else ""
            )
            if name in forbidden_calls:
                bypasses.append(f"{function.name}:{name}")
    assert bypasses == []


def test_production_restore_starts_session_route_owner_before_route_replay() -> None:
    tree = _native_route_tree()
    start_runtime = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_start_runtime"
    )
    calls = [
        node.func.attr
        for node in ast.walk(start_runtime)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {
            "_ensure_session_route_coordinator",
            "_restore_interactive_routes",
        }
    ]
    assert calls.count("_ensure_session_route_coordinator") == 1
    assert calls.index("_ensure_session_route_coordinator") < calls.index(
        "_restore_interactive_routes"
    )


def test_only_composition_factory_constructs_actor_route_effects() -> None:
    tree = _native_route_tree()
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    found: list[tuple[str, str]] = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        target = call.func
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else target.id
            if isinstance(target, ast.Name)
            else ""
        )
        if name not in {"declare_liveliness", "ZenohInboxEndpoint"}:
            continue
        owner: ast.AST | None = call
        function = "<module>"
        while owner in parents:
            owner = parents[owner]
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = owner.name
                break
        found.append((function, name))

    assert sorted(found) == [
        ("_start_runtime", "ZenohInboxEndpoint"),
        ("_start_runtime", "declare_liveliness"),
        ("register_route", "ZenohInboxEndpoint"),
        ("register_route", "declare_liveliness"),
    ]


def test_session_owner_lease_is_acquired_released_and_stale_fenced(
    tmp_path: Path,
) -> None:
    class Routes:
        def __init__(self) -> None:
            self.operations: list[tuple[str, str, str]] = []
            self._owners: dict[str, set[str]] = {}

        def ensure(self, spec, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            self.operations.append(("ensure", spec.route_id, owner_lease))
            owners = self._owners.setdefault(spec.route_id, set())
            changed = owner_lease not in owners
            owners.add(owner_lease)
            return SimpleNamespace(changed=changed)

        def drop(self, route_id, *, owner_lease=None):  # type: ignore[no-untyped-def]
            assert owner_lease is not None
            self.operations.append(("drop", route_id, owner_lease))
            owners = self._owners.setdefault(route_id, set())
            changed = owner_lease in owners
            owners.discard(owner_lease)
            return SimpleNamespace(changed=changed)

        def owners(self, route_id: str) -> tuple[str, ...]:
            return tuple(sorted(self._owners.get(route_id, set())))

    class Harnesses:
        def status(self) -> tuple[()]:
            return ()

    class Presence:
        def actor_online(self, _actor: str) -> bool:
            return False

        def online_actors(self) -> tuple[str, ...]:
            return ()

        def online_mailboxes(self) -> tuple[str, ...]:
            return ()

    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="node",
        owner="owner",
        **_deep_module_daemon_dependencies(),
    )
    inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    routes = Routes()
    app._inbox = inbox
    app._routes = routes  # type: ignore[assignment]
    app._harnesses = Harnesses()  # type: ignore[assignment]
    app._presence = Presence()  # type: ignore[assignment]
    actor = "agent:owner:node:alice"
    base = {
        "actor": "alice",
        "cwd": str(tmp_path),
        "command": ["claude"],
        "source": "claude-channel",
        "channelConfirmed": True,
    }
    try:
        app.handle("session.register", {**base, "sessionRef": "old"})
        app.handle("session.register", {**base, "sessionRef": "new"})

        assert ("ensure", actor, "session:old") in routes.operations
        assert ("ensure", actor, "session:new") in routes.operations
        assert ("drop", actor, "session:old") in routes.operations
        assert routes.owners(actor) == (f"persona:{actor}", "session:new")

        before = tuple(routes.operations)
        with pytest.raises(DaemonRequestError):
            app.handle(
                "session.heartbeat",
                {"actor": "alice", "sessionRef": "old"},
            )
        assert tuple(routes.operations) == before

        app.handle(
            "session.unregister", {"actor": "alice", "sessionRef": "new"}
        )
        assert routes.owners(actor) == (f"persona:{actor}",)
    finally:
        assert app._agent_session_domains.close()
        inbox.close()
