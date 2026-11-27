from pathlib import Path

from hyprial.daemon.impl.terminals.client.rpc import HcClient
from tests.daemon.terminals.fake_hc.server import FakeHcServer


def test_fake_server_supports_multiple_clients_and_configurable_status(tmp_path: Path) -> None:
    endpoint = tmp_path / "hc.sock"
    with FakeHcServer(
        endpoint,
        version="0.1.21",
        generation=1,
        holders=[{"pid": 41, "instance": "holder-a", "current": True, "terminals": 0}],
    ) as server:
        first = HcClient(endpoint)
        second = HcClient(endpoint)
        assert first.hello().version == "0.1.21"
        status = second.server_status()
        assert status.generation == 1
        assert status.holders[0].instance == "holder-a"
        terminal = first.terminal_run(["pi"], name="member")
        server.emit_status(terminal.address, {"state": "working"})
        assert second.terminal_get(terminal.address).status.state == "working"


def test_fake_server_lifecycle_helpers_mark_lost_terminals(tmp_path: Path) -> None:
    endpoint = tmp_path / "hc.sock"
    with FakeHcServer(endpoint) as server:
        client = HcClient(endpoint)
        terminal = client.terminal_run(["pi"], name="member")
        server.lose_holder(terminal.address)
        lost = client.terminal_get(terminal.address)
        assert lost.state == "lost"
        assert lost.reason == "holder_lost"
