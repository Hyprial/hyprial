from __future__ import annotations

import socket
import time
from pathlib import Path

from hyprial.daemon.impl.org.document import parse_document
from hyprial.daemon.impl.org.mesh import OrgContextMesh
from hyprial.daemon.impl.org.store import OrgContextStore
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _document(version: int = 7, *, title: str = "Live Zenoh organization"):
    return parse_document(
        f"""# {title}

```yaml
meta:
  version: {version}
  issued_at: 2026-08-16
  publisher: alicesmith
lines: []
members: []
routing: []
norms:
  autonomy: [任务域内自治]
  escalation: 超域必须升级
  etiquette: 先引荐再委派
residents: []
```
"""
    )


def test_real_zenoh_sessions_publish_and_query_on_isolated_port(tmp_path: Path) -> None:
    endpoint = f"tcp/127.0.0.1:{_free_port()}"
    source_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    receiver_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    source_store = OrgContextStore(tmp_path / "source")
    receiver_store = OrgContextStore(tmp_path / "receiver")
    source_store.adopt(_document())
    source = OrgContextMesh(source_transport, source_store, "source-node")
    receiver = OrgContextMesh(receiver_transport, receiver_store, "receiver-node")
    try:
        time.sleep(0.5)
        assert source.publish_accepted() is True
        deadline = time.monotonic() + 3.0
        while not receiver_store.list_pending():
            if time.monotonic() >= deadline:
                raise AssertionError("org-context announcement did not reach peer")
            time.sleep(0.05)

        result = receiver.fetch(
            source="source-node",
            allowed_sources={"source-node"},
            timeout=2.0,
        )

        assert result.response_count == 1
        assert len(result.staged) == 1
        assert receiver_store.list_pending()[0].version == 7
        assert receiver_store.load_accepted() is None
    finally:
        receiver.close()
        source.close()
        receiver_transport.close()
        source_transport.close()


def test_real_zenoh_broadcast_query_collects_each_concrete_node(
    tmp_path: Path,
) -> None:
    endpoint_a = f"tcp/127.0.0.1:{_free_port()}"
    endpoint_b = f"tcp/127.0.0.1:{_free_port()}"
    source_a_transport = ZenohTransport(ZenohConfig(listen=(endpoint_a,)))
    source_b_transport = ZenohTransport(ZenohConfig(listen=(endpoint_b,)))
    receiver_transport = ZenohTransport(
        ZenohConfig(connect=(endpoint_a, endpoint_b))
    )
    source_a_store = OrgContextStore(tmp_path / "source-a")
    source_b_store = OrgContextStore(tmp_path / "source-b")
    receiver_store = OrgContextStore(tmp_path / "receiver")
    source_a_store.adopt(_document(7, title="Source A"))
    source_b_store.adopt(_document(8, title="Source B"))
    source_a = OrgContextMesh(source_a_transport, source_a_store, "source-a")
    source_b = OrgContextMesh(source_b_transport, source_b_store, "source-b")
    receiver = OrgContextMesh(receiver_transport, receiver_store, "receiver")
    try:
        time.sleep(0.5)

        result = receiver.fetch(
            allowed_sources={"source-a", "source-b"},
            timeout=2.0,
        )

        assert result.response_count == 2
        assert {record.source for record in result.staged} == {
            "source-a",
            "source-b",
        }
        assert {record.version for record in receiver_store.list_pending()} == {7, 8}
        assert receiver_store.load_accepted() is None
    finally:
        receiver.close()
        source_b.close()
        source_a.close()
        receiver_transport.close()
        source_b_transport.close()
        source_a_transport.close()
