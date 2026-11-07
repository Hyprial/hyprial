"""holders_online regressions: live serving/presence union, not probe cache."""

from __future__ import annotations

import socket
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import KeySpace, TransportSample, ZenohConfig, ZenohTransport


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _eventually(action, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (AssertionError, OrgFsError) as error:
            last_error = error
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _assert_equal(actual, expected):
    assert actual == expected
    return actual


def _loopback_pair(tmp_path):
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Holder status after private sidecar admission (§7), not directory enrollment.
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    peer = OrgFsRuntime(tmp_path / "peer", node_id="node-peer", author="user:peer", trust_tailnet_members=True)
    time.sleep(0.3)
    owner.bind_transport(owner_transport, supplier_online=lambda _node: True)
    peer.bind_transport(
        peer_transport,
        supplier_online=lambda _node: True,
        holder_candidates=lambda: ("node-owner",),
    )
    return owner, peer, owner_transport, peer_transport


def test_serving_owner_is_listed_in_holders_online(tmp_path) -> None:
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner")
    try:
        space = owner.facade.create_space("holders online self")
        assert "node-owner" not in owner.status(space.space_id).holders_online

        owner.serve(space.space_id, "memory")

        status = owner.status(space.space_id)
        assert "node-owner" in status.holders_online
    finally:
        owner.close()


def test_online_holding_peer_is_listed_and_drops_when_offline(tmp_path) -> None:
    owner, peer, owner_transport, peer_transport = _loopback_pair(tmp_path)
    peer_token = peer_transport.declare_liveliness(
        KeySpace().actor_liveliness("node-peer")
    )
    try:
        space = owner.facade.create_space("holders online peer")
        owner.facade.invite(space.space_id, "user:peer", "rw")
        owner._announce_all()
        _eventually(lambda: peer.facade.join(space.space_id))
        peer.serve(space.space_id, "memory")
        peer._announce_all()

        _eventually(
            lambda: _assert_equal(
                "node-peer" in owner.status(space.space_id).holders_online,
                True,
            )
        )

        peer_token.close()

        _eventually(
            lambda: _assert_equal(
                "node-peer" in owner.status(space.space_id).holders_online,
                False,
            )
        )
    finally:
        peer_token.close()
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()


def test_peer_that_never_announced_the_space_is_not_listed(tmp_path) -> None:
    owner, peer, owner_transport, peer_transport = _loopback_pair(tmp_path)
    peer_transport.declare_liveliness(KeySpace().actor_liveliness("node-peer"))
    try:
        space = owner.facade.create_space("holders online non-holder")
        owner.serve(space.space_id, "memory")
        owner._announce_all()
        # The peer is alive and online but never joins or announces the space.
        _eventually(lambda: _assert_equal("node-peer" in owner._lively_peers, True))

        status = owner.status(space.space_id)
        assert "node-peer" not in status.holders_online
        assert "node-owner" in status.holders_online
    finally:
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()


def test_agent_liveliness_is_not_tracked_but_host_is(tmp_path) -> None:
    """Non-host negative control for the host-only liveliness settlement."""

    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner")
    try:
        agent_key = KeySpace().actor_liveliness("agent:owner-lin:machine:worker")
        host_key = KeySpace().actor_liveliness("node-peer")

        owner._on_peer_liveliness(TransportSample(agent_key, b""))
        assert "agent:owner-lin:machine:worker" not in owner._lively_peers

        owner._on_peer_liveliness(TransportSample(host_key, b""))
        assert "node-peer" in owner._lively_peers

        owner._on_peer_liveliness(TransportSample(host_key, b"", kind="delete"))
        assert "node-peer" not in owner._lively_peers
    finally:
        owner.close()


def test_late_announce_after_liveliness_delete_does_not_resurrect_peer(
    tmp_path,
) -> None:
    """An announce applied after the peer's liveliness delete must not make an
    offline peer look online (CI 38513: re-announce raced the delete)."""

    owner, peer, owner_transport, peer_transport = _loopback_pair(tmp_path)
    peer_token = peer_transport.declare_liveliness(
        KeySpace().actor_liveliness("node-peer")
    )
    try:
        space = owner.facade.create_space("holders online late announce")
        owner.facade.invite(space.space_id, "user:peer", "rw")
        owner._announce_all()
        _eventually(lambda: peer.facade.join(space.space_id))
        peer.serve(space.space_id, "memory")
        peer._announce_all()
        _eventually(
            lambda: _assert_equal(
                "node-peer" in owner.status(space.space_id).holders_online, True
            )
        )

        peer_token.close()
        _eventually(lambda: _assert_equal("node-peer" in owner._lively_peers, False))
        # Model the late announce deterministically: the holder hint comes back
        # after the delete settled, exactly what an in-flight announce does.
        with owner._pending_announces_lock:
            owner._holders.setdefault(space.space_id, set()).add("node-peer")

        assert "node-peer" not in owner.status(space.space_id).holders_online
    finally:
        peer_token.close()
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()
