"""Single-writer gates for daemon-managed registry mutations."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import ast
import threading
import time
from pathlib import Path

import pytest
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

from hyprial.daemon.impl.configuration.ownership import DaemonOwnershipBusy, DaemonStateOwnershipFence
from hyprial.daemon.impl.operations.management import OfflineManagementLease


ROOT = Path(__file__).resolve().parents[3]


def test_daemon_ownership_fence_is_exclusive_and_reusable(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first = DaemonStateOwnershipFence.acquire(state_dir)
    try:
        with pytest.raises(DaemonOwnershipBusy):
            DaemonStateOwnershipFence.acquire(state_dir)
    finally:
        first.close()

    replacement = DaemonStateOwnershipFence.acquire(state_dir)
    replacement.close()


def test_offline_management_cannot_open_registries_while_daemon_owns_state(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    with DaemonStateOwnershipFence.acquire(state_dir):
        with pytest.raises(DaemonOwnershipBusy):
            with OfflineManagementLease(
                state_dir,
                owner="alice",
                machine="node-1",
                hyprial_home=tmp_path / "home",
            ):
                raise AssertionError("busy offline authority must not enter")

    assert not (state_dir / "agents.sqlite3").exists()
    assert not (state_dir / "desired-state.json.v1").exists()


def test_cli_setup_and_adapter_remove_have_no_raw_registry_constructors() -> None:
    """Offline mutation must stay behind the fenced management authority."""

    forbidden = {"AgentRegistry", "DesiredStateStore", "AgentSessionDomains"}
    for relative in (
        "src/hyprial/daemon/impl/squire/setup.py",
        "src/hyprial/daemon/impl/bootstrap/adapter_registration.py",
    ):
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constructed = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert constructed.isdisjoint(forbidden), (relative, constructed & forbidden)


def test_identity_free_sqlite_pin_mutation_bypass_is_retired() -> None:
    registry_sources = sorted((ROOT / "src/hyprial/identity/impl/agents/registry").glob("*.py"))
    if not registry_sources:
        raise RuntimeError("No native AgentRegistry implementation sources")
    registry = ast.parse("\n".join(path.read_text(encoding="utf-8") for path in registry_sources))
    defined = {
        node.name
        for node in registry.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert defined.isdisjoint(
        {"read_adapter_pin", "remove_adapter_pin", "restore_adapter_pin"}
    )


def test_concurrent_first_ipc_constructs_one_management_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hyprial.daemon.impl.application as application_module

    app = application_module.DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="node-1",
        owner="alice",
        **_deep_module_daemon_dependencies(),
    )
    app._harnesses = object()  # type: ignore[assignment]
    barrier = threading.Barrier(3)
    constructed: list[object] = []
    returned: list[object] = []

    class _Marker:
        def close(self, _timeout: float = 5.0) -> bool:
            return True

    def construct(*_args: object, **_kwargs: object) -> object:
        marker = _Marker()
        constructed.append(marker)
        time.sleep(0.05)
        return marker

    monkeypatch.setitem(app._registry_management_handler.__func__.__globals__, "RegistryManagementHandler", construct)

    def resolve() -> None:
        barrier.wait()
        returned.append(app._registry_management_handler())

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(2.0)
        assert not thread.is_alive()

    assert len(constructed) == 1
    assert len(returned) == 2
    assert returned[0] is returned[1] is constructed[0]
    app._agent_session_domains.close()
    app._agent_domains_finalizer.detach()
