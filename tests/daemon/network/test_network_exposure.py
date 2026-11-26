from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from hyprial.daemon.impl.forwarding import (
    ExposureStore,
    ForwardingSidecarController,
    ForwardingSidecarError,
    ForwardingSidecarSupervisor,
    validate_unix_exposure_target,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

FIXTURE = Path(__file__).resolve().parents[3] / 'tests/fixtures/network/fake_forwarding_sidecar.py'


class _Controller:
    def __init__(self) -> None:
        self.applied: list[dict[str, object]] = []
        self.admissions: list[tuple[str, list[str]]] = []
        self.closed = False

    def expose(
        self, exposure: dict[str, object], *, admission: str, keys: Any = ()
    ) -> dict[str, object]:
        self.applied.append(dict(exposure))
        self.admissions.append((admission, list(keys)))
        return dict(exposure)

    def close(self) -> None:
        self.closed = True


def test_desired_exposures_persist_and_are_reissued_after_restart(
    tmp_path: Path, generation_scheduler
) -> None:
    """A persisted exposure is replayed on every sidecar start, admitting nobody.

    Service grant S4-py: the replay is what closes the gap between a sidecar
    coming up and the admission set being derived, so it must never be the
    thing that opens the port.
    """

    path = tmp_path / "network-exposures.json"
    store = ExposureStore(path)
    expected = {
        "port": 8443,
        "target": "tcp:127.0.0.1:9443",
        "proxyProtocol": "none",
    }
    store.set(expected)
    assert ExposureStore(path).list() == [expected]

    made: list[_Controller] = []
    callbacks: list[Any] = []

    def factory(_environ, on_exit):
        callbacks.append(on_exit)
        controller = _Controller()
        made.append(controller)
        return controller

    supervisor = ForwardingSidecarSupervisor(
        {},
        event_log=lambda *_args, **_kwargs: None,
        controller_factory=factory,
        scheduler=generation_scheduler,
        desired_exposures=store.list,
    )
    supervisor.ensure_started()
    assert made[0].applied == [expected]
    assert made[0].admissions == [("keys", [])]

    callbacks[0](17)
    supervisor._scheduled_relaunch(
        supervisor._generation
    )  # exercise the real spawn seam
    assert made[1].applied == [expected]
    assert made[1].admissions == [("keys", [])]
    supervisor.close()


def test_old_sidecar_refuses_expose_without_hanging() -> None:
    controller = object.__new__(ForwardingSidecarController)
    controller._expose_capable = False

    with pytest.raises(ForwardingSidecarError, match="SIDECAR_EXPOSE_UNSUPPORTED"):
        controller.expose(
            {
                "port": 8443,
                "target": "tcp:127.0.0.1:9443",
                "proxyProtocol": "none",
            },
            admission="keys",
        )


@pytest.mark.parametrize("own_io", [False, True])
def test_controller_round_trips_expose_unexpose_and_peer_key(
    tmp_path: Path, actor_host, own_io: bool
) -> None:
    receipt = tmp_path / "receipt.jsonl"
    controller = ForwardingSidecarController(
        [sys.executable, str(FIXTURE)],
        {
            "v": 7,
            "op": "up",
            "keyFile": str(tmp_path / "device-key.json"),
            "membershipStore": str(tmp_path / 'membership.json'),
            "owner": "alice",
            "deviceId": "local",
            "controlPort": 7448,
            "addressFile": str(tmp_path / "state/tailcat/address"),
            "inboundTarget": 'unix:' + str(tmp_path / 'private.sock'),
            "peerPort": 7447,
        },
        environ={"FAKE_FORWARD_RECEIPT": str(receipt)},
    )
    if own_io:
        from hyprial.daemon.impl.forwarding import ForwardingControllerAuthority

        controller = ForwardingControllerAuthority(controller, actor_host=actor_host)
    try:
        exposure = {
            "port": 8443,
            "target": "tcp:127.0.0.1:9443",
            "proxyProtocol": "none",
        }
        assert controller.expose(exposure, admission="allow") == exposure
        assert controller.peer_key("100.64.20.8:54321") == {
            "addr": "100.64.20.8:54321",
            "found": True,
            "key": "nodekey:fixture-peer",
        }
        controller.unexpose(8443)
    finally:
        controller.close()

    if own_io:
        for invoke in (
            lambda: controller.expose(exposure, admission="allow"),
            lambda: controller.unexpose(8443),
            lambda: controller.peer_key("100.64.20.8:54321"),
        ):
            with pytest.raises(ForwardingSidecarError, match="I/O owner overloaded"):
                invoke()

    commands = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert [item["op"] for item in commands] == [
        "up",
        "expose",
        "peer-key",
        "unexpose",
        "down",
    ]
    # Protocol v3: the Python-side "none" went out as "" on the wire.
    assert commands[1]["proxyProtocol"] == ""
    # Protocol v7: the caller's explicit admission goes out as it was given.
    assert commands[1]["admission"] == "allow"


def test_unix_target_directory_requires_owner_only_mode(tmp_path: Path) -> None:
    directory = tmp_path / "service"
    directory.mkdir(mode=0o700)
    target = f"unix:{directory / 'orgfs.sock'}"
    assert validate_unix_exposure_target(target) == target

    directory.chmod(0o755)
    with pytest.raises(ForwardingSidecarError, match="0700"):
        validate_unix_exposure_target(target)


def test_network_peer_key_ipc_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v3 rename: network.whois is gone; network.peerKey answers."""

    import hyprial.daemon.impl.application.netendpoints.forwarding as application_module
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.kernel import DaemonRequestError

    state = tmp_path / "state"
    state.mkdir()
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="peer-key-ipc-test",
        hyprial_home=tmp_path / "home",
    **_deep_module_daemon_dependencies())
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    calls: list[str] = []

    class _Supervisor:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def ensure_started(self) -> None:
            pass

        def peer_key(self, address: str) -> dict[str, object]:
            calls.append(address)
            return {"addr": address, "found": True, "key": "nodekey:fixture"}

    monkeypatch.setattr(
        application_module, "ForwardingSidecarSupervisor", _Supervisor
    )
    app._forwarding_environment = {"HYPRIAL_FORWARDING_TEST": "1"}
    try:
        result = app.handle("network.peerKey", {"addr": "100.64.20.8:54321"})
        assert result == {
            "addr": "100.64.20.8:54321",
            "found": True,
            "key": "nodekey:fixture",
        }
        assert calls == ["100.64.20.8:54321"]

        with pytest.raises(DaemonRequestError):
            app.handle("network.peerKey", {"addr": "not-an-address"})
        with pytest.raises(DaemonRequestError):
            app.handle("network.whois", {"addr": "100.64.20.8:54321"})
    finally:
        app._agent_session_domains.close()


def test_service_cli_help_inventory() -> None:
    root = get_command(_cli_root_app)
    assert "host" in root.commands
    assert "network" not in root.commands
    assert "transfer" not in root.commands["agent"].commands
    assert "runtime" not in root.commands["agent"].commands
    host = root.commands["host"]
    assert hasattr(host, "commands")
    assert {"expose", "unexpose", "exposures", "peer-key"}.isdisjoint(host.commands)
    service = root.commands["org"].commands["service"]
    assert hasattr(service, "commands")
    assert {"expose", "unexpose", "exposures", "whois"} <= set(service.commands)

    result = CliRunner().invoke(_cli_root_app, ["org", "service", "--help"])
    assert result.exit_code == 0
    for command in ("expose", "unexpose", "exposures", "whois"):
        assert command in result.output


def test_proxy_v2_requires_a_unix_target() -> None:
    """A loopback TCP backend can't tell the sidecar from a local forger."""

    from hyprial.daemon.impl.forwarding import _normalize_exposure

    with pytest.raises(ForwardingSidecarError, match="requires a unix: target"):
        _normalize_exposure(
            {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "v2"}
        )
    assert (
        _normalize_exposure(
            {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "none"}
        )["proxyProtocol"]
        == "none"
    )
    assert (
        _normalize_exposure(
            {
                "port": 8443,
                "target": "unix:/private/orgfs/fs.sock",
                "proxyProtocol": "v2",
            }
        )["proxyProtocol"]
        == "v2"
    )


def test_invalid_persisted_exposure_is_skipped_not_fatal(tmp_path: Path) -> None:
    """A stale entry that no longer validates can't take down forwarding."""

    path = tmp_path / "network-exposures.json"
    good = {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "none"}
    stale = {"port": 8444, "target": "tcp:127.0.0.1:9444", "proxyProtocol": "v2"}
    path.write_text(json.dumps({"version": 1, "exposures": [good, stale]}))
    store = ExposureStore(path)

    assert store.list() == [good]
    [rejected] = store.rejected()
    assert rejected["exposure"] == stale
    assert "requires a unix: target" in rejected["reason"]


class _RefusingController(_Controller):
    def expose(
        self, exposure: dict[str, object], *, admission: str, keys: Any = ()
    ) -> dict[str, object]:
        if exposure["port"] == 8444:
            raise ForwardingSidecarError("sidecar refused this exposure")
        return super().expose(exposure, admission=admission, keys=keys)


def test_one_refused_exposure_does_not_stop_forwarding_start(
    generation_scheduler,
) -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []
    made: list[_RefusingController] = []
    first = {"port": 8444, "target": "tcp:127.0.0.1:9444", "proxyProtocol": "none"}
    second = {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "none"}

    def factory(_environ, _on_exit):
        controller = _RefusingController()
        made.append(controller)
        return controller

    supervisor = ForwardingSidecarSupervisor(
        {},
        event_log=lambda level, event, **fields: events.append((level, event, fields)),
        controller_factory=factory,
        scheduler=generation_scheduler,
        desired_exposures=lambda: [first, second],
    )
    supervisor.ensure_started()
    try:
        assert made and not made[0].closed
        assert made[0].applied == [second]
        assert any(
            event == "network.exposure.rejected" and fields.get("port") == 8444
            for _level, event, fields in events
        )
        assert not any(
            event == "zenoh.forwarding.start_failed" for _l, event, _f in events
        )
    finally:
        supervisor.close()


def test_skipped_exposures_are_visible_to_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persisted entry that no longer validates is reported, not dropped."""

    from hyprial.daemon.impl import application as application_module
    from hyprial.daemon.impl.application import DaemonApplication

    state = tmp_path / "state"
    state.mkdir()
    good = {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "none"}
    stale = {"port": 8444, "target": "tcp:127.0.0.1:9444", "proxyProtocol": "v2"}
    (state / "network-exposures.json").write_text(
        json.dumps({"version": 1, "exposures": [good, stale]})
    )
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="exposure-visibility-test",
        hyprial_home=tmp_path / "home",
    **_deep_module_daemon_dependencies())
    # handle() only asserts these exist; network.exposures never uses them.
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    try:
        result = app.handle("network.exposures", {})
        assert result["exposures"] == [good]
        [rejected] = result["rejected"]
        assert rejected["exposure"] == stale
        assert "requires a unix: target" in rejected["reason"]

        logged: list[tuple[str, str, dict[str, Any]]] = []
        monkeypatch.setattr(
            app,
            "_log",
            lambda level, _component, event, **fields: logged.append(
                (level, event, fields)
            ),
        )

        class _Supervisor:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def ensure_started(self) -> None:
                pass

        monkeypatch.setattr(
            application_module, "ForwardingSidecarSupervisor", _Supervisor
        )
        app._forwarding_environment = {"HYPRIAL_FORWARDING_TEST": "1"}
        app._start_forwarding_supervisor()
        assert [
            (level, fields["port"])
            for level, event, fields in logged
            if event == "network.exposure.skipped"
        ] == [("warn", 8444)]
    finally:
        app._agent_session_domains.close()


def test_network_exposures_text_lists_entries_that_are_not_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {
            "exposures": [],
            "rejected": [
                {
                    "exposure": {"port": 8444, "target": "tcp:127.0.0.1:9444"},
                    "reason": "proxyProtocol v2 requires a unix: target",
                }
            ],
        },
    )
    result = CliRunner().invoke(_cli_root_app, ["org", "service", "exposures"])
    assert result.exit_code == 0, result.output
    assert result.output.split() == [
        "8444",
        "NOT",
        "SERVED",
        "proxyProtocol",
        "v2",
        "requires",
        "a",
        "unix:",
        "target",
    ]


def test_fake_sidecar_validates_v7_expose_admission_and_key_count(tmp_path: Path) -> None:
    """Fake sidecar pins protocol-v7 expose admission, canonical keys, and keyCount."""

    import subprocess

    receipt = tmp_path / "receipt.jsonl"
    key_a = "nodekey:" + "0" * 63 + "1"
    commands = [
        {
            "v": 7,
            "op": "expose",
            "requestId": "missing-admission",
            "port": 8443,
            "target": "tcp:127.0.0.1:9443",
            "proxyProtocol": "",
        },
        {
            "v": 7,
            "op": "expose",
            "requestId": "allow-ok",
            "port": 8443,
            "target": "tcp:127.0.0.1:9443",
            "proxyProtocol": "",
            "admission": "allow",
        },
        {
            "v": 7,
            "op": "expose",
            "requestId": "keys-ok",
            "port": 8444,
            "target": "tcp:127.0.0.1:9444",
            "proxyProtocol": "",
            "admission": "keys",
            "keys": [key_a],
        },
        {
            "v": 7,
            "op": "expose",
            "requestId": "keys-dup",
            "port": 8444,
            "target": "tcp:127.0.0.1:9444",
            "proxyProtocol": "",
            "admission": "keys",
            "keys": [key_a, key_a],
        },
        {
            "v": 7,
            "op": "expose",
            "requestId": "keys-upper",
            "port": 8444,
            "target": "tcp:127.0.0.1:9444",
            "proxyProtocol": "",
            "admission": "keys",
            "keys": ["nodekey:" + "A" * 64],
        },
        {"v": 7, "op": "down"},
    ]
    payload = "\n".join(json.dumps(item) for item in commands) + "\n"
    completed = subprocess.run(
        [sys.executable, str(FIXTURE)],
        input=payload,
        text=True,
        capture_output=True,
        env={"FAKE_FORWARD_RECEIPT": str(receipt)},
        check=True,
        timeout=15,
    )
    events = [json.loads(line) for line in completed.stdout.splitlines()]
    by_request = {event["requestId"]: event for event in events if "requestId" in event}
    assert by_request["missing-admission"]["event"] == "error"
    assert by_request["missing-admission"]["code"] == "BAD_REQUEST"
    assert by_request["allow-ok"]["event"] == "exposed"
    assert by_request["allow-ok"]["admission"] == "allow"
    assert "keyCount" not in by_request["allow-ok"]
    assert by_request["keys-ok"]["event"] == "exposed"
    assert by_request["keys-ok"]["admission"] == "keys"
    assert by_request["keys-ok"]["keyCount"] == 1
    assert "keys" not in by_request["keys-ok"]
    assert by_request["keys-dup"]["event"] == "error"
    assert by_request["keys-dup"]["code"] == "BAD_REQUEST"
    assert by_request["keys-upper"]["event"] == "error"
    assert by_request["keys-upper"]["code"] == "BAD_REQUEST"


@pytest.mark.parametrize(
    "admission,keys,wire",
    [
        ("allow", (), {"admission": "allow"}),
        ("keys", (), {"admission": "keys", "keys": []}),
        (
            "keys",
            ("nodekey:" + "ab" * 32,),
            {"admission": "keys", "keys": ["nodekey:" + "ab" * 32]},
        ),
    ],
)
def test_controller_expose_sends_the_callers_admission(
    admission: str, keys: tuple, wire: dict[str, Any]
) -> None:
    """Protocol v7 requires an explicit admission on every expose; there is no default.

    Service grant S4-py: the controller used to send "allow" when the caller
    said nothing. It now refuses a call without a mode, and puts exactly the
    caller's mode and key list on the wire.
    """

    controller = object.__new__(ForwardingSidecarController)
    controller._expose_capable = True
    sent: list[dict[str, object]] = []

    def fake_request(payload: dict[str, object], expected_event: str) -> dict[str, object]:
        sent.append(dict(payload))
        assert expected_event == "exposed"
        reply: dict[str, object] = {
            "v": payload["v"],
            "event": "exposed",
            "port": payload["port"],
            "target": payload["target"],
            "proxyProtocol": payload["proxyProtocol"],
            "admission": payload["admission"],
        }
        if payload["admission"] == "keys":
            reply["keyCount"] = len(payload["keys"])
        return reply

    controller._request = fake_request  # type: ignore[method-assign]
    result = controller.expose(
        {
            "port": 8443,
            "target": "tcp:127.0.0.1:9443",
            "proxyProtocol": "none",
        },
        admission=admission,
        keys=keys,
    )
    assert sent == [
        {
            "v": 7,
            "op": "expose",
            "port": 8443,
            "target": "tcp:127.0.0.1:9443",
            "proxyProtocol": "",
            **wire,
        }
    ]
    assert result == {
        "port": 8443,
        "target": "tcp:127.0.0.1:9443",
        "proxyProtocol": "none",
    }


def test_controller_expose_without_a_mode_is_refused_before_the_wire() -> None:
    """An expose that says whom to admit nowhere is the one thing v7 does not accept."""

    controller = object.__new__(ForwardingSidecarController)
    controller._expose_capable = True
    sent: list[dict[str, object]] = []

    def fake_request(payload: dict[str, object], expected_event: str) -> dict[str, object]:
        sent.append(dict(payload))
        raise AssertionError("the request must not reach the sidecar")

    controller._request = fake_request  # type: ignore[method-assign]
    with pytest.raises(TypeError, match="admission"):
        controller.expose(
            {
                "port": 8443,
                "target": "tcp:127.0.0.1:9443",
                "proxyProtocol": "none",
            }
        )
    assert sent == []

