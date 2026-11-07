"""Coexist semantics: configured forwarding and system discovery both dial.

The pre-coexist daemon answered "configured forwarding XOR host tailnet": a
hub that turned forwarding on silently stopped dialing its original tailnet
peers (infra-op isolated reading, 2026-09-15: with forwarding on, connect
held only the two loopback sidecar ports). These tests pin the replacement
contract: the union with forwarding first, either half failing alone, the
``HYPRIAL_FORWARDING_EXCLUSIVE`` switch that restores the old
negative-control behaviour, and the ``HYPRIAL_PEER_DISCOVERY=0`` shape.

Every daemon here pins ``HYPRIAL_PEER_DISCOVERY_COMMAND`` or ``=0``: a test
daemon must never fall through to an uncontrolled directory or the
production mesh -- which the coexist default would otherwise happily ask.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import sys
from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.bootstrap.discovery import ForwardingEndpoints
from hyprial.daemon.impl.forwarding.slots import slot_endpoints
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

FIXTURE = Path(__file__).resolve().parents[4] / 'tests/fixtures/network/fake_forwarding_sidecar.py'

#: The forwarding half dials the fixed slot set now, not per-peer loopback
#: ports.  Real-fixture tests use a 2-slot pool (``_slot_set``); the
#: injected-backend tests use this explicit pair.
SLOTS = ("unixsock-stream/s/slot-00", "unixsock-stream/s/slot-01")


def _slot_set(tmp_path: Path) -> tuple[str, ...]:
    return slot_endpoints(tmp_path / "home", 2)


def _up(tmp_path: Path) -> dict[str, object]:
    return {
        "v": 6, "requestId": "up-fixture",
        "op": "up",
        "keyFile": str(tmp_path / "device-key.json"),
        "membershipStore": str(tmp_path / 'membership.json'),
        "owner": "fixture", "deviceId": "device", "controlPort": 7448,
        "addressFile": str(tmp_path / "state/tailcat/address"),
        "inboundTarget": 'unix:' + str(tmp_path / 'zenoh.sock'),
        "peerPort": 7447,
    }


#: The org-directory view the daemon-side tests pin: two members, self excluded.
DIRECTORY = {
    "100.64.20.2": "tcPeerTwo",
    "100.64.20.3": "tcPeerThree",
}


def _command_backend(tmp_path: Path, *endpoints: str) -> str:
    """The escape-hatch directory: one endpoint per line, loopback only."""

    script = tmp_path / "directory.sh"
    script.write_text(
        "#!/bin/sh\n"
        + "".join(f"echo {endpoint}\n" for endpoint in endpoints)
    )
    script.chmod(0o755)
    return str(script)


class _Controller:
    """The pinned controller stub shape (see test_daemon_discovery.py)."""

    def __init__(self) -> None:
        self.mappings = {"100.64.20.2": 41002, "100.64.20.3": 41003}
        self.operations: list[tuple[str, str]] = []
        self.closed = False

    def status(self) -> tuple[tuple[str, ...], dict[str, str]]:
        return (), {}

    def map_peer(self, peer: str, address: str, slot: str) -> str:
        self.operations.append(("map", peer))
        raise AssertionError("stub is fully mapped; map_peer must not be called")

    def unmap_peer(self, peer: str) -> None:
        self.operations.append(("unmap", peer))
        self.mappings.pop(peer, None)

    def close(self) -> None:
        self.closed = True


def _env(
    monkeypatch,
    tmp_path: Path,
    *,
    forwarding_env: bool,
    exclusive: str | None = None,
    discovery: str | None = "1",
    command: str | None = None,
) -> None:
    # A small fixed pool so the forwarding half's slot set is short and
    # assertable; the forwarding half is the slot set (no per-peer ports).
    monkeypatch.setenv("HYPRIAL_ZENOH_SLOT_POOL", "2")
    if forwarding_env:
        monkeypatch.setenv(
            "HYPRIAL_FORWARDING_COMMAND", json.dumps([sys.executable, str(FIXTURE)])
        )
        monkeypatch.setenv("HYPRIAL_FORWARDING_UP", json.dumps(_up(tmp_path)))
        monkeypatch.setenv("FAKE_FORWARD_RECEIPT", str(tmp_path / "receipt.jsonl"))
    if exclusive is not None:
        monkeypatch.setenv("HYPRIAL_FORWARDING_EXCLUSIVE", exclusive)
    if discovery is not None:
        monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", discovery)
    if command is not None:
        monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY_COMMAND", command)


def _app(
    tmp_path: Path,
    monkeypatch,
    *,
    forwarding_env: bool,
    exclusive: str | None = None,
    discovery: str | None = "1",
    command: str | None = None,
) -> DaemonApplication:
    _env(
        monkeypatch,
        tmp_path,
        forwarding_env=forwarding_env,
        exclusive=exclusive,
        discovery=discovery,
        command=command,
    )
    return DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon-state/daemon.sock",
        node_id="coexist-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
    **_deep_module_daemon_dependencies())


def _pin_directory(app: DaemonApplication, monkeypatch) -> None:
    """The org-directory view the supervisor's backend reconciles against."""

    monkeypatch.setattr(app, "_tailcat_directory_peers", lambda: DIRECTORY)


def _events(app: DaemonApplication, monkeypatch) -> list[tuple[str, dict[str, object]]]:
    captured: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **fields: captured.append((event, fields)),
    )
    return captured


def test_union_is_forwarding_first_discovery_after_no_duplicates(
    tmp_path: Path, monkeypatch
) -> None:
    """The coexist contract: both halves dial, forwarding pinned first.

    The overlapping endpoint (a slot the directory also names) appears
    once, in the forwarding position -- an operator who named a peer via
    forwarding keeps it tried first instead of racing the directory.
    """

    slots = _slot_set(tmp_path)
    directory = _command_backend(tmp_path, slots[1], "tcp/127.0.0.1:7801")
    app = _app(
        tmp_path,
        monkeypatch,
        forwarding_env=True,
        discovery="1",
        command=directory,
    )
    _pin_directory(app, monkeypatch)
    try:
        assert app._discover_peer_endpoints() == slots + ("tcp/127.0.0.1:7801",)
    finally:
        app._close()


def test_forwarding_failure_leaves_discovery_endpoints_and_logs_error(
    tmp_path: Path, monkeypatch
) -> None:
    """A status fault logs but does not empty the forwarding half: the slot
    set outlives any sidecar health blip (design §5), and discovery is
    appended regardless."""

    class _Broken(_Controller):
        def status(self):
            raise ConnectionError("sidecar exited with status 17")

    directory = _command_backend(tmp_path, "tcp/127.0.0.1:7801", "tcp/127.0.0.1:7802")
    _env(
        monkeypatch, tmp_path, forwarding_env=False, discovery="1", command=directory
    )
    app = DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon-state/daemon.sock",
        node_id="coexist-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
        forwarding_discovery=ForwardingEndpoints(
            _Broken(),
            slots=lambda: SLOTS,
            on_failure=lambda detail: app._log(
                "error",
                "zenoh",
                "zenoh.forwarding.failed",
                detail=detail[:500],
            ),
        ),
    **_deep_module_daemon_dependencies())
    events = _events(app, monkeypatch)
    try:
        assert app._discover_peer_endpoints() == SLOTS + (
            "tcp/127.0.0.1:7801",
            "tcp/127.0.0.1:7802",
        )
    finally:
        app._close()
    zenoh_events = [
        event for event, _fields in events if event.startswith("zenoh.")
    ]
    assert zenoh_events == ["zenoh.forwarding.failed"]


def test_discovery_failure_leaves_forwarding_endpoints_and_logs_warn(
    tmp_path: Path, monkeypatch
) -> None:
    """The mirror of the above: a broken directory is a warn, not an outage."""

    import hyprial.daemon.impl.application.netendpoints.endpoints as application_module

    class _BrokenDirectory:
        def __init__(self, _command: tuple[str, ...]) -> None:
            pass

        def list_reachable_endpoints(self) -> tuple[str, ...]:
            raise RuntimeError("directory unreachable")

    monkeypatch.setattr(
        application_module, "CommandEndpoints", _BrokenDirectory, raising=True
    )
    _env(
        monkeypatch,
        tmp_path,
        forwarding_env=False,
        discovery="1",
        command=_command_backend(tmp_path, "tcp/127.0.0.1:7801"),
    )
    controller = _Controller()
    app = DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon-state/daemon.sock",
        node_id="coexist-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
        forwarding_discovery=ForwardingEndpoints(controller, slots=lambda: SLOTS),
    **_deep_module_daemon_dependencies())
    events = _events(app, monkeypatch)
    try:
        assert app._discover_peer_endpoints() == SLOTS
    finally:
        app._close()
    zenoh_events = [
        event for event, _fields in events if event.startswith("zenoh.")
    ]
    assert zenoh_events == ["zenoh.discovery.failed"]
    assert controller.closed is True


def test_exclusive_switch_restores_forwarding_only_no_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    """`HYPRIAL_FORWARDING_EXCLUSIVE=1` is the old behaviour, explicitly."""

    directory = _command_backend(tmp_path, "tcp/127.0.0.1:7801", "tcp/127.0.0.1:7802")
    app = _app(
        tmp_path,
        monkeypatch,
        forwarding_env=True,
        exclusive="1",
        discovery="1",
        command=directory,
    )
    _pin_directory(app, monkeypatch)
    try:
        assert app._discover_peer_endpoints() == _slot_set(tmp_path)
    finally:
        app._close()


def test_exclusive_switch_with_dead_sidecar_still_dials_the_fixed_slot_set(
    tmp_path: Path, monkeypatch
) -> None:
    """The slots negative control: exclusive means no directory fallback --
    but the fixed slot set is derivable from the home, so a dead sidecar
    still dials it (design §5), and the directory is never consulted."""

    monkeypatch.setenv("HYPRIAL_ZENOH_SLOT_POOL", "2")
    monkeypatch.setenv("HYPRIAL_FORWARDING_COMMAND", '["/missing/candidate","fwd"]')
    monkeypatch.setenv("HYPRIAL_FORWARDING_UP", json.dumps(_up(tmp_path)))
    monkeypatch.setenv("HYPRIAL_FORWARDING_EXCLUSIVE", "1")
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "1")
    monkeypatch.setenv(
        "HYPRIAL_PEER_DISCOVERY_COMMAND",
        _command_backend(tmp_path, "tcp/127.0.0.1:7801"),
    )
    app = DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon-state/daemon.sock",
        node_id="coexist-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
    **_deep_module_daemon_dependencies())
    events = _events(app, monkeypatch)
    try:
        assert app._discover_peer_endpoints() == _slot_set(tmp_path)
    finally:
        app._close()
    names = [event for event, _fields in events]
    assert names[0] == "zenoh.forwarding.start_failed"
    # The directory answered 7801, and exclusive still returned only the
    # slot set: proving the switch cut the directory, not a dead directory.
    assert "zenoh.discovery.failed" not in names


def test_peer_discovery_zero_with_forwarding_leaves_only_forwarding(
    tmp_path: Path, monkeypatch
) -> None:
    """`HYPRIAL_PEER_DISCOVERY=0` still cuts the system half (infra-op shape)."""

    app = _app(tmp_path, monkeypatch, forwarding_env=True, discovery="0")
    _pin_directory(app, monkeypatch)
    try:
        assert app._discover_peer_endpoints() == _slot_set(tmp_path)
    finally:
        app._close()


def test_exclusive_switch_without_forwarding_configured_is_a_no_op(
    tmp_path: Path, monkeypatch
) -> None:
    """The switch only shapes a node that configured forwarding.

    Set without forwarding, it neither cuts the directory nor errors: the
    node is an ordinary discovery node, pinned here so the silent no-op is a
    documented fact rather than an untested accident.
    """

    directory = _command_backend(tmp_path, "tcp/127.0.0.1:7801", "tcp/127.0.0.1:7802")
    app = _app(
        tmp_path,
        monkeypatch,
        forwarding_env=False,
        exclusive="1",
        discovery="1",
        command=directory,
    )
    try:
        assert app._discover_peer_endpoints() == (
            "tcp/127.0.0.1:7801",
            "tcp/127.0.0.1:7802",
        )
    finally:
        app._close()
