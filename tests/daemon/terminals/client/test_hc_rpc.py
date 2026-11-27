from pathlib import Path

import pytest

from hyprial.daemon.impl.terminals.client.protocol import HcRpcError
from hyprial.daemon.impl.terminals.client.rpc import HcClient, probe_socket_alive
from tests.daemon.terminals.fake_hc.server import FakeHcServer


def test_probe_only_connects_and_round_trips_terminal_lifecycle(tmp_path: Path) -> None:
    endpoint = tmp_path / "hc.sock"
    assert probe_socket_alive(endpoint) is False
    with FakeHcServer(endpoint) as server:
        assert probe_socket_alive(endpoint) is True
        client = HcClient(endpoint)
        hello = client.hello()
        assert hello.generation == 1
        assert hello.framed is False
        assert hello.version == "0.1.22"
        assert hello.core.name == "fake-core"
        assert client.server_status().version == "0.1.22"
        terminal = client.terminal_run(
            ["claude", "--model", "demo"],
            cwd=str(tmp_path),
            env={"TERM": "xterm"},
            labels={"agent": "a1"},
            name="member-1",
        )
        assert terminal.argv == ("claude", "--model", "demo")
        assert terminal.labels == {"agent": "a1"}
        assert terminal.state == "live"
        assert client.terminal_get(terminal.address) == terminal
        assert client.terminal_list(labels={"agent": "a1"})[0] == terminal
        updated = client.terminal_label(terminal.address, {"role": "tui", "agent": ""})
        assert updated.labels == {"role": "tui"}
        killed = client.terminal_kill(terminal.address)
        assert killed.state == "finished"
        assert killed.reason == "killed"
        assert client.terminal_rm(terminal.address) == {"removed": terminal.address}
        run_frame = next(frame for frame in server.frames if frame["method"] == "terminal.run")
        assert run_frame["params"]["argv"] == ["claude", "--model", "demo"]
        assert run_frame["params"]["keep"] is False
        assert run_frame["params"]["no_parent"] is True
        assert run_frame["params"]["no_notify"] is True
        assert run_frame["params"]["env"] == {"TERM": "xterm"}


def test_terminal_run_validates_values_and_reuses_finished_names(tmp_path: Path) -> None:
    endpoint = tmp_path / "hc.sock"
    with FakeHcServer(endpoint):
        client = HcClient(endpoint)
        with pytest.raises(ValueError, match="env values"):
            client.terminal_run(["pi"], env={"BAD": 1})
        with pytest.raises(ValueError, match="name"):
            client.terminal_run(["pi"], name="Bad Name")
        first = client.terminal_run(["pi"], name="same-name")
        with pytest.raises(HcRpcError) as duplicate:
            client.terminal_run(["pi"], name="same-name")
        assert duplicate.value.kind == "already_exists"
        client.terminal_kill(first.address)
        reused = client.terminal_run(["pi"], name="same-name")
        assert reused.address != first.address


def test_server_stop_finishes_non_kept_terminals(tmp_path: Path) -> None:
    endpoint = tmp_path / "hc.sock"
    with FakeHcServer(endpoint):
        client = HcClient(endpoint)
        terminal = client.terminal_run(["pi"], keep=False)
        kept = client.terminal_run(["pi"], keep=True)
        result = client.server_stop()
        assert result == {"stopped": True}
        assert client.terminal_get(terminal.address).reason == "server_stop"
        assert client.terminal_get(terminal.address).signal == 1
        assert client.terminal_get(kept.address).state == "live"


def test_rpc_error_uses_data_kind_over_numeric_code(tmp_path: Path) -> None:
    endpoint = tmp_path / "hc.sock"
    with FakeHcServer(endpoint, error_code=-32000):
        client = HcClient(endpoint)
        with pytest.raises(HcRpcError) as error:
            client.terminal_get("missing")
        assert error.value.kind == "not_found"
    with FakeHcServer(endpoint, error_code=-32603):
        client = HcClient(endpoint)
        with pytest.raises(HcRpcError) as error:
            client.terminal_get("missing")
        assert error.value.kind == "not_found"


def test_client_does_not_expose_server_restart() -> None:
    assert not hasattr(HcClient, "server_restart")
