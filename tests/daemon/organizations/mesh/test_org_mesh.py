from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
from typing import Any

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.org.document import parse_document, serialize_document
from hyprial.daemon.impl.org.mesh import OrgContextMesh
from hyprial.daemon.impl.org.store import OrgContextStore, OrgStoreError
from hyprial.daemon.impl.transport import KeySpace, TransportSample


class _Registration:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Bus:
    def __init__(self) -> None:
        self.subscribers: list[
            tuple[str, Callable[[TransportSample], None], _Registration]
        ] = []
        self.queryables: list[
            tuple[str, Callable[[str], bytes | None], _Registration]
        ] = []
        self.gets: list[tuple[str, float, bool]] = []

    @staticmethod
    def matches(declared: str, requested: str) -> bool:
        if declared == requested:
            return True
        if requested.endswith("*"):
            return declared.startswith(requested[:-1])
        if declared.endswith("*"):
            return requested.startswith(declared[:-1])
        return False


class _Session:
    def __init__(self, bus: _Bus) -> None:
        self.bus = bus

    def put(self, key: str, payload: bytes) -> None:
        sample = TransportSample(key, payload)
        for expression, callback, registration in tuple(self.bus.subscribers):
            if not registration.closed and self.bus.matches(expression, key):
                callback(sample)

    def get(
        self,
        key_expr: str,
        *,
        timeout: float = 3.0,
        errors: list[str] | None = None,
        all_replies: bool = False,
    ) -> list[TransportSample]:
        del errors
        self.bus.gets.append((key_expr, timeout, all_replies))
        replies = []
        for expression, handler, registration in tuple(self.bus.queryables):
            if registration.closed or not self.bus.matches(expression, key_expr):
                continue
            payload = handler(key_expr)
            if payload is not None:
                replies.append(TransportSample(key_expr, payload))
        return replies

    def subscribe(
        self, key_expr: str, callback: Callable[[TransportSample], None]
    ) -> _Registration:
        registration = _Registration()
        self.bus.subscribers.append((key_expr, callback, registration))
        return registration

    def declare_queryable(
        self, key_expr: str, handler: Callable[[str], bytes | None]
    ) -> _Registration:
        registration = _Registration()
        self.bus.queryables.append((key_expr, handler, registration))
        return registration

    def close(self) -> None:
        pass


def _document(version: int, *, title: str | None = None):
    document = parse_document(
        f"""# {title or f"Organization v{version}"}

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
    return document


def _envelope(node: str, document: str) -> bytes:
    return json.dumps(
        {
            "schemaVersion": 1,
            "type": "org-context",
            "sourceNode": node,
            "document": document,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def test_publish_subscribe_and_fetch_roundtrip_only_stage_pending(
    tmp_path: Path,
) -> None:
    bus = _Bus()
    source_store = OrgContextStore(tmp_path / "source")
    receiver_store = OrgContextStore(tmp_path / "receiver")
    accepted = _document(3)
    source_store.adopt(accepted)
    source = OrgContextMesh(_Session(bus), source_store, "node-a")
    receiver = OrgContextMesh(_Session(bus), receiver_store, "node-b")
    try:
        assert source.publish_accepted() is True
        assert receiver_store.load_accepted() is None
        (announced,) = receiver_store.list_pending()
        assert announced.version == 3
        assert announced.source == "node-a"

        fetched = receiver.fetch(
            source="node-a", allowed_sources={"node-a"}, timeout=0.1
        )
        assert fetched.response_count == 1
        assert len(fetched.staged) == 1
        assert receiver_store.load_accepted() is None
        assert len(receiver_store.list_pending()) == 1
    finally:
        receiver.close()
        source.close()


def test_zenoh_candidate_admission_does_not_wait_for_pending_file_io(
    tmp_path: Path, monkeypatch
) -> None:
    bus = _Bus()
    source_store = OrgContextStore(tmp_path / "source")
    receiver_store = OrgContextStore(tmp_path / "receiver")
    source_store.adopt(_document(4))
    source = OrgContextMesh(_Session(bus), source_store, "node-source")
    receiver = OrgContextMesh(_Session(bus), receiver_store, "node-receiver")
    entered = threading.Event()
    release = threading.Event()
    original = receiver._receive_candidate

    def blocked_receive(*args: Any, **kwargs: Any):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(receiver, "_receive_candidate", blocked_receive)
    try:
        payload = source._wire_payload()
        assert payload is not None
        started = time.monotonic()
        receiver._admit_announcement(
            TransportSample(KeySpace().org_context("node-source"), payload)
        )
        assert time.monotonic() - started < 0.1
        assert entered.wait(2)
        assert receiver_store.list_pending() == ()

        # The query callback serves its immutable accepted projection without
        # reopening the accepted document while called on a transport thread.
        monkeypatch.setattr(
            source_store,
            "load_accepted",
            lambda: (_ for _ in ()).throw(AssertionError("query reopened the file")),
        )
        assert source._answer_request("ignored") == payload
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while receiver._effects.snapshot().outstanding and time.monotonic() < deadline:
            time.sleep(0.005)
        receiver.close()
        source.close()

    assert len(receiver_store.list_pending()) == 1


def test_org_context_authority_restart_does_not_repeat_admitted_stage(
    tmp_path: Path,
) -> None:
    candidate_store = OrgContextStore(tmp_path / "candidate")
    candidate_mesh = OrgContextMesh(
        _Session(_Bus()), candidate_store, "node-local"
    )
    document = _document(9)
    envelope = json.dumps(
        {
            "schemaVersion": 1,
            "type": "org-context",
            "sourceNode": "node-remote",
            "document": serialize_document(document),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    entered = threading.Event()
    release = threading.Event()
    original_stage = candidate_store.stage
    calls = 0

    def blocked_stage(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(3)
        return original_stage(*args, **kwargs)

    candidate_store.stage = blocked_stage  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            accepted = pool.submit(
                candidate_mesh.receive,
                envelope,
                expected_source="node-remote",
            )
            assert entered.wait(2)
            assert (
                candidate_mesh._runtime.tell(candidate_mesh._authority, object())
                is AdmissionResult.ACCEPTED
            )
            deadline = time.monotonic() + 3
            while (
                candidate_mesh._runtime.snapshot(candidate_mesh._authority).generation
                == 1
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            assert candidate_mesh._runtime.snapshot(candidate_mesh._authority).generation > 1
            release.set()
            record = accepted.result(timeout=3)
        assert record is not None
        assert calls == 1
    finally:
        release.set()
        candidate_mesh.close()


def test_org_context_total_custody_bounds_deferred_and_rebases_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr('hyprial.daemon.impl.org.mesh._ORG_CONTEXT_CAPACITY', 2)
    store = OrgContextStore(tmp_path / "bounded")
    mesh = OrgContextMesh(_Session(_Bus()), store, "node-local")
    mesh._effects._capacity = 1
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    original = mesh._receive_candidate
    payload = _envelope("node-remote", serialize_document(_document(12)))

    def blocked(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(3)
        return original(*args, **kwargs)

    mesh._receive_candidate = blocked
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(mesh.receive, payload, expected_source="node-remote")
            assert entered.wait(1)
            second = pool.submit(mesh.receive, payload, expected_source="node-remote")
            deadline = time.monotonic() + 2
            while len(mesh._deferred) != 1:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            with pytest.raises(OrgStoreError, match="total custody is full"):
                mesh.receive(payload, expected_source="node-remote")
            generation = mesh._runtime.snapshot(mesh._authority).generation
            assert mesh._runtime.tell(mesh._authority, object()) is AdmissionResult.ACCEPTED
            while mesh._runtime.snapshot(mesh._authority).generation == generation:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            current = mesh._runtime.snapshot(mesh._authority).generation
            assert len(mesh._calls) == 2
            while [item.generation for item in mesh._deferred] != [current]:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            assert [item.generation for item in mesh._deferred] == [current]
            release.set()
            assert first.result(timeout=2) is not None
            assert second.result(timeout=2) is not None
        assert calls == 2
    finally:
        release.set()
        mesh.close()


def test_invalid_schema_and_non_monotonic_versions_are_logged_not_staged(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []
    store = OrgContextStore(tmp_path)
    accepted = _document(2)
    store.adopt(accepted)
    mesh = OrgContextMesh(
        _Session(_Bus()),
        store,
        "receiver",
        logger=lambda level, event, **fields: events.append((level, event, fields)),
    )
    try:
        valid_v3 = serialize_document(_document(3))
        invalid_schema = valid_v3.replace("lines: []", "lines: wrong", 1)
        assert mesh.receive(_envelope("bad-schema", invalid_schema)) is None

        assert (
            mesh.receive(_envelope("old-node", serialize_document(_document(2)))) is None
        )

        assert store.list_pending() == ()
        assert store.load_accepted() == accepted
        rejected = [
            fields["reason"]
            for _level, event, fields in events
            if event == "org.context.rejected"
        ]
        assert rejected == ["schema-invalid", "non-monotonic"]
        assert all(
            level == "warn"
            for level, event, _fields in events
            if event.endswith("rejected")
        )
    finally:
        mesh.close()


def test_request_endpoint_answers_only_accepted_never_pending(tmp_path: Path) -> None:
    bus = _Bus()
    source_store = OrgContextStore(tmp_path / "source")
    source_store.adopt(_document(1))
    source_store.stage(_document(5), source="other-node")
    receiver_store = OrgContextStore(tmp_path / "receiver")
    source = OrgContextMesh(_Session(bus), source_store, "node-a")
    receiver = OrgContextMesh(_Session(bus), receiver_store, "node-b")
    try:
        result = receiver.fetch(source="node-a", allowed_sources={"node-a"})

        assert result.response_count == 1
        (candidate,) = receiver_store.list_pending()
        assert candidate.version == 1
        assert candidate.source == "node-a"
        assert receiver_store.load_accepted() is None
    finally:
        receiver.close()
        source.close()


def test_request_endpoint_with_only_pending_returns_no_response(tmp_path: Path) -> None:
    bus = _Bus()
    source_store = OrgContextStore(tmp_path / "source")
    source_store.stage(_document(5), source="other-node")
    receiver_store = OrgContextStore(tmp_path / "receiver")
    source = OrgContextMesh(_Session(bus), source_store, "node-a")
    receiver = OrgContextMesh(_Session(bus), receiver_store, "node-b")
    try:
        result = receiver.fetch(source="node-a", allowed_sources={"node-a"})

        assert result.response_count == 0
        assert result.staged == ()
        assert receiver_store.list_pending() == ()
    finally:
        receiver.close()
        source.close()


def test_fetch_all_targets_uses_one_unconsolidated_broadcast_query(
    tmp_path: Path,
) -> None:
    bus = _Bus()
    source_a_store = OrgContextStore(tmp_path / "source-a")
    source_b_store = OrgContextStore(tmp_path / "source-b")
    receiver_store = OrgContextStore(tmp_path / "receiver")
    source_a_store.adopt(_document(3, title="Source A"))
    source_b_store.adopt(_document(4, title="Source B"))
    source_a = OrgContextMesh(_Session(bus), source_a_store, "node-a")
    source_b = OrgContextMesh(_Session(bus), source_b_store, "node-b")
    receiver = OrgContextMesh(_Session(bus), receiver_store, "node-c")
    try:
        result = receiver.fetch(
            allowed_sources={"node-a", "node-b"}, timeout=0.25
        )

        assert result.requested_sources == ("node-a", "node-b")
        assert result.response_count == 2
        assert {record.source for record in result.staged} == {"node-a", "node-b"}
        assert bus.gets == [("hyprial/v1/org/request/*", 0.25, True)]
        assert receiver_store.load_accepted() is None
    finally:
        receiver.close()
        source_b.close()
        source_a.close()
