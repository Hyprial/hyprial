"""cli_codex_interactive: delivery scenarios."""

from __future__ import annotations
import threading
import time
from pathlib import Path
import pytest
from hyprial.daemon.impl.harnesses import codex

from .support import (
    batch_route,
    ACTOR,
    THREAD_ID,
    _batch_prompt,
    _legacy_runtime_context as _legacy_runtime_context,
    _hint,
)


def test_codex_interactive_carrier_reuses_streaming_busy_gate(
    tmp_path: Path,
) -> None:
    class FakeServer:
        pid = 123

        def start_turn(self, prompt: str) -> str:
            return "turn-1"

        def read_turn(self, turn_id: str) -> dict[str, object]:
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "done",
                    }
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    message = {
        "messageId": "m-1",
        "conversationId": "c-1",
        "from": "requester",
        "intent": "request",
        "message": "hello",
    }
    replies: list[tuple[str, dict[str, object]]] = []
    fetched = False

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal fetched
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            if params.get("fetched") is True:
                fetched = True
                return {"messages": [message]}
            return {"messages": [] if fetched and replies else [message]}
        if method == "message.ack":
            replies.append((method, params))
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    carrier = codex.CodexInteractiveCarrier(
        FakeServer(),
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=Path("/tmp/codex-pr1"),
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=0.01,
    )
    carrier.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not replies:
        time.sleep(0.01)
    carrier.stop()
    assert replies == [
        (
            "message.ack",
            {"actor": ACTOR, "sessionRef": THREAD_ID, "messageId": "m-1"},
        )
    ]


def test_codex_interactive_carrier_preserves_fifo_for_two_pending_messages(
    tmp_path: Path,
) -> None:
    class FakeServer:
        pid = 123

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def start_turn(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return f"turn-{len(self.prompts)}"

        def read_turn(self, turn_id: str) -> dict[str, object]:
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": f"reply-{turn_id}",
                    }
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    server = FakeServer()
    messages = [
        {
            "messageId": "m-1",
            "conversationId": "c-1",
            "from": "requester",
            "intent": "request",
            "message": "first",
        },
        {
            "messageId": "m-2",
            "conversationId": "c-1",
            "from": "requester",
            "intent": "request",
            "message": "second",
        },
    ]
    replies: list[dict[str, object]] = []

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            replied_ids = {str(item["messageId"]) for item in replies}
            return {
                "messages": [
                    item
                    for item in messages
                    if str(item["messageId"]) not in replied_ids
                ]
            }
        if method == "message.ack":
            replies.append(params)
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    carrier = codex.CodexInteractiveCarrier(
        server,
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=Path("/tmp/codex-pr1"),
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=0.01,
    )
    carrier.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(replies) < 2:
        time.sleep(0.01)
    carrier.stop()
    # One batch turn carries both rows in daemon order; each id still settles
    # on its own hint.
    assert server.prompts == [_batch_prompt(messages)]
    assert [item["messageId"] for item in replies] == ["m-1", "m-2"]


def test_codex_interactive_carrier_acks_event_intent(tmp_path: Path) -> None:
    class FakeServer:
        pid = 123

        def start_turn(self, prompt: str) -> str:
            return "turn-1"

        def read_turn(self, turn_id: str) -> dict[str, object]:
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "ignored for event",
                    }
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    message = {
        "messageId": "m-event",
        "conversationId": "c-1",
        "from": "notifier",
        "intent": "event",
        "message": "deployment finished",
    }
    completions: list[tuple[str, dict[str, object]]] = []
    fetched = False

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal fetched
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            if params.get("fetched") is True:
                fetched = True
                return {"messages": [message]}
            return {"messages": [] if fetched and completions else [message]}
        if method == "message.ack":
            completions.append((method, params))
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    carrier = codex.CodexInteractiveCarrier(
        FakeServer(),
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=Path("/tmp/codex-pr1"),
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=0.01,
    )
    carrier.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not completions:
        time.sleep(0.01)
    carrier.stop()

    assert completions == [
        (
            "message.ack",
            {"actor": ACTOR, "sessionRef": THREAD_ID, "messageId": "m-event"},
        )
    ]


def test_codex_interactive_carrier_reregisters_after_stale_session(
    tmp_path: Path,
) -> None:
    class FakeServer:
        pid = 123

    calls: list[tuple[str, dict[str, object]]] = []
    first_poll = True

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal first_poll
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        calls.append((method, params))
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list" and first_poll:
            first_poll = False
            raise RuntimeError("STALE_SESSION: daemon generation changed")
        if method == "session.register":
            return {"ok": True, "registered": True}
        if method == "message.pending.list":
            return {"messages": []}
        raise AssertionError(method)

    carrier = codex.CodexInteractiveCarrier(
        FakeServer(),
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=Path("/tmp/codex-pr1"),
        command=["codex", "--remote"],
        daemon_request=batch_route(daemon_request),
        state_path=tmp_path / "carrier.sqlite3",
        process_pid=123,
        process_identity="proc-starttime:77",
        poll_seconds=0.01,
    )
    carrier.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not any(
        method == "session.register" for method, _params in calls
    ):
        time.sleep(0.01)
    carrier.stop()
    assert [method for method, _params in calls[:3]] == [
        "session.refresh",
        "message.pending.list",
        "session.register",
    ]
    registration = calls[2][1]
    assert registration["processPid"] == 123
    assert registration["processIdentity"] == "proc-starttime:77"


# M4 hard-gate aliases keep the shared carrier behaviors visible in the Jev
# implementation PR's own diff.  The delegated tests below exercise the real
# carrier, not a source-string or class-name assertion.
def test_m4_codex_interactive_busy_regression(tmp_path: Path) -> None:
    test_codex_interactive_carrier_reuses_streaming_busy_gate(tmp_path)


@pytest.mark.parametrize("phase", ["busy", "fifo", "settlement", "rebuild"])
def test_m4_codex_interactive_barrier_peak_is_one_before_first_turn_releases(
    tmp_path: Path, phase: str
) -> None:
    """A blocked native turn cannot let the second carrier prompt escape."""

    first_entered = threading.Event()
    release_first = threading.Event()
    lock = threading.Lock()
    prompts: list[str] = []
    replies: list[dict[str, object]] = []
    active = 0
    peak = 0
    messages = [
        {
            "messageId": f"m-peak-{phase}-1",
            "conversationId": f"c-peak-{phase}",
            "from": "requester",
            "intent": "request",
            "message": "first",
        },
        {
            "messageId": f"m-peak-{phase}-2",
            "conversationId": f"c-peak-{phase}",
            "from": "requester",
            "intent": "request",
            "message": "second",
        },
    ]

    class FakeServer:
        pid = 123

        def start_turn(self, prompt: str) -> str:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                prompts.append(prompt)
            if prompt == _batch_prompt(messages):
                first_entered.set()
                assert release_first.wait(timeout=2.0)
            return f"turn-{len(prompts)}"

        def read_turn(self, turn_id: str) -> dict[str, object]:
            nonlocal active
            with lock:
                active -= 1
            return {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": turn_id,
                    }
                ],
            }

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    def daemon_request(method: str, params: dict[str, object]) -> dict[str, object]:
        hinted = _hint(method, params)
        if hinted is not None:
            return hinted
        if method in {"session.refresh", "session.heartbeat"}:
            return {"ok": True}
        if method == "message.pending.list":
            replied = {item["messageId"] for item in replies}
            return {"messages": [item for item in messages if item["messageId"] not in replied]}
        if method == "message.ack":
            replies.append(params)
            return {"ok": True, "acknowledged": True}
        raise AssertionError(method)

    carrier = codex.CodexInteractiveCarrier(
        FakeServer(),
        actor=ACTOR,
        session_ref=THREAD_ID,
        cwd=tmp_path,
        command=["codex"],
        daemon_request=batch_route(daemon_request),
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=0.01,
    )
    carrier.start()
    try:
        assert first_entered.wait(timeout=2.0)
        time.sleep(0.05)
        with lock:
            # One batch turn carries both rows; no second prompt may escape.
            assert peak == 1
            assert prompts == [_batch_prompt(messages)]
        release_first.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(replies) < 2:
            time.sleep(0.01)
        assert len(replies) == 2
        assert prompts == [_batch_prompt(messages)]
        assert peak == 1
    finally:
        release_first.set()
        carrier.stop()


def test_m4_codex_interactive_fifo_regression(tmp_path: Path) -> None:
    test_codex_interactive_carrier_preserves_fifo_for_two_pending_messages(tmp_path)
