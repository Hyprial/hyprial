"""Writer-level §7 guards: queueing, drain and close (moved from turn_delivery)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from hyprial.biz.impl.hooks.recap.consumer import RecapHookConsumer
from hyprial.biz.impl.hooks.recap.writer import RecapWrite

from .support import (
    RECIPIENT,
    TURN_HOOK_CLOSE_TIMEOUT_SECONDS,
    build_service,
    observe,
    wait_for_recap_lines,
    write_hooks_config,
)


def _consumer(
    home: Path,
    *,
    queue_size: int = 256,
    logger=None,
    monotonic=time.monotonic,
) -> RecapHookConsumer:
    return RecapHookConsumer(
        home_for_actor=lambda _actor: home,
        logger=logger,
        monotonic=monotonic,
        queue_size=queue_size,
    )


def test_recap_append_never_runs_on_publisher_thread(tmp_path: Path) -> None:
    home = tmp_path / "home"
    write_hooks_config(home)
    _bus, service, consumer = build_service(home)
    appended = threading.Event()
    writer_threads: list[int] = []
    original_append = consumer._writer._append  # noqa: SLF001

    def recording_append(path: Path, value: dict[str, object]) -> None:
        writer_threads.append(threading.get_ident())
        original_append(path, value)
        appended.set()

    consumer._writer._append = recording_append  # type: ignore[method-assign]  # noqa: SLF001
    publisher_thread = threading.get_ident()
    observe(service, "id")

    assert appended.wait(1)
    assert writer_threads and writer_threads[0] != publisher_thread


def test_recap_queue_overflow_is_counted_and_rate_limited(tmp_path: Path) -> None:
    home = tmp_path / "home"
    logs: list[tuple[str, dict[str, object]]] = []
    consumer = _consumer(
        home,
        queue_size=1,
        logger=lambda _level, _component, name, **fields: logs.append((name, fields)),
        monotonic=lambda: 0.0,
    )
    entered = threading.Event()
    release = threading.Event()
    original_append = consumer._writer._append  # noqa: SLF001

    def blocked_append(path: Path, value: dict[str, object]) -> None:
        entered.set()
        release.wait(1)
        original_append(path, value)

    consumer._writer._append = blocked_append  # type: ignore[method-assign]  # noqa: SLF001

    try:
        assert consumer._writer.enqueue(  # noqa: SLF001
            RecapWrite(home, {"index": 0}, RECIPIENT, "first")
        )
        assert entered.wait(1)
        # The blocked writer took item 0 off the queue, so one slot is free:
        # exactly the v1 queue_size=1 overflow shape.
        assert consumer._writer.enqueue(  # noqa: SLF001
            RecapWrite(home, {"index": 1}, RECIPIENT, "delivery-1")
        )
        assert not consumer._writer.enqueue(  # noqa: SLF001
            RecapWrite(home, {"index": 2}, RECIPIENT, "delivery-2")
        )
        assert not consumer._writer.enqueue(  # noqa: SLF001
            RecapWrite(home, {"index": 3}, RECIPIENT, "delivery-3")
        )
        assert consumer.overflow_count == 2
        assert [name for name, _fields in logs].count("hooks.recap_overflow") == 1
    finally:
        release.set()
    assert consumer.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)


def test_close_drains_every_queued_recap_and_stops_writer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    write_hooks_config(home)
    bus, service, consumer = build_service(home)
    for index in range(12):
        observe(service, f"delivery-{index}")

    # Production teardown order: the bus drains its lanes (every observe
    # enqueues) before the consumer drains its writer.
    assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    started = time.monotonic()
    drained = consumer.close(timeout=TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    close_ms = int((time.monotonic() - started) * 1000)
    path = home / "state" / "recaps.jsonl"
    try:
        written = [
            json.loads(line)["deliveryId"] for line in path.read_text().splitlines()
        ]
    except FileNotFoundError:
        written = None
    assert drained and written == [f"delivery-{index}" for index in range(12)]
    assert consumer._writer._writer is None  # noqa: SLF001
    assert close_ms >= 0  # closeMs is recorded for CI evidence dumps


def test_writer_waiting_for_lock_drains_recaps_enqueued_before_close(
    tmp_path: Path,
) -> None:
    """The writer must not honour close while recaps are still queued.

    Deterministic replay of the CI drain stall (run 18319): the writer finds
    the queue empty and waits for the lock; recaps are enqueued and close is
    marked while it waits.  Once it gets the lock it has to write them, not
    return with them still queued.
    """

    home = tmp_path / "home"
    consumer = _consumer(home)
    written: list[object] = []
    consumer._writer._append = lambda _home, value: written.append(value)  # type: ignore[method-assign]  # noqa: SLF001
    waiting = threading.Event()

    class _SignallingWake(threading.Event):
        def clear(self) -> None:
            super().clear()
            waiting.set()  # the writer clears the wake just before taking the lock

    consumer._writer._wake = _SignallingWake()  # noqa: SLF001
    lock = consumer._writer._writer_lock  # noqa: SLF001
    lock.acquire()
    writer = threading.Thread(target=consumer._writer._write_recaps, daemon=True)  # noqa: SLF001
    try:
        consumer._writer._writer = writer  # noqa: SLF001
        writer.start()
        assert waiting.wait(2.0), "writer never reached the empty-queue branch"
        for index in range(3):
            consumer._writer._writes.put_nowait(  # noqa: SLF001
                RecapWrite(home, {"index": index}, RECIPIENT, f"delivery-{index}")
            )
        consumer._writer._closing = True  # noqa: SLF001  # what close() sets under this lock
    finally:
        lock.release()
    writer.join(2.0)

    assert not writer.is_alive()
    assert written == [{"index": 0}, {"index": 1}, {"index": 2}]
    assert consumer._writer._writes.unfinished_tasks == 0  # noqa: SLF001
    assert consumer._writer._writer is None  # noqa: SLF001


def test_close_wakes_idle_recap_writer_without_waiting_for_poll(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    write_hooks_config(home)
    bus, service, consumer = build_service(home)
    observe(service, "id")
    # Wait until the record is on disk: the writer is then inside its idle
    # wait window and unfinished_tasks has settled to zero.
    wait_for_recap_lines(home / "state" / "recaps.jsonl", 1)
    deadline = time.monotonic() + 1.0
    while consumer._writer._writes.unfinished_tasks:  # noqa: SLF001
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert consumer._writer._writer is not None  # noqa: SLF001
    assert consumer._writer._writer.is_alive()  # noqa: SLF001

    started = time.monotonic()
    assert consumer.close(timeout=0.2)
    assert time.monotonic() - started < 0.2
    assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)


def test_recap_enqueue_after_close_is_logged_at_info(tmp_path: Path) -> None:
    home = tmp_path / "home"
    logs: list[tuple[str, str, dict[str, object]]] = []
    consumer = _consumer(
        home,
        logger=lambda level, _component, event, **fields: logs.append(
            (level, event, fields)
        ),
    )
    assert consumer.close(timeout=0.0)

    consumer._writer.enqueue(  # noqa: SLF001
        RecapWrite(home, {"schemaVersion": 1, "event": "turn"}, RECIPIENT, "id")
    )

    assert logs == [
        (
            "info",
            "hooks.recap_after_close",
            {"agent": RECIPIENT, "deliveryId": "id"},
        )
    ]
    assert not (home / "state" / "recaps.jsonl").exists()


def test_close_returns_within_budget_when_recap_writer_is_stuck(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    consumer = _consumer(home)
    entered = threading.Event()
    release = threading.Event()
    original_append = consumer._writer._append  # noqa: SLF001

    # Held until the test releases it (30 s is only a safety net), so the
    # "close honours its timeout" leg never depends on wall-clock load.
    held_seconds = 30.0

    def stuck_append(path: Path, value: dict[str, object]) -> None:
        entered.set()
        release.wait(held_seconds)
        original_append(path, value)

    consumer._writer._append = stuck_append  # type: ignore[method-assign]  # noqa: SLF001
    assert consumer._writer.enqueue(  # noqa: SLF001
        RecapWrite(home, {"index": 0}, RECIPIENT, "id")
    )
    assert entered.wait(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)

    started = time.monotonic()
    try:
        assert consumer.close(timeout=0.05) is False
        # Returned without waiting for the held writer (which blocks up to
        # held_seconds): a close that ignored its timeout would sit here.
        assert time.monotonic() - started < held_seconds / 2
    finally:
        release.set()
    assert consumer.close(timeout=TURN_HOOK_CLOSE_TIMEOUT_SECONDS), (
        "released recap writer did not stop within the teardown budget: "
        f"{consumer._writer._writes.unfinished_tasks} recap(s) still queued"  # noqa: SLF001
    )
