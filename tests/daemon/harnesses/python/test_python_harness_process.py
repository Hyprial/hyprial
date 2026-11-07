"""Parent-side Jev admission, result, and JSONL log tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.operations.top import read_worker_turn_stats
from hyprial.daemon.impl.harnesses.python_worker import PythonHarnessProcess
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel


def _environment(state_dir: Path | None = None) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "HOME", "LANG", "PYTHONPATH"}
    }
    environment["HYPRIAL_SOURCE_COMMIT"] = "3137ed2ad92e5bb2c4369661d660815b63ae787b"
    if state_dir is not None:
        environment["HARNESS_STATE_DIR"] = str(state_dir)
        # The worker reads TYPESAFE_API_KEY from ~/.config/typesafe/env when the
        # environment has none.  An inherited real HOME would hand these tests
        # the machine owner's key -- and a real call to the service.
        home = state_dir.parent / "isolated-home"
        home.mkdir(parents=True, exist_ok=True)
        environment["HOME"] = str(home)
    return environment


def _process(tmp_path: Path, *, on_turn_completed=None) -> PythonHarnessProcess:  # type: ignore[no-untyped-def]
    spec = HarnessLaunchSpec(
        "jev",
        "route-chooser",
        True,
        cwd=str(Path.cwd()),
        command=(
            sys.executable,
            "-m",
            "hyprial.daemon.impl.harnesses.python_worker._python_worker",
            "--kind",
            "jev",
        ),
    )
    return PythonHarnessProcess(
        spec,
        env=_environment(tmp_path / "state"),
        on_turn_completed=on_turn_completed,
    )


def test_python_worker_skips_a_consumed_delivery_before_writing_the_call(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    recipient = "agent:alice:mac:route-chooser"
    item = HarnessDelivery(
        "python-consumed",
        "conversation",
        "sender",
        recipient,
        '{"state":{"secret":"unique skipped python body"},"questions":{}}',
    )
    inbox = InboxService(state_dir / "inbox.sqlite3", MemoryDeliveryTransport())
    assert inbox.receive(
        InboxMessage(
            message_id=item.delivery_id,
            conversation_id=item.conversation_id,
            sender="agent:alice:mac:requester",
            recipient=recipient,
            payload=item.message.encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        ),
        now_ms=1,
    ).acknowledged
    assert inbox.ack(recipient, item.delivery_id).acknowledged
    channel = build_worker_channel(
        actor=recipient,
        session_ref="python-pending-fence",
        hyprial_home=tmp_path / "home",
        state_dir=state_dir,
    )
    spec = HarnessLaunchSpec(
        "jev",
        "route-chooser",
        True,
        cwd=str(Path.cwd()),
        command=(
            sys.executable,
            "-m",
            "hyprial.daemon.impl.harnesses.python_worker._python_worker",
            "--kind",
            "jev",
        ),
    )
    process = PythonHarnessProcess(
        spec,
        env=_environment(state_dir),
        worker_channel=channel,
    )
    writer = process._write_frame  # noqa: SLF001 - pre-write admission probe
    written: list[object] = []
    process._write_frame = written.append  # type: ignore[method-assign]  # noqa: SLF001
    try:
        assert process.enqueue(item)
        assert written == []
        [result] = process.drain_results()
        assert result.status is HarnessResultStatus.INTERRUPTED
        assert result.failure_code == "already-consumed"
        assert process.in_flight == 0
    finally:
        process._write_frame = writer  # type: ignore[method-assign]  # noqa: SLF001
        process.stop()
        inbox.close()

    log_path = state_dir / "logs" / "workers" / "jev-route-chooser.jsonl"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    skipped = [line for line in records if line["event"] == "worker.turn.skipped"]
    assert [(line["deliveryId"], line["reason"]) for line in skipped] == [
        (item.delivery_id, "already-consumed")
    ]
    assert not any(line["event"] == "worker.turn.failed" for line in records)
    assert "unique skipped python body" not in log_path.read_text()


def test_python_worker_prompts_a_system_notice_without_an_inbox_row(
    tmp_path: Path, monkeypatch,
) -> None:
    state_dir = tmp_path / "state"
    process = _process(tmp_path)
    process.worker_channel = build_worker_channel(
        actor="agent:alice:mac:route-chooser",
        session_ref="python-notice-fence",
        hyprial_home=tmp_path / "home",
        state_dir=state_dir,
    )
    monkeypatch.setattr(
        "hyprial.daemon.impl.inbox.authority.read.delivery_pending",
        lambda *_args, **_kwargs: pytest.fail("notice consulted inbox projection"),
    )
    writer = process._write_frame  # noqa: SLF001 - pre-write admission probe
    written: list[object] = []
    process._write_frame = written.append  # type: ignore[method-assign]  # noqa: SLF001
    notice = HarnessDelivery(
        "python-notice",
        "conversation",
        "system:hyprial:remote-node",
        "agent:alice:mac:route-chooser",
        '{"message":"delivery failed","systemNotice":true}',
        notice=True,
    )
    try:
        assert process.enqueue(notice)
        assert written == [
            {
                "v": 1,
                "op": "call",
                "id": notice.delivery_id,
                "payload": {"message": "delivery failed", "systemNotice": True},
            }
        ]
    finally:
        process._write_frame = writer  # type: ignore[method-assign]  # noqa: SLF001
        process.stop()


def test_python_worker_warns_once_per_projection_failure_kind(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    process = _process(tmp_path)
    process.worker_channel = build_worker_channel(
        actor="agent:alice:mac:route-chooser",
        session_ref="python-projection-warning-fence",
        hyprial_home=tmp_path / "home",
        state_dir=state_dir,
    )
    writer = process._write_frame  # noqa: SLF001 - pre-write admission probe
    written: list[dict] = []
    process._write_frame = written.append  # type: ignore[method-assign]  # noqa: SLF001
    try:
        for index in range(2):
            assert process.enqueue(
                HarnessDelivery(
                    f"python-unknown-{index}",
                    "conversation",
                    "sender",
                    "agent:alice:mac:route-chooser",
                    "{}",
                )
            )
    finally:
        process._write_frame = writer  # type: ignore[method-assign]  # noqa: SLF001
        process.stop()

    log_path = state_dir / "logs" / "workers" / "jev-route-chooser.jsonl"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    warnings = [
        item
        for item in records
        if item["event"] == "worker.delivery_projection_unreadable"
    ]
    assert [(item["level"], item["failureKind"]) for item in warnings] == [
        ("warn", "FileNotFoundError")
    ]
    # UNKNOWN (an unreadable inbox) runs the turn: every call frame is written.
    assert [frame.get("id") for frame in written if frame.get("op") == "call"] == [
        "python-unknown-0",
        "python-unknown-1",
    ]


def _string_values(value: object):
    """Yield decoded string values so redaction is checked after JSON parsing."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _string_values(nested)


def test_parent_rejects_the_eleventh_reserved_call(tmp_path: Path) -> None:
    process = _process(tmp_path)
    writer = process._write_frame  # noqa: SLF001 - admission-only control
    process._write_frame = lambda _frame: None  # noqa: SLF001
    try:
        def delivery(index: int) -> HarnessDelivery:
            return HarnessDelivery(
                f"id-{index}", "conversation", "sender", "route-chooser", "{}"
            )

        assert all(process.enqueue(delivery(index)) for index in range(10))
        assert process.in_flight == 10
        assert process.enqueue(delivery(10)) is False
        assert process.in_flight == process.max_in_flight == 10
    finally:
        process._write_frame = writer  # noqa: SLF001
        process.stop()


def test_bounded_result_drain_keeps_unclaimed_worker_results(tmp_path: Path) -> None:
    observed = []
    process = _process(
        tmp_path, on_turn_completed=lambda delivery, result, **timing: observed.append(delivery.delivery_id)
    )
    try:
        deadline = time.monotonic() + 2
        for index in range(2):
            assert process.enqueue(HarnessDelivery(
                f"bounded-{index}", "conversation", "sender", "route-chooser",
                json.dumps({"state": {}, "questions": {}}),
            ))
            # The worker executes calls concurrently. Establish result
            # arrival order without consuming either result before the test.
            while process._results.qsize() != index + 1:
                assert time.monotonic() < deadline
                time.sleep(0.005)
        with pytest.raises(ValueError):
            process.drain_results(-1)
        assert process.drain_results(0) == ()
        assert observed == []
        first = process.drain_results(1)
        assert [result.delivery_id for result in first] == ["bounded-0"]
        assert observed == ["bounded-0"]
        assert "bounded-1" in process._records
        assert process._results.qsize() == 1
        second = process.drain_results(1)
        assert [result.delivery_id for result in second] == ["bounded-1"]
        assert observed == ["bounded-0", "bounded-1"]
        assert process.drain_results(1) == ()
    finally:
        process.stop()


def test_parent_logs_ready_metric_and_turn_error_without_payload(tmp_path: Path) -> None:
    state = tmp_path / "state"
    process = _process(tmp_path)
    try:
        delivery = HarnessDelivery(
            "id-error", "conversation", "sender", "route-chooser",
            json.dumps(
                {
                    "state": {"secret": "not logged", "业务": "不可记录🔒"},
                    "questions": {},
                }
            ),
        )
        assert process.enqueue(delivery)
        result = ()
        for _ in range(40):
            result = process.drain_results()
            if result:
                break
            time.sleep(0.025)
        assert result and result[0].failure_code == "PROVIDER_INVALID_REQUEST"
        log_path = state / "logs" / "workers" / "jev-route-chooser.jsonl"
        # The reader thread publishes the result before it logs the turn's
        # trailing "send" script.metric (python_worker._handle_terminal puts the
        # result, then calls _log_script_send, whose duration covers the
        # handoff).  Draining the result therefore does not mean that line is
        # written yet; wait for it, bounded, instead of racing it.
        lines: list[dict[str, object]] = []
        deadline = time.monotonic() + 2.0
        while True:
            lines = [json.loads(line) for line in log_path.read_text().splitlines()]
            sends = [
                line for line in lines
                if line.get("event") == "script.metric" and line.get("segment") == "send"
            ]
            if sends or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        events = [line["event"] for line in lines]
        assert "script.ready" in events
        assert "worker.started" in events
        assert "worker.turn.started" in events
        assert "worker.turn.failed" in events
        assert events.count("script.metric") == 3
        # Logger.log currently writes ensure_ascii=False (log/__init__.py:377),
        # so the raw-text assertion is intentional for both credential-shaped
        # ASCII and non-ASCII business content.
        rendered = log_path.read_text()
        assert "not logged" not in rendered
        assert "不可记录🔒" not in rendered
        parsed_values = [
            text
            for line in lines
            for text in _string_values(line)
        ]
        assert all("not logged" not in text for text in parsed_values)
        assert all("不可记录🔒" not in text for text in parsed_values)
    finally:
        process.stop()


def test_parent_reports_completed_turn_to_observer(tmp_path: Path) -> None:
    observed: list[tuple[HarnessDelivery, object, dict[str, object]]] = []

    def observer(delivery, result, **facts):  # type: ignore[no-untyped-def]
        observed.append((delivery, result, facts))

    process = _process(tmp_path, on_turn_completed=observer)
    try:
        delivery = HarnessDelivery(
            "id-observed",
            "conversation",
            "sender",
            "route-chooser",
            json.dumps({"state": {}, "questions": {}}),
        )
        assert process.enqueue(delivery)
        deadline = time.monotonic() + 2
        while not process.drain_results():
            assert time.monotonic() < deadline
            time.sleep(0.025)
        assert len(observed) == 1
        assert observed[0][0] == delivery
        assert observed[0][2]["tool_names"] == ()
        assert observed[0][2]["ended_at_ms"] >= observed[0][2]["started_at_ms"]
    finally:
        process.stop()


def test_missing_worker_generation_marker_distrusts_turn_statistics(tmp_path: Path) -> None:
    log_path = tmp_path / "workers.jsonl"
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-09-20T00:00:00.000Z",
                        "event": "worker.turn.started",
                        "messageId": "m1",
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-09-20T00:00:01.000Z",
                        "event": "worker.turn.completed",
                        "messageId": "m1",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_worker_turn_stats(log_path) is None


def test_missing_typesafe_key_preserves_permanent_error_code(tmp_path: Path) -> None:
    process = _process(tmp_path)
    try:
        delivery = HarnessDelivery(
            "id-auth", "conversation", "sender", "route-chooser",
            json.dumps(
                {
                    "state": {"ticket": "redacted"},
                    "questions": {"urgent": {"type": "noul"}},
                }
            ),
        )
        assert process.enqueue(delivery)
        result = ()
        for _ in range(40):
            result = process.drain_results()
            if result:
                break
            time.sleep(0.025)
        assert result and result[0].failure_code == "PROVIDER_AUTHENTICATION_FAILED"
    finally:
        process.stop()
