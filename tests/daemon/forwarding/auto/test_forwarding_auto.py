"""Automatic forwarding (forwarding defaults plan step 4b-i, v3 cutover).

``HYPRIAL_FORWARDING=off|auto|on`` (or ``settings.json`` ``forwarding.mode``)
plus a device-ready home resolve into a forwarding plan inside the daemon.
The default is ``auto`` (step 4b-ii): a home without a device key is
unaffected.

Since the tailnet cutover (2026-10-03) eligibility is "the device key and its
published record exist (login ensured them) and the Tailcat sidecar is
locatable" -- there is no control plane to join and no pin to verify here.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import socket
from tempfile import TemporaryDirectory
from pathlib import Path

import pytest

import hyprial.daemon.impl.application.netendpoints.endpoints as netendpoints_module
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.forwarding_config import (
    AutomaticForwarding,
    ForwardingConfigurationError,
    ForwardingPolicy,
    automatic_forwarding,
    forwarding_policy,
)
from hyprial.daemon.impl.network.tailcat import BINARY_ENV
from hyprial.identity.impl.device import (
    DeviceRecord,
    device_key_path,
    write_device_record,
)
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)
from hyprial.shell.impl.daemon_wiring import usage_factory

_OPENED: list[DaemonApplication] = []

#: The directory view the end-to-end test pins: two org members, self excluded.
DIRECTORY = {
    "100.64.20.2": "tcPeerTwo",
    "100.64.20.3": "tcPeerThree",
}


@pytest.fixture
def tmp_path():
    # macOS Unix sockets have a 104-byte path limit.
    with TemporaryDirectory(prefix='hrpy-auto-', dir='/tmp') as directory:
        yield Path(directory)


@pytest.fixture(autouse=True)
def _close_apps(tmp_path):
    yield
    while _OPENED:
        _OPENED.pop()._close()


def _device_home(home: Path) -> Path:
    """What `hyprial login` leaves behind: a device key and its record."""

    home.mkdir(parents=True, exist_ok=True)
    key_file = device_key_path(home)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text('{"v":5,"server":{},"client":{}}', encoding="utf-8")
    key_file.chmod(0o600)
    write_device_record(
        home,
        DeviceRecord(
            device_id="node-a.example",
            owner="tester",
            server_public="nodekey:server",
            client_public="nodekey:client",
        ),
    )
    return home


def _verified(monkeypatch, tmp_path: Path) -> Path:
    """A locatable sidecar binary, named through HYPRIAL_TAILCAT_BINARY."""

    binary = tmp_path / "hyprial-tailcat"
    binary.write_text("sidecar", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv(BINARY_ENV, str(binary))
    return binary


def _from_environment(tmp_path: Path, monkeypatch, **env: str) -> DaemonApplication:
    home = tmp_path / "home"
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HYPRIAL_NODE_ID", "node-a.example")
    for name in (
        "HYPRIAL_NETWORK_ISOLATED",
        "HYPRIAL_FORWARDING",
        "HYPRIAL_FORWARDING_COMMAND",
        "HYPRIAL_FORWARDING_UP",
        "HYPRIAL_FORWARDING_INBOUND_TARGET",
        "HYPRIAL_ZENOH_LISTEN",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    app = DaemonApplication.from_environment(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state/daemon.sock",
        usage_factory=usage_factory,
        **_deep_module_daemon_dependencies(),
    )
    _OPENED.append(app)
    return app


# -- policy -----------------------------------------------------------------


def test_policy_absent_is_auto_and_values_come_from_the_switch() -> None:
    assert forwarding_policy({}) == ForwardingPolicy("auto", "default")
    for mode in ("off", "auto", "on"):
        assert forwarding_policy({"HYPRIAL_FORWARDING": f" {mode.upper()} "}) == (
            ForwardingPolicy(mode, "env")
        )
    with pytest.raises(ForwardingConfigurationError) as raised:
        forwarding_policy({"HYPRIAL_FORWARDING": "sometimes"})
    assert raised.value.code == "FORWARDING_MODE_INVALID"


# -- eligibility ------------------------------------------------------------


def test_a_device_ready_home_with_a_locatable_sidecar_is_eligible(
    tmp_path, monkeypatch
) -> None:
    home = _device_home(tmp_path / "home")
    binary = _verified(monkeypatch, tmp_path)

    plan, reason = automatic_forwarding(home, {BINARY_ENV: str(binary)}, node_id="node-a.example")

    assert reason is None
    assert plan == AutomaticForwarding(home, binary, "node-a.example")


def test_ineligible_homes_say_why(tmp_path, monkeypatch) -> None:
    binary = _verified(monkeypatch, tmp_path)

    bare = tmp_path / "bare"
    bare.mkdir()
    assert automatic_forwarding(bare, {}, node_id="n") == (None, "DEVICE_KEY_MISSING")

    key_only = tmp_path / "key-only"
    key_file = device_key_path(key_only)
    key_file.parent.mkdir(parents=True)
    key_file.write_text("{}", encoding="utf-8")
    assert automatic_forwarding(key_only, {}, node_id="n") == (
        None,
        "DEVICE_RECORD_MISSING",
    )

    ready = _device_home(tmp_path / "ready")
    assert automatic_forwarding(ready, {}, node_id="n") == (None, "SIDECAR_MISSING")
    plan, reason = automatic_forwarding(ready, {BINARY_ENV: str(binary)}, node_id="n")
    assert (plan is not None) and reason is None


# -- the daemon's decision --------------------------------------------------


def test_a_restart_with_no_forwarding_variables_keeps_forwarding(
    tmp_path, monkeypatch
) -> None:
    """Backlog #9's acceptance: a daemon relaunched from a plain shell (no
    forwarding.env, no switch) on a device-ready home still forwards."""

    _device_home(tmp_path / "home")
    binary = _verified(monkeypatch, tmp_path)

    app = _from_environment(tmp_path, monkeypatch)

    assert app._forwarding_automatic is not None
    assert app._forwarding_automatic.binary == binary
    assert app._forwarding_status_json()["policy"] == {
        "mode": "auto",
        "source": "default",
    }


def test_the_default_leaves_a_home_without_a_device_key_alone(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "home").mkdir()

    app = _from_environment(tmp_path, monkeypatch)

    assert app._forwarding_automatic is None
    assert app._forwarding_environment == {}
    status = app._forwarding_status_json()
    assert status["state"] == "off"
    assert status["unavailable"] == "DEVICE_KEY_MISSING"


def _settings(home: Path, record: object) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(record), encoding="utf-8")


def test_settings_mode_applies_and_the_environment_wins(tmp_path, monkeypatch) -> None:
    home = _device_home(tmp_path / "home")
    _verified(monkeypatch, tmp_path)
    _settings(home, {"owner": "o", "forwarding": {"mode": "off"}})

    stored = _from_environment(tmp_path, monkeypatch)
    assert stored._forwarding_automatic is None
    assert stored._forwarding_status_json()["policy"] == {
        "mode": "off",
        "source": "settings",
    }

    overridden = _from_environment(tmp_path, monkeypatch, HYPRIAL_FORWARDING="auto")
    assert overridden._forwarding_automatic is not None
    assert overridden._forwarding_status_json()["policy"]["source"] == "env"


def test_a_damaged_setting_refuses_rather_than_falling_back_to_auto(
    tmp_path, monkeypatch
) -> None:
    """Falling back to the default would undo an operator's ``off`` exactly
    when their file is damaged."""

    home = _device_home(tmp_path / "home")
    _settings(home, {"forwarding": {"mode": "sometimes"}})
    with pytest.raises(ValueError, match="FORWARDING_MODE_INVALID"):
        _from_environment(tmp_path, monkeypatch)

    (home / "settings.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="FORWARDING_SETTINGS_INVALID"):
        _from_environment(tmp_path, monkeypatch)


def test_the_writer_keeps_unrelated_settings(tmp_path) -> None:
    from hyprial.daemon.impl.forwarding_config import write_forwarding_mode

    home = tmp_path / "home"
    _settings(home, {"owner": "o", "autoUpgrade": True, "forwarding": {"x": 1}})

    path = write_forwarding_mode(" OFF ", home)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "owner": "o",
        "autoUpgrade": True,
        "forwarding": {"x": 1, "mode": "off"},
    }
    with pytest.raises(ForwardingConfigurationError):
        write_forwarding_mode("sometimes", home)
    (home / "settings.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot parse"):
        write_forwarding_mode("on", home)
    assert (home / "settings.json").read_text(encoding="utf-8") == "{not json"


def test_config_set_forwarding_mode(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    import hyprial.shell.impl.cli.runtime.entry as cli

    home = tmp_path / "home"
    _settings(home, {"owner": "o"})
    monkeypatch.setenv("HYPRIAL_HOME", str(home))

    result = CliRunner().invoke(
        cli.app, ["config", "set", "forwarding.mode", "off", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["value"] == "off"
    assert forwarding_policy({}, home) == ForwardingPolicy("off", "settings")
    bad = CliRunner().invoke(
        cli.app, ["config", "set", "forwarding.mode", "sometimes", "--json"]
    )
    assert bad.exit_code != 0
    assert "INVALID_CONFIGURATION" in bad.output


def test_auto_plans_for_an_eligible_home_without_choosing_a_port_yet(
    tmp_path, monkeypatch
) -> None:
    _device_home(tmp_path / "home")
    binary = _verified(monkeypatch, tmp_path)

    app = _from_environment(tmp_path, monkeypatch, HYPRIAL_FORWARDING="auto")

    assert app._forwarding_automatic is not None
    assert app._forwarding_automatic.binary == binary
    # The inbound port is certain only once the session binds it.
    assert app._forwarding_environment == {}
    assert app._forwarding_status_json()["policy"] == {"mode": "auto", "source": "env"}


def test_auto_on_an_ineligible_home_is_off_with_a_reason(tmp_path, monkeypatch) -> None:
    (tmp_path / "home").mkdir()

    app = _from_environment(tmp_path, monkeypatch, HYPRIAL_FORWARDING="auto")

    status = app._forwarding_status_json()
    assert status["state"] == "off"
    assert status["unavailable"] == "DEVICE_KEY_MISSING"


def test_on_refuses_to_start_an_ineligible_home(tmp_path, monkeypatch) -> None:
    (tmp_path / "home").mkdir()

    with pytest.raises(ValueError, match="FORWARDING_UNAVAILABLE.*DEVICE_KEY_MISSING"):
        _from_environment(tmp_path, monkeypatch, HYPRIAL_FORWARDING="on")


def test_off_suppresses_even_explicit_forwarding(tmp_path, monkeypatch) -> None:
    """The durable rollback: a stale forwarding.env cannot keep it on."""

    _device_home(tmp_path / "home")
    binary = _verified(monkeypatch, tmp_path)

    app = _from_environment(
        tmp_path,
        monkeypatch,
        HYPRIAL_FORWARDING="off",
        HYPRIAL_FORWARDING_INBOUND_TARGET="127.0.0.1:39111",
        HYPRIAL_ZENOH_LISTEN="tcp/127.0.0.1:39111",
    )

    assert app._forwarding_environment == {}
    assert app._forwarding_automatic is None
    assert binary.is_file()


def test_isolation_vetoes_auto_and_refuses_on(tmp_path, monkeypatch) -> None:
    _device_home(tmp_path / "home")
    _verified(monkeypatch, tmp_path)

    app = _from_environment(
        tmp_path, monkeypatch, HYPRIAL_NETWORK_ISOLATED="1", HYPRIAL_FORWARDING="auto"
    )
    assert app._forwarding_automatic is None
    assert app._forwarding_environment == {}

    with pytest.raises(ValueError, match="isolated daemon never forwards"):
        _from_environment(
            tmp_path, monkeypatch, HYPRIAL_NETWORK_ISOLATED="1", HYPRIAL_FORWARDING="on"
        )


def test_an_explicit_listen_list_is_reused_never_appended(tmp_path, monkeypatch) -> None:
    _device_home(tmp_path / "home")
    _verified(monkeypatch, tmp_path)

    with pytest.raises(ForwardingConfigurationError):
        _from_environment(
            tmp_path, monkeypatch, HYPRIAL_FORWARDING='auto',
            HYPRIAL_ZENOH_LISTEN='tcp/10.0.0.9:7447,tcp/127.0.0.1:39222',
        )

    with pytest.raises(ForwardingConfigurationError):
        _from_environment(tmp_path, monkeypatch, HYPRIAL_FORWARDING='auto',
                          HYPRIAL_ZENOH_LISTEN='tcp/10.0.0.9:7447')


# -- startup: listener composition, port ownership, first launch ------------


def _free_endpoint() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"


class _Plan:
    """AutomaticForwarding's shape; records the target it was completed with."""

    def __init__(self) -> None:
        self.targets: list[str] = []

    def environment(self, target: str) -> dict[str, str]:
        self.targets.append(target)
        return {"HYPRIAL_FORWARDING_COMMAND": '["fake"]', "HYPRIAL_FORWARDING_UP": "{}"}


def _started(tmp_path: Path, monkeypatch, derived: str | None, plan: _Plan) -> DaemonApplication:
    home = tmp_path / "home"
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.setattr(
        DaemonApplication, "_derive_listen_endpoint", lambda self: derived
    )
    launched: list[dict[str, str]] = []

    def launch(self) -> None:
        # Control/membership starts before the upper-layer session.
        if not launched:
            assert self._transport is None
            launched.append(dict(self._forwarding_environment))

    monkeypatch.setattr(DaemonApplication, "_start_forwarding_supervisor", launch)
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="auto-node",
        hyprial_home=home,
        owner="tester",
        forwarding_policy=ForwardingPolicy("auto", "env"),
        forwarding_automatic=plan,  # type: ignore[arg-type]
    **_deep_module_daemon_dependencies())
    _OPENED.append(app)
    app._start_runtime()
    app.launched = launched  # type: ignore[attr-defined]
    return app


def test_the_loopback_listener_is_added_after_the_nodes_own_listener(
    tmp_path, monkeypatch
) -> None:
    """The plan's RED: a loopback listener used to replace the node's own
    listener instead of being added after it."""

    derived = _free_endpoint()
    plan = _Plan()

    app = _started(tmp_path, monkeypatch, derived, plan)

    assert len(app.zenoh_listen) == 1
    private = app.zenoh_listen[0]
    assert private.startswith('unixsock-stream/')
    assert derived not in app.zenoh_listen
    assert plan.targets == ['unix:' + private.removeprefix('unixsock-stream/')]
    assert app.launched == [  # type: ignore[attr-defined]
        {"HYPRIAL_FORWARDING_COMMAND": '["fake"]', "HYPRIAL_FORWARDING_UP": "{}"}
    ]


def test_a_derived_listener_collision_does_not_create_a_tcp_bypass(tmp_path, monkeypatch):
    with socket.socket() as held:
        held.bind(('127.0.0.1', 0))
        held.listen()
        derived = f'tcp/127.0.0.1:{held.getsockname()[1]}'
        app = _started(tmp_path, monkeypatch, derived, _Plan())
    assert len(app.zenoh_listen) == 1
    assert app.zenoh_listen[0].startswith('unixsock-stream/')


def test_private_listener_failure_does_not_fall_back_to_tcp(tmp_path, monkeypatch):
    def fail(config, **kwargs):
        raise RuntimeError('private socket bind failed')
    monkeypatch.setattr(netendpoints_module, 'ZenohTransport', fail)
    with pytest.raises(RuntimeError, match='private socket bind failed'):
        _started(tmp_path, monkeypatch, None, _Plan())


def test_auto_end_to_end_launches_the_sidecar_at_the_bound_port_and_dials_its_peers(
    tmp_path, monkeypatch
) -> None:
    """The whole automatic path with the real supervisor and session: the
    sidecar is told the port this daemon's session actually holds, and the
    directory peers it maps are dialed by the step 3 redial -- no restart."""

    import sys
    import time

    fixture = Path(__file__).resolve().parents[4] / 'tests/fixtures/network/fake_forwarding_sidecar.py'
    wrapper = tmp_path / "hyprial-tailcat"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fixture}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    home = _device_home(tmp_path / "home")
    receipt = tmp_path / "receipt.jsonl"
    monkeypatch.setenv("FAKE_FORWARD_RECEIPT", str(receipt))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    # The whole automatic path over the fixed slot pool: the session opens
    # on the fixed slot set and the directory peers bind onto slots (no
    # restart).  A 2-slot pool keeps the dialed set short and assertable.
    monkeypatch.setenv("HYPRIAL_ZENOH_SLOT_POOL", "2")
    monkeypatch.setattr(
        DaemonApplication, "_tailcat_directory_peers", lambda self: DIRECTORY
    )
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="auto-node",
        hyprial_home=home,
        owner="tester",
        forwarding_policy=ForwardingPolicy("auto", "env"),
        forwarding_automatic=AutomaticForwarding(home, wrapper, "auto-node"),
    **_deep_module_daemon_dependencies())
    _OPENED.append(app)
    app._start_runtime()

    (loopback,) = app.zenoh_listen
    ups = [
        json.loads(line)
        for line in receipt.read_text(encoding="utf-8").splitlines()
        if '"up"' in line
    ]
    assert ups and ups[0]['inboundTarget'] == 'unix:' + loopback.removeprefix('unixsock-stream/')
    assert ups[0]["keyFile"] == str(device_key_path(home))

    from hyprial.daemon.impl.forwarding.slots import slot_endpoints

    # The session dials the FIXED slot set, which never changes as peers
    # bind (no restart); that the zenoh connect set holds the slots is the
    # end-to-end truth, independent of the status projection.
    wanted = list(slot_endpoints(home, 2))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        app._reconcile_forwarding_endpoints()
        if set(wanted) <= set(app._transport.config.connect):  # type: ignore[union-attr]
            break
        time.sleep(0.1)
    status = app._forwarding_status_json()
    assert status["state"] == "running"
    assert status["endpoints"] == wanted
    assert status["restartRequired"] is False
    assert status["policy"] == {"mode": "auto", "source": "env"}
    assert set(wanted) <= set(app._transport.config.connect)  # type: ignore[union-attr]
    # The directory peers bound onto slots (map-peer), reported in status.
    assert {entry["peer"] for entry in status["slots"]["assigned"]} == set(DIRECTORY)
