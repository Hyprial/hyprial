from pathlib import Path

import pytest

from hyprial.daemon.impl.terminals.client.events import HcEventSubscription
from hyprial.daemon.impl.terminals.client.protocol import HcHeartbeatTimeout, HcStreamClosed
from hyprial.daemon.impl.terminals.client.rpc import HcClient
from tests.daemon.terminals.fake_hc.server import FakeHcServer


def test_subscription_delivers_ordered_events_and_filters_stale_unknown(tmp_path: Path) -> None:
    endpoint = tmp_path / "hc.sock"
    with FakeHcServer(endpoint) as server:
        client = HcClient(endpoint)
        terminal = client.terminal_run(["pi"], name="member")
        subscription = HcEventSubscription(client, read_timeout=0.2)
        assert server.frames[-1]["method"] == "events.subscribe"
        assert "docs" not in server.frames[-1]["params"]
        try:
            server.emit_event("doc", terminal.address, {"ignored": True})
            server.emit_event("created", terminal.address, {"argv": ["pi"]})
            server.emit_status(
                terminal.address,
                {"state": "blocked", "kind": "permission", "app": "pi", "msg": "allow"},
            )
            server.emit_status(terminal.address, None)
            server.finish_terminal(terminal.address, exit_code=0, reason="exited")
            server.emit_event("removed", terminal.address, {})
            received = [subscription.next_event() for _ in range(5)]
            assert [event.type for event in received] == [
                "created",
                "status",
                "status",
                "finished",
                "removed",
            ]
            assert received[1].data["status"]["kind"] == "permission"
            assert received[2].data["status"] is None
            assert received[3].data["reason"] == "exited"
        finally:
            subscription.close()


def test_heartbeat_keeps_subscription_alive_and_stall_is_bounded(tmp_path: Path) -> None:
    endpoint = tmp_path / "hc.sock"
    with FakeHcServer(endpoint, heartbeat_interval=0.01):
        subscription = HcEventSubscription(HcClient(endpoint), read_timeout=0.05)
        try:
            assert subscription.next_event().type == "heartbeat"
            assert subscription.next_event().type == "heartbeat"
        finally:
            subscription.close()
    with FakeHcServer(endpoint):
        subscription = HcEventSubscription(HcClient(endpoint), read_timeout=0.03)
        with pytest.raises(HcHeartbeatTimeout):
            subscription.next_event()
        subscription.close()


def test_missed_replaces_snapshot_and_updates_watermark(tmp_path: Path) -> None:
    endpoint = tmp_path / "hc.sock"
    with FakeHcServer(endpoint) as server:
        client = HcClient(endpoint)
        terminal = client.terminal_run(["pi"], name="member")
        subscription = HcEventSubscription(client, read_timeout=0.2)
        try:
            server.emit_missed([terminal], seq=42)
            event = subscription.next_event()
            assert event.type == "missed"
            assert event.seq == 42
            assert event.snapshot is not None
            assert event.snapshot.seq == 42
            server.emit_event("finished", terminal.address, {"reason": "exited"}, seq=43)
            assert subscription.next_event().seq == 43
        finally:
            subscription.close()


def test_core_restart_closes_old_stream_and_new_snapshot_resets_seq(tmp_path: Path) -> None:
    endpoint = tmp_path / "hc.sock"
    with FakeHcServer(endpoint) as server:
        client = HcClient(endpoint)
        subscription = HcEventSubscription(client, read_timeout=0.2)
        server.emit_event("created", "t-before", {})
        assert subscription.next_event().type == "created"
        server.simulate_core_restart(new_version="0.1.22")
        with pytest.raises(HcStreamClosed):
            subscription.next_event()
        fresh = HcEventSubscription(client, read_timeout=0.2)
        try:
            assert fresh.snapshot.seq < subscription.last_seq
        finally:
            fresh.close()
