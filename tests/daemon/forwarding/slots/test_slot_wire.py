"""The Python wire client speaks the v5 slot protocol end to end.

Runs the protocol-v6 fixture sidecar (pinned to sidecar/hyprial-tailcat's
stdio JSON lines) as a real subprocess: ``map-peer`` names a slot by its
wire id and the answer echoes it, growth binds lazily beyond the ``up``
list, ``unmap-peer`` returns the slot to hold, and the tcp transport
(the Windows scheme) resolves kernel-assigned ports the daemon reads
back from ``state.slots``.  The REAL sidecar binary is held to the same
wire by its Go suites and by ``sidecar/e2e-zenoh-slots/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.forwarding import ForwardingSidecarController
from hyprial.daemon.impl.forwarding.slots import (
    TCP_SLOT_GROW_SPEC,
    slot_socket_paths,
)

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "tests/fixtures/network/fake_forwarding_sidecar.py"
)


def _controller(home: Path, slots: list[str]) -> ForwardingSidecarController:
    up = {
        "v": 6, "requestId": "up-fixture",
        "op": "up",
        "keyFile": str(home / "secrets/tailcat/device-key.json"),
        "membershipStore": str(home / 'membership.json'),
        "owner": "fixture", "deviceId": "device", "controlPort": 7448,
        "addressFile": str(home / "state/tailcat/address"),
        "inboundTarget": 'unix:' + str(home / 'zenoh.sock'),
        "peerPort": 42037,
        "slots": slots,
    }
    return ForwardingSidecarController(
        (sys.executable, str(_FIXTURE)),
        up,
        environ={"FAKE_FORWARD_RECEIPT": str(home / "receipt.jsonl")},
    )


@pytest.fixture
def unix_controller(tmp_path: Path):
    paths = list(slot_socket_paths(tmp_path, 4))
    owner = _controller(tmp_path, paths)
    try:
        yield owner, paths
    finally:
        owner.close()


def test_map_peer_echoes_the_slot_wire_id_and_is_idempotent(unix_controller) -> None:
    owner, paths = unix_controller
    assert owner.map_peer("alice/dev-a", "tc://alice", paths[2]) == paths[2]
    assert owner.map_peer("alice/dev-a", "tc://alice", paths[2]) == paths[2]
    _peers, mappings = owner.status()
    assert mappings == {"alice/dev-a": paths[2]}

    owner.unmap_peer("alice/dev-a")
    _peers, mappings = owner.status()
    assert mappings == {}


def test_growth_binds_a_slot_beyond_the_up_list(unix_controller) -> None:
    owner, paths = unix_controller
    # Same directory, same naming rule -- the lazy-bind growth contract.
    grown = str(Path(paths[0]).parent / "slot-07")
    assert owner.map_peer("late/dev-x", "tc://late", grown) == grown


def test_slot_must_be_a_path_or_tcp_spec(unix_controller) -> None:
    owner, _paths = unix_controller
    from hyprial.daemon.impl.forwarding import ForwardingSidecarError

    with pytest.raises(ForwardingSidecarError):
        owner.map_peer("alice/dev-a", "tc://alice", "relative/slot")


def test_tcp_transport_reports_resolved_ids_and_grows(tmp_path: Path) -> None:
    owner = _controller(tmp_path, [TCP_SLOT_GROW_SPEC] * 3)
    try:
        # The up answer named the kernel-assigned ids, in request order.
        reported = owner.slots_reported
        assert reported is not None and len(reported) == 3
        assert all(
            item.startswith("tcp:127.0.0.1:") and not item.endswith(":0")
            for item in reported
        )

        # Binding names a reported id and the answer echoes it.
        assert (
            owner.map_peer("alice/dev-a", "tc://alice", reported[1]) == reported[1]
        )

        # Growth: the grow spec answers the NEW slot's resolved id, and
        # the next state report includes it.
        resolved = owner.map_peer("late/dev-x", "tc://late", TCP_SLOT_GROW_SPEC)
        assert isinstance(resolved, str)
        assert resolved.startswith("tcp:127.0.0.1:") and resolved not in reported
        owner.status()
        assert owner.slots_reported == reported + (resolved,)
    finally:
        owner.close()
