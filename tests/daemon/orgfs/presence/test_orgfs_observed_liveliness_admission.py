"""Exact directory-owner ordering for a lagging shared presence verdict."""

from __future__ import annotations

import json
import time

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import KeySpace, TransportSample


def _settle(runtime: OrgFsRuntime, action: str, sample: TransportSample) -> None:
    runtime._admit_directory_sample(action, sample)
    deadline = time.monotonic() + 2.0
    while runtime.directory_ingress_pending:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    assert runtime.directory_ingress_dropped == 0


def _announce() -> TransportSample:
    return TransportSample(
        KeySpace().orgfs_announce("node-a"),
        json.dumps({
            "schemaVersion": 1,
            "type": "orgfs-announce",
            "node": "node-a",
            "durable": True,
            "spaces": [{"spaceId": "space-a"}],
        }).encode(),
    )


def test_committed_host_put_admits_announce_before_shared_presence_catches_up(
    tmp_path,
) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-b", author="user:reader")
    runtime._supplier_online = lambda _node: False
    runtime._start_directory_ingress()
    try:
        _settle(runtime, "liveliness", TransportSample(
            KeySpace().actor_liveliness("node-a"), b"", kind="put",
        ))
        _settle(runtime, "announce", _announce())
        # No transport is bound, so another announce cannot accidentally
        # rescue the missing admission condition in this exact ordering.
        assert runtime._holder_snapshot("space-a") == ("node-a",)
        assert "node-a" not in runtime._pending_announces
    finally:
        runtime.close()


def test_agent_token_cannot_authorize_a_host_announce(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-b", author="user:reader")
    runtime._supplier_online = lambda _node: False
    runtime._start_directory_ingress()
    try:
        _settle(runtime, "liveliness", TransportSample(
            KeySpace().actor_liveliness("agent:owner:node-a:worker"),
            b"", kind="put",
        ))
        assert runtime._lively_peers == set()
        _settle(runtime, "announce", _announce())
        assert runtime._holder_snapshot("space-a") == ()
        assert "node-a" in runtime._pending_announces
    finally:
        runtime.close()


def test_host_delete_fences_a_later_announce_while_shared_verdict_is_offline(
    tmp_path,
) -> None:
    runtime = OrgFsRuntime(tmp_path, node_id="node-b", author="user:reader")
    runtime._supplier_online = lambda _node: False
    runtime._start_directory_ingress()
    try:
        key = KeySpace().actor_liveliness("node-a")
        _settle(runtime, "liveliness", TransportSample(key, b"", kind="put"))
        _settle(runtime, "liveliness", TransportSample(key, b"", kind="delete"))
        _settle(runtime, "announce", _announce())
        assert runtime._holder_snapshot("space-a") == ()
        assert "node-a" in runtime._pending_announces
    finally:
        runtime.close()
