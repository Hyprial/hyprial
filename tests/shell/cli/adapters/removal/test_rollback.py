"""Rollback: a failed ``hyprial adapter remove`` must be a byte-exact no-op.

Split out of ``test_cli_adapter_remove.py`` (I6-4) to keep that file under the
structural line limit; the fixtures stay in that module and are imported here.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from tests.shell.cli.adapters.test_cli_adapter_remove import (
    _channels_json,
    _desired_state_json,
    _seed_adapter,
    _seed_desired_state,
)


def test_rollback_when_desired_state_save_fails(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.daemon.impl import desired_state

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    before_channels = (home / "channels.json").read_bytes()
    before_versioned = _desired_state_json(home)

    def failing_save(self: Any, state: Any) -> None:
        raise OSError("simulated desired-state write failure")

    monkeypatch.setattr(desired_state.DesiredStateStore, "save", failing_save)

    with pytest.raises(OSError):
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    assert (home / "channels.json").read_bytes() == before_channels
    assert _desired_state_json(home) == before_versioned
    assert (home / "secrets" / "lark-feishu.json").exists()


def test_rollback_preserves_permission_error_fields_from_desired_state(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    import errno
    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.daemon.impl import desired_state

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    before_channels = (home / "channels.json").read_bytes()
    before_versioned = _desired_state_json(home)
    blocked_path = home / "desired-state.json.v1"

    def failing_save(self: Any, state: Any) -> None:
        raise PermissionError(errno.EACCES, "synthetic permission denial", str(blocked_path))

    monkeypatch.setattr(desired_state.DesiredStateStore, "save", failing_save)
    with pytest.raises(PermissionError) as caught:
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )
    assert caught.value.errno == errno.EACCES
    assert caught.value.filename == str(blocked_path)
    assert caught.value.strerror == "synthetic permission denial"
    assert (home / "channels.json").read_bytes() == before_channels
    assert _desired_state_json(home) == before_versioned
    assert (home / "secrets" / "lark-feishu.json").exists()


def test_rollback_when_secret_delete_fails(monkeypatch: Any, tmp_path: Path) -> None:
    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.identity.impl.agents import AgentRegistry
    from hyprial.kernel import resolve_node_id
    from hyprial.daemon.impl.configuration.identity import resolve_node_owner

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    owner = resolve_node_owner()
    monkeypatch.setenv("HYPRIAL_NODE_ID", "adapter-rollback-fixture")
    machine = resolve_node_id()
    registry = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    registry.create("worker")
    registry.close()
    before_channels = (home / "channels.json").read_bytes()
    before_versioned = _desired_state_json(home)
    secret_path = home / "secrets" / "lark-feishu.json"
    before_secret = secret_path.read_bytes()

    real_unlink = Path.unlink

    def failing_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == secret_path:
            raise OSError("simulated secret delete failure")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(OSError):
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    assert (home / "channels.json").read_bytes() == before_channels
    assert _desired_state_json(home) == before_versioned
    assert secret_path.read_bytes() == before_secret
    assert secret_path.stat().st_mode & 0o777 == 0o600
    reloaded = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    assert reloaded.require("worker") is not None
    reloaded.close()


def test_rollback_after_database_unpin_mutates_then_raises(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A late actor-port failure must restore all four removal surfaces.

    This is deliberately stronger than making ``unpin_if`` fail before doing
    anything: the pin row is removed first and only then does the port raise.
    The compensation path therefore has to recreate the exact binding as well
    as restore desired state and the two files.
    """

    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.identity.impl.agents import AgentRegistry
    from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
    from hyprial.daemon.impl.harnesses.runtime.ports import HarnessAdapterRegistrationProjection
    from hyprial.kernel import resolve_node_id
    from hyprial.daemon.impl.configuration.identity import resolve_node_owner

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    owner = resolve_node_owner()
    monkeypatch.setenv("HYPRIAL_NODE_ID", "adapter-rollback-fixture")
    machine = resolve_node_id()
    registry = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    registry.create("worker")
    registry.close()

    before_channels = (home / "channels.json").read_bytes()
    before_secret = (home / "secrets" / "lark-feishu.json").read_bytes()
    before_desired = _desired_state_json(home)
    real_remove = ManagedHarnessRuntime.remove_adapter_registration

    def mutate_then_fail(
        self: ManagedHarnessRuntime,
        snapshot: HarnessAdapterRegistrationProjection,
    ) -> bool:
        real_remove(self, snapshot)
        raise OSError("simulated post-removal port failure")

    monkeypatch.setattr(
        ManagedHarnessRuntime, "remove_adapter_registration", mutate_then_fail
    )

    with pytest.raises(OSError, match="post-removal"):
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    assert (home / "channels.json").read_bytes() == before_channels
    assert (home / "secrets" / "lark-feishu.json").read_bytes() == before_secret
    assert _desired_state_json(home) == before_desired
    reloaded = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    assert reloaded.require("worker") is not None
    reloaded.close()


def test_rollback_after_desired_removal_mutates_then_raises(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A lost actor reply after durable desired-state removal is compensated."""

    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.daemon.impl.harnesses.runtime.ports import HarnessAdapterRegistrationProjection
    from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    before_channels = (home / "channels.json").read_bytes()
    before_secret = (home / "secrets" / "lark-feishu.json").read_bytes()
    before_desired = _desired_state_json(home)
    real_remove = ManagedHarnessRuntime.remove_adapter_registration

    def mutate_then_fail(
        self: ManagedHarnessRuntime,
        snapshot: HarnessAdapterRegistrationProjection,
    ) -> bool:
        real_remove(self, snapshot)
        raise TimeoutError("simulated lost harness actor reply")

    monkeypatch.setattr(
        ManagedHarnessRuntime, "remove_adapter_registration", mutate_then_fail
    )

    with pytest.raises(TimeoutError, match="lost harness actor reply"):
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    assert (home / "channels.json").read_bytes() == before_channels
    assert (home / "secrets" / "lark-feishu.json").read_bytes() == before_secret
    assert _desired_state_json(home) == before_desired


def test_channels_write_failure_never_mutates_any_registry(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The first file write is the transaction admission point."""

    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.identity.impl.agents import AgentRegistry
    from hyprial.daemon.impl.configuration.identity import resolve_node_owner

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    owner = resolve_node_owner()
    from hyprial.kernel import resolve_node_id

    machine = resolve_node_id()
    registry = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    registry.create("worker")
    registry.close()

    before_channels = (home / "channels.json").read_bytes()
    before_secret = (home / "secrets" / "lark-feishu.json").read_bytes()
    before_desired = _desired_state_json(home)

    def fail_channels_write(_path: Path, _payload: object) -> None:
        raise OSError("simulated channels write failure")

    monkeypatch.setattr(adapter_registration, "atomic_json_write", fail_channels_write)

    with pytest.raises(OSError, match="channels write"):
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    assert (home / "channels.json").read_bytes() == before_channels
    assert (home / "secrets" / "lark-feishu.json").read_bytes() == before_secret
    assert _desired_state_json(home) == before_desired
    reloaded = AgentRegistry(home / "agents.sqlite3", owner=owner, machine=machine)
    assert reloaded.require("worker") is not None
    reloaded.close()


def test_concurrent_force_add_waits_for_remove_and_keeps_replacement(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.kernel import PersistentConfigStore

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    at_unlink = threading.Event()
    release_unlink = threading.Event()
    remove_result: list[dict[str, object]] = []
    add_result: list[dict[str, object]] = []
    errors: list[BaseException] = []
    real_unlink = adapter_registration._unlink_if_current

    def paused_unlink(path: Path, expected: bytes | None) -> None:
        at_unlink.set()
        assert release_unlink.wait(2.0)
        real_unlink(path, expected)

    monkeypatch.setattr(adapter_registration, "_unlink_if_current", paused_unlink)

    def remove() -> None:
        try:
            remove_result.append(
                adapter_registration.remove_lark_gateway(
                    hyprial_home=home, state_dir=home, name="feishu"
                )
            )
        except BaseException as error:
            errors.append(error)

    def replace() -> None:
        try:
            add_result.append(
                adapter_registration.add_lark_gateway(
                    hyprial_home=home,
                    name="feishu",
                    app_id="replacement-app",
                    app_secret="replacement-secret",
                    routes=[
                        adapter_registration.RouteInput(
                            name="dm", native_id="oc_replacement"
                        )
                    ],
                    force=True,
                )
            )
        except BaseException as error:
            errors.append(error)

    removing = threading.Thread(target=remove)
    replacing = threading.Thread(target=replace)
    removing.start()
    assert at_unlink.wait(2.0)
    replacing.start()
    replacing.join(0.1)
    assert replacing.is_alive(), "force-add must wait for remove's config lease"
    release_unlink.set()
    removing.join(3.0)
    replacing.join(3.0)

    assert not errors
    assert len(remove_result) == len(add_result) == 1
    gateway = _channels_json(home)["gateways"][0]
    assert gateway["appId"] == "replacement-app"
    assert (home / "secrets" / "lark-feishu.json").read_text(encoding="utf-8").find(
        "replacement-secret"
    ) >= 0
    loaded = PersistentConfigStore(home, home).load()
    assert loaded.channels.gateways[0].app_id == "replacement-app"


def test_rollback_cas_preserves_uncoordinated_replacement(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import pytest

    import hyprial.daemon.impl.bootstrap.adapter_registration as adapter_registration
    from hyprial.kernel import atomic_json_write

    home = tmp_path / "home"
    _seed_adapter(home)
    _seed_desired_state(home)
    secret_path = home / "secrets" / "lark-feishu.json"
    replacement_channels = {
        "version": 1,
        "gateways": [
            {
                "provider": "lark",
                "name": "feishu",
                "appId": "replacement-app",
                "credentialRef": "lark-feishu",
                "routes": [{"name": "dm", "type": "direct", "nativeId": "oc_new"}],
                "defaultRoute": "dm",
            }
        ],
    }

    def replace_then_fail(_path: Path, _expected: bytes | None) -> None:
        atomic_json_write(home / "channels.json", replacement_channels)
        atomic_json_write(secret_path, {"appSecret": "replacement-secret"})
        raise OSError("simulated failure after uncoordinated replacement")

    monkeypatch.setattr(adapter_registration, "_unlink_if_current", replace_then_fail)

    with pytest.raises(adapter_registration.AdapterConfigConflictError) as caught:
        adapter_registration.remove_lark_gateway(
            hyprial_home=home, state_dir=home, name="feishu"
        )

    assert isinstance(caught.value.__cause__, BaseExceptionGroup)
    assert any(
        isinstance(error, adapter_registration.AdapterConfigConflictError)
        for error in caught.value.__cause__.exceptions
    )
    assert _channels_json(home) == replacement_channels
    assert "replacement-secret" in secret_path.read_text(encoding="utf-8")
