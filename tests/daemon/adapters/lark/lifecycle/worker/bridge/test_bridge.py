from __future__ import annotations

import json
import socket
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
import hyprial.daemon.impl.adapters.lark.worker.entry as lark_worker
from hyprial.daemon.impl.adapters.lark.worker import bridge as lark_worker_bridge
from hyprial.daemon.impl.adapters.lark import ActorTarget, HarnessDelivery
from hyprial.daemon.impl.adapters.lark.worker.process import LarkWorkerProcess
from hyprial.daemon.impl.adapters.lark.worker.bridge import _handle_reply_bridge_frame, _run_reply_bridge, _serve_reply_bridge
from hyprial.daemon.impl.adapters.lark.worker.sweep import STALE_REBUILD_EXIT_CODE

def test_daemon_worker_control_socket_returns_reply_delivery_receipt(
) -> None:
    daemon_end, worker_end = socket.socketpair()

    class Process:
        @staticmethod
        def poll() -> None:
            return None

    process = object.__new__(LarkWorkerProcess)
    process._process = Process()  # type: ignore[assignment]
    process._control_socket = daemon_end
    process._control_lock = threading.Lock()
    process._control_owner_lock = threading.Lock()
    process._control_owner = None
    process._lock = threading.Lock()
    process._readiness = "online"
    process._logger = None

    def worker() -> None:
        request = json.loads(worker_end.recv(65_536).split(b"\n", 1)[0])
        assert request["replyTo"] == "msg-lark-inbound"
        worker_end.sendall(
            json.dumps(
                {
                    "deliveryId": "msg-reply",
                    "ok": True,
                    "nativeMessageId": "om_reply",
                }
            ).encode()
            + b"\n"
        )

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        accepted = process.deliver_reply(
            HarnessDelivery(
                delivery_id="msg-reply",
                message_id="msg-reply",
                reply_to="msg-lark-inbound",
                from_actor=ActorTarget(
                    actor_id="agent:allen:mac:pi",
                    actor_key="agent:allen:mac:pi",
                    display_name="agent:allen:mac:pi",
                ),
                text="pong",
            )
        )
        assert accepted is True
    finally:
        daemon_end.close()
        worker_end.close()
        thread.join(timeout=1.0)
        if process._control_owner is not None:
            assert process._control_owner.close()


def test_daemon_worker_control_socket_rejects_receipt_without_native_message_id(
) -> None:
    daemon_end, worker_end = socket.socketpair()

    class Process:
        @staticmethod
        def poll() -> None:
            return None

    process = object.__new__(LarkWorkerProcess)
    process._process = Process()  # type: ignore[assignment]
    process._control_socket = daemon_end
    process._control_lock = threading.Lock()
    process._lock = threading.Lock()
    process._readiness = "online"
    process._logger = None

    def worker() -> None:
        worker_end.recv(65_536)
        worker_end.sendall(b'{"deliveryId":"msg-reply","ok":true}\n')

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        accepted = process.deliver_reply(
            HarnessDelivery(
                delivery_id="msg-reply",
                message_id="msg-reply",
                reply_to="msg-lark-inbound",
                from_actor=ActorTarget("actor", "actor", "actor"),
                text="pong",
            )
        )
        assert accepted is False
    finally:
        daemon_end.close()
        worker_end.close()
        thread.join(timeout=1.0)


def test_daemon_worker_control_socket_logs_worker_rejection_reason() -> None:
    """A worker refusal (non-zero platform code, or no native id) must leave
    its reason on the adapter log — this is what makes a silent harness_reply
    diagnosable by delivery id."""

    daemon_end, worker_end = socket.socketpair()

    class Process:
        @staticmethod
        def poll() -> None:
            return None

    class RecordingLogger:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def log(self, level: str, event: str, **fields: object) -> None:
            self.records.append({"level": level, "event": event, **fields})

    logger = RecordingLogger()
    process = object.__new__(LarkWorkerProcess)
    process._process = Process()  # type: ignore[assignment]
    process._control_socket = daemon_end
    process._control_lock = threading.Lock()
    process._lock = threading.Lock()
    process._readiness = "online"
    process._logger = logger  # type: ignore[assignment]

    def worker() -> None:
        worker_end.recv(65_536)
        worker_end.sendall(
            b'{"deliveryId":"msg-reply","ok":false,'
            b'"error":"reply message failed: code=230012 message=content rejected"}\n'
        )

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        accepted = process.deliver_reply(
            HarnessDelivery(
                delivery_id="msg-reply",
                message_id="msg-reply",
                reply_to="msg-lark-inbound",
                from_actor=ActorTarget("actor", "actor", "actor"),
                text="pong",
            )
        )
        assert accepted is False
    finally:
        daemon_end.close()
        worker_end.close()
        thread.join(timeout=1.0)

    assert len(logger.records) == 1
    record = logger.records[0]
    assert record["level"] == "warn"
    assert record["event"] == "lark.reply_bridge.rejected"
    assert record["deliveryId"] == "msg-reply"
    assert "230012" in str(record["reason"])


def test_worker_harness_reject_delivery_logs_reason_instead_of_dropping() -> None:
    """The worker-side ``reject_delivery`` no-op used to discard the reply
    failure reason entirely (``del error``); it must now reach the adapter
    log so a refused send is never silent."""

    class RecordingLogger:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def log(self, level: str, event: str, **fields: object) -> None:
            self.records.append({"level": level, "event": event, **fields})

    logger = RecordingLogger()
    harness = lark_worker._Harness(Path("/tmp/unused.sock"), logger=logger)

    harness.reject_delivery(
        "msg-reply",
        "reply message failed: code=230012 message=content rejected",
        deterministic=False,
    )

    assert len(logger.records) == 1
    record = logger.records[0]
    assert record["level"] == "warn"
    assert record["event"] == "delivery.rejected"
    assert record["messageId"] == "msg-reply"
    assert record["deterministic"] is False
    assert "230012" in str(record["reason"])
    assert "content rejected" in str(record["reason"])


def test_daemon_worker_control_socket_breaks_on_mismatched_receipt() -> None:
    daemon_end, worker_end = socket.socketpair()

    class Process:
        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> int | None:
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> int:
            del timeout
            return 0

    child = Process()
    process = object.__new__(LarkWorkerProcess)
    process._process = child  # type: ignore[assignment]
    process._control_socket = daemon_end
    process._control_lock = threading.Lock()
    process._lock = threading.Lock()
    process._readiness = "online"
    process._logger = None

    def worker() -> None:
        worker_end.recv(65_536)
        worker_end.sendall(
            b'{"deliveryId":"another-reply","ok":true,'
            b'"nativeMessageId":"om_wrong"}\n'
        )

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        accepted = process.deliver_reply(
            HarnessDelivery(
                delivery_id="msg-reply",
                message_id="msg-reply",
                reply_to="msg-lark-inbound",
                from_actor=ActorTarget("actor", "actor", "actor"),
                text="pong",
            )
        )
        assert accepted is False
        assert process._control_socket is None
        assert child.terminated is True
    finally:
        worker_end.close()
        thread.join(timeout=1.0)


def test_worker_reply_bridge_reuses_native_delivery_without_nested_settlement() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[HarnessDelivery, bool]] = []

        def handle_delivery(  # type: ignore[no-untyped-def]
            self, delivery: HarnessDelivery, *, settle: bool = True
        ):
            self.calls.append((delivery, settle))
            return SimpleNamespace(
                status="accepted", native_message_id="om_reply", error=None
            )

    adapter = Adapter()
    response = _handle_reply_bridge_frame(  # type: ignore[arg-type]
        adapter,
        {
            "deliveryId": "msg-reply",
            "messageId": "msg-reply",
            "replyTo": "msg-lark-inbound",
            "fromActor": {
                "actorId": "agent:allen:mac:pi",
                "actorKey": "agent:allen:mac:pi",
                "displayName": "allen/pi",
            },
            "text": "pong",
        },
    )

    assert response == {
        "deliveryId": "msg-reply",
        "ok": True,
        "nativeMessageId": "om_reply",
    }
    [(delivery, settle)] = adapter.calls
    assert settle is False
    assert delivery.reply_to == "msg-lark-inbound"


def test_worker_reply_receipt_precedes_blocked_reaction_cleanup() -> None:
    daemon_end, worker_end = socket.socketpair()
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()

    class Adapter:
        def handle_delivery(  # type: ignore[no-untyped-def]
            self, delivery: HarnessDelivery, *, settle: bool = True
        ):
            assert settle is False
            assert delivery.delivery_id == "msg-reply"
            return SimpleNamespace(
                status="accepted", native_message_id="om-reply", error=None
            )

        def clear_delivery_reaction(self, reply_to: str) -> None:
            assert reply_to == "msg-lark-inbound"
            cleanup_entered.set()
            assert cleanup_release.wait(2.0)

    worker_fd = worker_end.detach()
    thread = threading.Thread(
        target=_serve_reply_bridge,
        args=(Adapter(), worker_fd),  # type: ignore[arg-type]
    )
    thread.start()
    try:
        daemon_end.sendall(
            json.dumps(
                {
                    "deliveryId": "msg-reply",
                    "messageId": "msg-reply",
                    "replyTo": "msg-lark-inbound",
                    "fromActor": {
                        "actorId": "agent:allen:mac:pi",
                        "actorKey": "agent:allen:mac:pi",
                        "displayName": "allen/pi",
                    },
                    "text": "pong",
                },
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        daemon_end.settimeout(0.5)
        response = json.loads(daemon_end.recv(65_536).split(b"\n", 1)[0])
        assert response == {
            "deliveryId": "msg-reply",
            "ok": True,
            "nativeMessageId": "om-reply",
        }
        assert cleanup_entered.wait(0.5)
        assert thread.is_alive()
    finally:
        cleanup_release.set()
        daemon_end.close()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_worker_alarm_bridge_uses_native_reply_without_receipt_leg() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def handle_alarm_delivery(  # type: ignore[no-untyped-def]
            self, correlation_id: str, text: str, *, idempotency_key: str
        ):
            self.calls.append((correlation_id, text, idempotency_key))
            return SimpleNamespace(
                status="accepted", native_message_id="om_alarm", error=None
            )

    adapter = Adapter()
    response = _handle_reply_bridge_frame(  # type: ignore[arg-type]
        adapter,
        {
            "kind": "alarm",
            "deliveryId": "alarm:msg-lark-inbound",
            "correlationId": "msg-lark-inbound",
            "text": "消息投递失败：TARGET_NOT_FOUND。",
            "idempotencyKey": "alarm:msg-lark-inbound:TARGET_NOT_FOUND",
        },
    )

    assert response == {
        "deliveryId": "alarm:msg-lark-inbound",
        "ok": True,
        "nativeMessageId": "om_alarm",
    }
    assert adapter.calls == [
        (
            "msg-lark-inbound",
            "消息投递失败：TARGET_NOT_FOUND。",
            "alarm:msg-lark-inbound:TARGET_NOT_FOUND",
        )
    ]


@pytest.mark.parametrize("defect", [NameError("missing"), ImportError("broken")])
def test_worker_reply_bridge_programming_defect_exits_for_supervised_restart(
    monkeypatch: pytest.MonkeyPatch, defect: Exception
) -> None:
    exits: list[int] = []

    def fail(_adapter: object, _control_fd: int) -> None:
        raise defect

    monkeypatch.setattr(lark_worker_bridge, "_serve_reply_bridge", fail)

    _run_reply_bridge(  # type: ignore[arg-type]
        object(), 12, exit_process=lambda code: exits.append(code)
    )

    assert exits == [STALE_REBUILD_EXIT_CODE]


def test_worker_exits_when_the_daemon_end_of_the_bridge_closes() -> None:
    """The orphan shape (2026-09-20, 2026-09-23): the daemon is gone, the
    bridge thread sees EOF -- and the worker must not live on holding the
    Feishu connection beside the replacement daemon's worker."""

    daemon_end, worker_end = socket.socketpair()
    exits: list[int] = []
    worker_fd = worker_end.detach()
    daemon_end.close()

    _run_reply_bridge(  # type: ignore[arg-type]
        object(), worker_fd, exit_process=lambda code: exits.append(code)
    )

    assert exits == [STALE_REBUILD_EXIT_CODE]


def test_a_real_worker_process_dies_with_its_daemon_link(tmp_path: Path) -> None:
    """End to end in a real process: the SDK owns the main thread (here: a
    long sleep), the bridge thread sees the daemon end close, and the whole
    process exits with the rebuild code instead of living on as an orphan."""

    import subprocess

    daemon_end, worker_end = socket.socketpair()
    script = tmp_path / "child.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys, threading, time
            from hyprial.daemon.impl.adapters.lark.worker.bridge import _run_reply_bridge
            threading.Thread(
                target=_run_reply_bridge, args=(object(), int(sys.argv[1])), daemon=True
            ).start()
            time.sleep(60)  # stands in for stream.start(), which never returns
            """
        ),
        encoding="utf-8",
    )
    child = subprocess.Popen(
        [sys.executable, str(script), str(worker_end.fileno())],
        pass_fds=(worker_end.fileno(),),
    )
    try:
        worker_end.close()
        time.sleep(0.5)
        assert child.poll() is None, "the worker must stay up while its daemon is there"
        daemon_end.close()
        assert child.wait(timeout=10) == lark_worker_bridge.STALE_REBUILD_EXIT_CODE
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_worker_exits_when_the_bridge_socket_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[int] = []

    def reset(_adapter: object, _control_fd: int) -> None:
        raise ConnectionResetError("daemon went away")

    monkeypatch.setattr(lark_worker_bridge, "_serve_reply_bridge", reset)

    _run_reply_bridge(  # type: ignore[arg-type]
        object(), 12, exit_process=lambda code: exits.append(code)
    )

    assert exits == [STALE_REBUILD_EXIT_CODE]
