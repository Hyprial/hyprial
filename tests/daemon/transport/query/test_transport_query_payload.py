from __future__ import annotations

import queue
import socket
import threading
import time

import pytest
import zenoh

from hyprial.daemon.impl.transport import KeySpace, ZenohConfig, ZenohTransport


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def isolated_transport_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _pair() -> tuple[ZenohTransport, ZenohTransport]:
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    observer = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    time.sleep(0.3)
    return owner, observer


def _assert_query_completed(replies, *, within: float = 1.0) -> None:
    started = time.monotonic()
    with pytest.raises(Exception, match="empty and closed"):
        replies.recv()
    assert time.monotonic() - started < within


def test_query_can_reply_from_another_thread_after_direct_callback_returns() -> None:
    """A Query owns its lifetime until drop, not until its callback returns."""

    owner, observer = _pair()
    key = "hyprial/test/query-lifetime/reply"
    handed_off: queue.Queue[object] = queue.Queue()

    def hand_off(query: object) -> None:
        handed_off.put(query)

    registration = owner._session.declare_queryable(
        key,
        zenoh.handlers.Callback(hand_off, indirect=False),
        complete=True,
    )
    try:
        time.sleep(0.3)
        replies = observer._session.get(
            key,
            target=zenoh.QueryTarget.ALL,
            consolidation=zenoh.ConsolidationMode.NONE,
            timeout=2.0,
        )
        query = handed_off.get(timeout=1.0)

        def reply_later() -> None:
            # The direct callback has handed the Query to this test and
            # returned before this independent worker is even started.
            time.sleep(0.1)
            query.reply(key, b"late", encoding="application/octet-stream")
            query.drop()

        worker = threading.Thread(target=reply_later, name="late-query-reply")
        worker.start()
        reply = replies.recv()
        assert reply.ok.payload.to_bytes() == b"late"
        _assert_query_completed(replies)
        worker.join(1.0)
        assert not worker.is_alive()
    finally:
        registration.undeclare()
        observer.close()
        owner.close()


def test_query_dropped_after_direct_callback_returns_completes_without_reply() -> None:
    owner, observer = _pair()
    key = "hyprial/test/query-lifetime/drop"
    handed_off: queue.Queue[object] = queue.Queue()
    registration = owner._session.declare_queryable(
        key,
        zenoh.handlers.Callback(handed_off.put, indirect=False),
        complete=True,
    )
    try:
        time.sleep(0.3)
        replies = observer._session.get(
            key,
            target=zenoh.QueryTarget.ALL,
            consolidation=zenoh.ConsolidationMode.NONE,
            timeout=2.0,
        )
        query = handed_off.get(timeout=1.0)
        worker = threading.Thread(target=query.drop, name="late-query-drop")
        worker.start()
        _assert_query_completed(replies)
        worker.join(1.0)
        assert not worker.is_alive()
    finally:
        registration.undeclare()
        observer.close()
        owner.close()


def test_orgfs_keys_are_canonical_and_reject_unsafe_segments() -> None:
    keys = KeySpace()

    assert keys.orgfs_log("space", "tree", "node.incarnation.lane", 42) == (
        "hyprial/v1/org/fs/space/log/tree/node.incarnation.lane/42"
    )
    assert keys.orgfs_log_writer("space", "tree", "writer") == (
        "hyprial/v1/org/fs/space/log/tree/writer/*"
    )
    assert keys.orgfs_log_any("space") == "hyprial/v1/org/fs/space/log/**"
    assert keys.orgfs_sync("space", "node") == "hyprial/v1/org/fs/space/sync/node"
    assert keys.orgfs_snapshot("space", "tree", "snapshot-1") == (
        "hyprial/v1/org/fs/space/snapshot/tree/snapshot-1"
    )
    assert keys.orgfs_blob("a" * 64) == f"hyprial/v1/org/fs/blob/{'a' * 64}"
    assert keys.orgfs_announce("node") == "hyprial/v1/org/fs/announce/node"

    with pytest.raises(ValueError):
        keys.orgfs_log("space", "tree", "writer/with-slash", 1)
    with pytest.raises(ValueError):
        keys.orgfs_sync("space/with-slash", "node")
    with pytest.raises(ValueError):
        keys.orgfs_log("space", "tree", "writer", -1)


def test_query_delivers_payload_and_all_replies_disables_consolidation() -> None:
    owner, observer = _pair()
    key = KeySpace().orgfs_sync("space", "node")
    seen: list[tuple[str, bytes | None]] = []

    def handler(selector: str, payload: bytes | None):
        seen.append((selector, payload))
        return [(key, str(index).encode("ascii")) for index in range(5)]

    registration = owner.declare_query_handler(key, handler)
    try:
        time.sleep(0.3)
        consolidated = observer.query(key, b'{"request":true}', timeout=2.0)
        assert len(consolidated) == 1

        replies = observer.query(
            key, b'{"request":true}', timeout=2.0, all_replies=True
        )
        assert [int(sample.payload) for sample in replies] == [0, 1, 2, 3, 4]
        assert seen[-1] == (key, b'{"request":true}')
    finally:
        registration.close()
        observer.close()
        owner.close()


@pytest.mark.parametrize("size", [64 * 1024, 1024 * 1024])
def test_query_replies_support_frozen_payload_sizes(size: int) -> None:
    owner, observer = _pair()
    key = KeySpace().orgfs_sync("space", "node")
    response = b"x" * size
    registration = owner.declare_query_handler(
        key, lambda _selector, _payload: [(key, response)]
    )
    try:
        time.sleep(0.3)
        replies = observer.query(key, b"request", timeout=3.0, all_replies=True)
        assert [sample.payload for sample in replies] == [response]
    finally:
        registration.close()
        observer.close()
        owner.close()


def test_new_handler_accepts_legacy_get_without_a_payload() -> None:
    owner, observer = _pair()
    key = KeySpace().orgfs_sync("space", "node")
    seen: list[bytes | None] = []

    def handler(_selector: str, payload: bytes | None):
        seen.append(payload)
        return [(key, b"legacy")]

    registration = owner.declare_query_handler(key, handler)
    try:
        time.sleep(0.3)
        replies = observer.get(key, timeout=2.0)
        assert [sample.payload for sample in replies] == [b"legacy"]
        assert seen == [None]
    finally:
        registration.close()
        observer.close()
        owner.close()


def test_callback_registrations_do_not_create_one_python_thread_each() -> None:
    """Subscriber/queryable count must not determine the process thread count.

    Zenoh's default indirect Python callback owns one ``pyo3-closure`` thread
    per declaration.  A daemon has declarations for every live actor and route,
    so that default turns ordinary route churn into hundreds of parked threads.
    The transport's bounded dispatch lanes are the thread owners instead.
    """

    owner, observer = _pair()
    baseline = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.endswith("(pyo3-closure)")
    }
    query_worker_baseline = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("hyprial-zenoh-query-")
    }
    registrations = []
    received = [threading.Event() for _ in range(12)]
    try:
        for index, event in enumerate(received):
            key = f"hyprial/test/callback-thread/{index}"
            registrations.append(
                owner.subscribe(key, lambda _sample, ready=event: ready.set())
            )
            registrations.append(
                owner.declare_queryable(
                    key, lambda _selector, value=index: str(value).encode()
                )
            )

        # Declarations propagate asynchronously across the real local Zenoh
        # link; this is setup convergence, not the thread-release deadline.
        time.sleep(0.3)
        for index, event in enumerate(received):
            key = f"hyprial/test/callback-thread/{index}"
            observer.put(key, b"sample")
            assert event.wait(2.0)
            assert [sample.payload for sample in observer.get(key, timeout=2.0)] == [
                str(index).encode()
            ]

        new_callback_threads = [
            thread.name
            for thread in threading.enumerate()
            if thread.ident not in baseline
            and thread.name.endswith("(pyo3-closure)")
        ]
        assert new_callback_threads == []
        query_workers = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("hyprial-zenoh-query-")
            and thread.ident not in query_worker_baseline
        }
        assert len(query_workers) == 4
    finally:
        for registration in reversed(registrations):
            registration.close()
        observer.close()
        owner.close()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        live = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.endswith("(pyo3-closure)")
        }
        if live <= baseline:
            break
        time.sleep(0.01)
    assert live <= baseline

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        live_query_workers = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("hyprial-zenoh-query-")
        }
        if live_query_workers <= query_worker_baseline:
            break
        time.sleep(0.01)
    assert live_query_workers <= query_worker_baseline


def test_slow_query_handler_does_not_stall_subscriber_on_same_session() -> None:
    owner, observer = _pair()
    query_key = "hyprial/test/query-stall"
    sample_key = "hyprial/test/query-stall/sample"
    handler_entered = threading.Event()
    sample_received = threading.Event()

    def slow_handler(_selector: str) -> bytes:
        handler_entered.set()
        time.sleep(2.0)
        return b"done"

    queryable = owner.declare_queryable(query_key, slow_handler)
    subscriber = owner.subscribe(sample_key, lambda _sample: sample_received.set())
    results: list[bytes] = []

    def run_query() -> None:
        results.extend(
            sample.payload for sample in observer.get(query_key, timeout=3.0)
        )

    query_thread = threading.Thread(target=run_query, name="slow-query-client")
    try:
        time.sleep(0.3)
        query_thread.start()
        assert handler_entered.wait(1.0)
        started = time.monotonic()
        observer.put(sample_key, b"during-query")
        assert sample_received.wait(0.5), "subscriber stalled behind slow query handler"
        assert time.monotonic() - started < 0.75
        query_thread.join(3.0)
        assert not query_thread.is_alive()
        assert results == [b"done"]
    finally:
        queryable.close()
        subscriber.close()
        observer.close()
        owner.close()


def test_handler_exception_is_logged_and_queryable_survives(caplog) -> None:
    owner, observer = _pair()
    key = KeySpace().orgfs_sync("space", "node")
    calls = 0

    def handler(_selector: str, _payload: bytes | None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("handler boom")
        return [(key, b"recovered")]

    registration = owner.declare_query_handler(key, handler)
    try:
        time.sleep(0.3)
        with caplog.at_level("ERROR", logger="hyprial.transport.zenoh"):
            assert observer.query(key, b"first", timeout=0.5) == []
        assert "orgfs query handler failed" in caplog.text

        replies = observer.query(key, b"second", timeout=2.0)
        assert [sample.payload for sample in replies] == [b"recovered"]
    finally:
        registration.close()
        observer.close()
        owner.close()
