from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from hyprial.daemon.impl.forwarding import (
    ForwardingSidecarController,
    ForwardingSidecarError,
)
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


FIXTURE = Path(__file__).resolve().parents[4] / 'tests/fixtures/network/fake_forwarding_sidecar.py'

#: The directory view the daemon-side tests pin: two org members, self excluded.
DIRECTORY = {
    "100.64.20.2": "tcPeerTwo",
    "100.64.20.3": "tcPeerThree",
}


def _slots(tmp_path: Path) -> list[str]:
    return [str(tmp_path / "slots/slot-00"), str(tmp_path / "slots/slot-01")]


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
        # The fixed slot pool is the only mapping shape (no legacy
        # per-peer loopback ports); every up carries its slots.
        "slots": _slots(tmp_path),
    }


def test_controller_owns_v5_process_and_exact_map_lifecycle(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.jsonl"
    up = _up(tmp_path)
    controller = ForwardingSidecarController(
        (sys.executable, str(FIXTURE)),
        up,
        environ={"FAKE_FORWARD_RECEIPT": str(receipt)},
    )
    slot = _slots(tmp_path)[0]
    assert controller.status() == ((), {})
    assert controller.map_peer("100.64.20.2", "tcPeerTwo", slot) == slot
    assert controller.status() == (("100.64.20.2",), {"100.64.20.2": slot})
    assert not hasattr(controller, 'allow')
    controller.unmap_peer("100.64.20.2")
    controller.close()

    commands = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert commands[0] == up
    assert [item["op"] for item in commands] == [
        "up",
        "status",
        "map-peer",
        "status",
        "unmap-peer",
        "down",
    ]


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("map_peer", ("peer-a", "tailcat://missing-tc-prefix", "/tmp/s/slot-00")),
    ],
)
def test_invalid_forwarding_values_are_rejected_locally_without_buffering(
    tmp_path: Path, operation: str, arguments: tuple[object, ...]
) -> None:
    receipt = tmp_path / "receipt.jsonl"
    controller = ForwardingSidecarController(
        (sys.executable, str(FIXTURE)),
        _up(tmp_path),
        environ={"FAKE_FORWARD_RECEIPT": str(receipt)},
        timeout=10.0,
    )
    try:
        started = time.monotonic()
        with pytest.raises(ForwardingSidecarError, match="BAD_REQUEST"):
            getattr(controller, operation)(*arguments)
        assert time.monotonic() - started < 1.0
        assert list(controller._response_buffer) == []  # noqa: SLF001
        commands = [json.loads(line) for line in receipt.read_text().splitlines()]
        assert [item["op"] for item in commands] == ["up"]
    finally:
        controller.close()


def test_derive_client_key_uses_v5_public_only_operation(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.jsonl"
    controller = ForwardingSidecarController(
        (sys.executable, str(FIXTURE)),
        _up(tmp_path),
        environ={"FAKE_FORWARD_RECEIPT": str(receipt)},
    )
    server_public = "nodekey:" + "a" * 64
    try:
        derived = controller.derive_client_key(server_public)
    finally:
        controller.close()
    assert derived.startswith("nodekey:") and derived != server_public
    commands = [json.loads(line) for line in receipt.read_text().splitlines()]
    derive = commands[1]
    assert derive["v"] == 6
    assert derive["op"] == "derive-client-key"
    assert derive["serverPublic"] == server_public
    assert isinstance(derive.get("requestId"), str) and derive["requestId"]
    assert "private" not in json.dumps(derive).lower()


@pytest.mark.parametrize("reply_id", ["missing", "wrong"])
def test_derive_client_key_rejects_uncorrelated_v5_reply(
    tmp_path: Path, reply_id: str
) -> None:
    receipt = tmp_path / "receipt.jsonl"
    controller = ForwardingSidecarController(
        (sys.executable, str(FIXTURE)),
        _up(tmp_path),
        environ={
            "FAKE_FORWARD_RECEIPT": str(receipt),
            "FAKE_FORWARD_DERIVE_REQUEST_ID": reply_id,
        },
        timeout=0.1,
    )
    try:
        with pytest.raises(ForwardingSidecarError, match="response timed out"):
            controller.derive_client_key("nodekey:" + "b" * 64)
    finally:
        controller.close()


def test_up_requires_the_v5_device_key_shape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="protocol v6"):
        ForwardingSidecarController(
            (sys.executable, str(FIXTURE)),
            {"v": 2, "op": "up", "resume": True},
            environ={"FAKE_FORWARD_RECEIPT": str(tmp_path / "r.jsonl")},
        )
    up = _up(tmp_path)
    del up["keyFile"]
    with pytest.raises(ValueError, match="keyFile"):
        ForwardingSidecarController(
            (sys.executable, str(FIXTURE)),
            up,
            environ={"FAKE_FORWARD_RECEIPT": str(tmp_path / "r.jsonl")},
        )


def test_reader_protocol_mismatch_is_sticky_and_names_both_versions(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.jsonl"
    controller = ForwardingSidecarController(
        (sys.executable, str(FIXTURE)),
        _up(tmp_path),
        environ={
            "FAKE_FORWARD_RECEIPT": str(receipt),
            "FAKE_FORWARD_STATUS_NON_V5_ONCE": "1",
        },
        timeout=0.3,
    )
    try:
        for _attempt in range(2):
            started = time.monotonic()
            with pytest.raises(
                ForwardingSidecarError, match="expected v6, saw v4"
            ):
                controller.status()
            assert time.monotonic() - started < 0.2
    finally:
        controller.close()


def test_status_treats_a_missing_peers_key_as_no_peers_yet(tmp_path: Path) -> None:
    """A sidecar that omits the key answered "no mapped peer yet"."""

    receipt = tmp_path / "receipt.jsonl"
    controller = ForwardingSidecarController(
        (sys.executable, str(FIXTURE)),
        _up(tmp_path),
        environ={
            "FAKE_FORWARD_RECEIPT": str(receipt),
            "FAKE_FORWARD_STATUS_OMITS_PEERS": "1",
        },
    )
    try:
        assert controller.status() == ((), {})
    finally:
        controller.close()


def test_sidecar_stderr_is_retained_and_attached_to_a_status_fault(
    tmp_path: Path,
) -> None:
    """The sidecar's stderr is the only place a failure explains itself."""

    receipt = tmp_path / "receipt.jsonl"
    controller = ForwardingSidecarController(
        (sys.executable, str(FIXTURE)),
        _up(tmp_path),
        environ={
            "FAKE_FORWARD_RECEIPT": str(receipt),
            "FAKE_FORWARD_STATUS_PEERS_BAD": "1",
            "FAKE_FORWARD_STDERR": "sidecar: cannot read node status",
        },
    )
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not controller.stderr_tail:
            time.sleep(0.01)
        assert controller.stderr_tail == ("sidecar: cannot read node status",)
        with pytest.raises(ForwardingSidecarError) as raised:
            controller.status()
        assert "invalid peers" in str(raised.value)
        assert "sidecar: cannot read node status" in str(raised.value)
    finally:
        controller.close()


def test_controller_snapshots_proxy_environment_but_strips_ts(tmp_path: Path, monkeypatch) -> None:
    import subprocess
    original_popen = subprocess.Popen
    spawned = []
    def recording_popen(*args, **kwargs):
        spawned.append(dict(kwargs['env']))
        return original_popen(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', recording_popen)
    receipt = tmp_path / "receipt.jsonl"
    environment = {
        "FAKE_FORWARD_RECEIPT": str(receipt),
        "TS_AUTHKEY": "must-not-reach-child",
        "HTTPS_PROXY": "http://fake-user:fake-proxy-secret@127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:10",
        "HTTP_PROXY": "http://127.0.0.1:11",
        "http_proxy": "http://127.0.0.1:12",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "localhost",
        "ALL_PROXY": "socks5://must-not-reach-child",
        "all_proxy": "socks5://must-not-reach-child",
    }
    controller = ForwardingSidecarController(
        (sys.executable, str(FIXTURE)), _up(tmp_path), environ=environment
    )
    controller.close()
    assert spawned == [controller.child_environment]
    assert "TS_AUTHKEY" not in spawned[0]
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        assert controller.child_environment[key] == environment[key]
    environment["HTTPS_PROXY"] = "changed-after-launch"
    assert "fake-proxy-secret" in controller.child_environment["HTTPS_PROXY"]
    assert "ALL_PROXY" not in controller.child_environment
    assert "all_proxy" not in controller.child_environment
    assert "fake-proxy-secret" not in "\n".join(controller.stderr_tail)


def test_abnormal_exit_is_reported_without_waiting_for_next_discovery(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.jsonl"
    exits: list[int] = []
    controller = ForwardingSidecarController(
        (sys.executable, str(FIXTURE)),
        _up(tmp_path),
        environ={
            "FAKE_FORWARD_RECEIPT": str(receipt),
            "FAKE_FORWARD_EXIT_AFTER_READY": "17",
        },
        on_exit=exits.append,
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not exits:
        time.sleep(0.01)
    assert exits == [17]
    controller.close()


def test_controller_close_is_idempotent_and_owns_only_its_child(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.jsonl"
    controller = ForwardingSidecarController(
        (sys.executable, str(FIXTURE)),
        _up(tmp_path),
        environ={"FAKE_FORWARD_RECEIPT": str(receipt), **os.environ},
    )
    pid = controller.pid
    controller.close()
    controller.close()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError(f"owned sidecar pid {pid} survived close")


def test_daemon_starts_the_configured_candidate_sidecar_and_dials_the_slot_set(
    tmp_path: Path, monkeypatch, own_daemon_application
) -> None:
    from hyprial.daemon.impl.forwarding.slots import slot_endpoints

    home = tmp_path / "home"
    receipt = tmp_path / "receipt.jsonl"
    up = _up(tmp_path)
    monkeypatch.setenv("HYPRIAL_ZENOH_SLOT_POOL", "2")
    monkeypatch.setenv(
        "HYPRIAL_FORWARDING_COMMAND", json.dumps([sys.executable, str(FIXTURE)])
    )
    monkeypatch.setenv("HYPRIAL_FORWARDING_UP", json.dumps(up))
    monkeypatch.setenv("FAKE_FORWARD_RECEIPT", str(receipt))
    # Coexist default: without this pin the daemon would also dial the
    # command-directory mid-test.
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    app = own_daemon_application(DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon-state/daemon.sock",
        node_id="forward-node",
        hyprial_home=home,
        owner="tester",
    **_deep_module_daemon_dependencies()))
    monkeypatch.setattr(app, "_tailcat_directory_peers", lambda: DIRECTORY)
    try:
        # Discovery answers the FIXED slot set, derived from the home and
        # pool size -- not per-peer loopback ports.  Mapping is the slot
        # reconcile's job (covered in tests/daemon/forwarding/slots/), so
        # discovery itself never map-peers.
        assert app._discover_peer_endpoints() == slot_endpoints(home, 2)
    finally:
        app._close()
    operations = [json.loads(line)["op"] for line in receipt.read_text().splitlines()]
    # Every incarnation replays the directory-derived allow set before use.
    assert operations == ["up", "status", "down"]


def test_configured_sidecar_start_failure_is_logged_without_directory_fallback(
    tmp_path: Path, monkeypatch, own_daemon_application
) -> None:
    from hyprial.daemon.impl.forwarding.slots import slot_endpoints

    home = tmp_path / "home"
    monkeypatch.setenv("HYPRIAL_ZENOH_SLOT_POOL", "2")
    monkeypatch.setenv("HYPRIAL_FORWARDING_COMMAND", '["/missing/candidate","forward"]')
    monkeypatch.setenv("HYPRIAL_FORWARDING_UP", json.dumps(_up(tmp_path)))
    # Pin the discovery half off: a failing sidecar must not make the test
    # daemon dial anywhere else looking for endpoints.
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    app = own_daemon_application(DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon-state/daemon.sock",
        node_id="forward-node",
        hyprial_home=home,
        owner="tester",
    **_deep_module_daemon_dependencies()))
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **fields: events.append((event, fields)),
    )
    try:
        # Even with the sidecar down, the connect set is the fixed slot
        # set (derivable from the home -- design §5), never the command
        # directory: a failed start must not trigger a fallback dial.
        assert app._discover_peer_endpoints() == slot_endpoints(home, 2)
    finally:
        app._close()
    names = [event for event, _fields in events]
    # The first two events are synchronous and therefore deterministic: the
    # failure itself, then the bounded relaunch the supervisor schedules
    # (the relaunch timer is cancelled by _close; how many of its attempts
    # landed before that is timing and not asserted here).
    assert names[0] == "zenoh.forwarding.start_failed"
    assert "zenoh.forwarding.restarting" in names
    restarting = [
        fields for event, fields in events if event == "zenoh.forwarding.restarting"
    ][0]
    assert restarting["reason"] == "start-failed"
    assert restarting["failures"] == 1
    assert isinstance(restarting["delaySeconds"], float)
    assert restarting["delaySeconds"] > 0


def test_empty_peers_omitted_by_the_sidecar():
    controller = object.__new__(ForwardingSidecarController)
    controller._request = lambda *_: {"v": 5, "event": "state"}
    assert controller.status() == ((), {})


@pytest.mark.parametrize("peers", [None, "100.64.20.2", [None], [{"peer": 1}]])
def test_explicit_invalid_peers_still_rejected(peers):
    controller = object.__new__(ForwardingSidecarController)
    controller._request = lambda *_: {"v": 5, "event": "state", "peers": peers}
    with pytest.raises(ForwardingSidecarError, match="invalid peers"):
        controller.status()


def test_status_parses_legacy_port_only_connection_telemetry() -> None:
    telemetry = {
        "ringCapacity": 16,
        "slots": [
            {
                "peer": "100.64.20.2",
                "mapPort": 43000,
                "closedCount": 2,
                "errorCounts": {"eof": 2, "reset": 1, "canceled": 1},
                "recent": [
                    {
                        "peer": "100.64.20.2",
                        "mapPort": 43000,
                        "bytesIn": 17,
                        "bytesOut": 19,
                        "durationMs": 23,
                        "closeSide": "peer",
                        "errors": {"in": "reset", "out": "canceled"},
                    }
                ],
            }
        ],
    }
    controller = object.__new__(ForwardingSidecarController)
    controller._request = lambda *_: {
        "v": 5,
        "event": "state",
        "peers": [{"peer": "100.64.20.2", "slot": "tcp:127.0.0.1:43000"}],
        "connectionTelemetry": telemetry,
    }

    assert controller.status() == (
        ("100.64.20.2",),
        {"100.64.20.2": "tcp:127.0.0.1:43000"},
    )
    assert controller.connection_telemetry == telemetry


def test_status_preserves_redacted_dial_failure_telemetry() -> None:
    telemetry = {
        "ringCapacity": 16,
        "slots": [
            {
                "peer": "100.64.20.2",
                "slot": "tcp:127.0.0.1:43000",
                "mapPort": 43000,
                "dialAttempts": 3,
                "dialFailures": 3,
                "dialFailureReasons": {
                    "timeout": 0,
                    "refused": 3,
                    "neterr": 0,
                    "other": 0,
                },
                "splicesEstablished": 0,
                "closedCount": 0,
                "closeReasons": {},
                "lastDialAtMs": 1792680000000,
                "lastDialError": "refused",
                "errorCounts": {},
                "recent": [],
            }
        ],
    }
    controller = object.__new__(ForwardingSidecarController)
    controller._request = lambda *_: {
        "v": 5,
        "event": "state",
        "peers": [],
        "connectionTelemetry": telemetry,
    }

    controller.status()

    assert controller.connection_telemetry == telemetry


def test_status_rejects_invalid_connection_telemetry() -> None:
    controller = object.__new__(ForwardingSidecarController)
    controller._request = lambda *_: {
        "v": 5,
        "event": "state",
        "peers": [],
        "connectionTelemetry": {"ringCapacity": 16, "slots": "not-a-list"},
    }

    with pytest.raises(ForwardingSidecarError, match="connection telemetry"):
        controller.status()


def _telemetry_for_slot(*, slot: object, map_port: object) -> dict:
    telemetry = _telemetry_with_errors(error_in="eof", error_out="canceled")
    telemetry_slot = telemetry["slots"][0]
    telemetry_slot["slot"] = slot
    telemetry_slot["mapPort"] = map_port
    telemetry_slot["recent"][0]["slot"] = slot
    telemetry_slot["recent"][0]["mapPort"] = map_port
    return telemetry


def test_status_accepts_v5_tcp_slot_and_numeric_port() -> None:
    telemetry = _telemetry_for_slot(slot="tcp:127.0.0.1:43000", map_port=43000)
    assert _status_for_telemetry(telemetry) == ((), {})


def test_status_accepts_v5_unix_slot_and_null_port() -> None:
    telemetry = _telemetry_for_slot(slot="/tmp/hyprial/slot-00", map_port=None)
    assert _status_for_telemetry(telemetry) == ((), {})


def test_status_rejects_null_port_without_slot() -> None:
    telemetry = _telemetry_with_errors(error_in="eof", error_out="canceled")
    telemetry["slots"][0]["mapPort"] = None
    telemetry["slots"][0]["recent"][0]["mapPort"] = None
    with pytest.raises(ForwardingSidecarError, match="connection telemetry"):
        _status_for_telemetry(telemetry)


@pytest.mark.parametrize("slot", [None, 7, ""])
def test_status_rejects_non_string_or_empty_slot(slot: object) -> None:
    telemetry = _telemetry_for_slot(slot=slot, map_port=43000)
    with pytest.raises(ForwardingSidecarError, match="connection telemetry"):
        _status_for_telemetry(telemetry)


@pytest.mark.parametrize("map_port", [True, "43000", 1.5])
def test_status_rejects_non_integer_non_null_port(map_port: object) -> None:
    telemetry = _telemetry_for_slot(slot="tcp:127.0.0.1:43000", map_port=map_port)
    with pytest.raises(ForwardingSidecarError, match="connection telemetry"):
        _status_for_telemetry(telemetry)


def test_status_records_whether_the_sidecar_reported_peers():
    """An absent key must stay tellable from an explicit empty list."""

    controller = object.__new__(ForwardingSidecarController)
    controller._request = lambda *_: {"v": 5, "event": "state"}
    assert controller.status() == ((), {})
    assert controller.peers_reported is False

    controller._request = lambda *_: {"v": 5, "event": "state", "peers": []}
    assert controller.status() == ((), {})
    assert controller.peers_reported is True


def test_map_peer_slot_binds_and_reports_the_slot(tmp_path: Path) -> None:
    """Protocol v5: map-peer with a slot answers peer-mapped {peer, slot}."""

    receipt = tmp_path / "receipt.jsonl"
    up = _up(tmp_path)
    up["slots"] = ["/tmp/hyprial-slots/slot-00", "/tmp/hyprial-slots/slot-01"]
    up["slotHoldMs"] = 10000
    controller = ForwardingSidecarController(
        (sys.executable, str(FIXTURE)),
        up,
        environ={"FAKE_FORWARD_RECEIPT": str(receipt)},
    )
    try:
        assert (
            controller.map_peer(
                "100.64.20.2", "tcPeerTwo", slot="/tmp/hyprial-slots/slot-00"
            )
            == "/tmp/hyprial-slots/slot-00"
        )
        # The status peers entry carries the slot instead of a localPort.
        assert controller.status() == (
            ("100.64.20.2",),
            {"100.64.20.2": "/tmp/hyprial-slots/slot-00"},
        )
        # A relative slot is rejected locally, before touching the wire.
        with pytest.raises(ForwardingSidecarError, match="BAD_REQUEST"):
            controller.map_peer("100.64.20.3", "tcPeerThree", slot="relative.sock")
        commands = [json.loads(line) for line in receipt.read_text().splitlines()]
        assert commands[0]["slots"] == up["slots"]
        map_commands = [item for item in commands if item["op"] == "map-peer"]
        assert len(map_commands) == 1
        assert map_commands[0].items() >= {
            "v": 6,
            "op": "map-peer",
            "peer": "100.64.20.2",
            "address": "tcPeerTwo",
            "slot": "/tmp/hyprial-slots/slot-00",
        }.items()
        assert isinstance(map_commands[0].get("requestId"), str)
        assert map_commands[0]["requestId"]
    finally:
        controller.close()


def test_authority_map_peer_passes_the_slot_through() -> None:
    from hyprial.daemon.impl.forwarding import ForwardingControllerAuthority

    class _StubController:
        def map_peer(self, peer: str, address: str, slot: str) -> str:
            return slot

        def close(self) -> None:
            return None

    authority = ForwardingControllerAuthority(_StubController())
    try:
        assert authority.map_peer("peer-a", "tcPeerTwo", "/tmp/slots/slot-00") == (
            "/tmp/slots/slot-00"
        )
    finally:
        authority.close()


def _telemetry_with_errors(*, error_in: object, error_out: object) -> dict:
    return {
        "ringCapacity": 16,
        "slots": [
            {
                "peer": "100.64.20.2",
                "mapPort": 43000,
                "closedCount": 1,
                "errorCounts": {"eof": 1},
                "recent": [
                    {
                        "peer": "100.64.20.2",
                        "mapPort": 43000,
                        "bytesIn": 17,
                        "bytesOut": 19,
                        "durationMs": 23,
                        "closeSide": "peer",
                        "errors": {"in": error_in, "out": error_out},
                    }
                ],
            }
        ],
    }


def _status_for_telemetry(connection_telemetry: object):
    controller = object.__new__(ForwardingSidecarController)
    payload = {"v": 5, "event": "state", "peers": []}
    if connection_telemetry is not None:
        payload["connectionTelemetry"] = connection_telemetry
    controller._request = lambda *_: payload
    return controller.status()


@pytest.mark.parametrize("side", ["in", "out"])
@pytest.mark.parametrize("value", [["x"], {"x": 1}, 7])
def test_recent_error_classes_reject_non_strings(side: str, value: object) -> None:
    other = "eof" if side == "out" else "reset"
    telemetry = _telemetry_with_errors(
        error_in=value if side == "in" else other,
        error_out=value if side == "out" else other,
    )

    with pytest.raises(ForwardingSidecarError, match="connection telemetry"):
        _status_for_telemetry(telemetry)


@pytest.mark.parametrize("value", ["eof", "reset", "timeout", "canceled", "other:custom"])
def test_recent_error_classes_accept_normalized_strings(value: str) -> None:
    telemetry = _telemetry_with_errors(error_in=value, error_out=value)

    assert _status_for_telemetry(telemetry) == ((), {})


def test_null_or_absent_connection_telemetry_is_accepted() -> None:
    assert _status_for_telemetry(None) == ((), {})
    controller = object.__new__(ForwardingSidecarController)
    controller._request = lambda *_: {"v": 5, "event": "state", "peers": []}
    assert controller.status() == ((), {})
