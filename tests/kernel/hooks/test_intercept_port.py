"""HookInterceptPort: bounded, fail-open, verbatim — the §4 intercept contract."""

from __future__ import annotations

import threading
import time

import pytest

from hyprial.kernel import HookInterceptPort, InterceptRequest


def _request(
    key: str = "delivery-1",
    *,
    handler: str = "agent:owner:node:handler",
    timeout_ms: int = 50,
) -> InterceptRequest:
    return InterceptRequest(
        key=key,
        owner="agent:owner:node:worker",
        handler=handler,
        timeout_ms=timeout_ms,
        document={"event": "delivery.prepared", "payload": {"deliveryId": key}},
    )


class _Invoker:
    def __init__(self, behavior: object) -> None:
        self.behavior = behavior
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, handler, document, *, timeout_seconds):  # type: ignore[no-untyped-def]
        self.calls.append((handler, dict(document)))
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        if callable(self.behavior):
            return self.behavior(timeout_seconds)
        return self.behavior


def _decide(port: HookInterceptPort, request: InterceptRequest, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while True:
        decision = port.prepare(request)
        if decision.ready:
            return decision
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_nonempty_reply_inside_deadline_is_returned_verbatim() -> None:
    opaque = "  first line\n<opaque>& unchanged  "
    invoker = _Invoker(opaque)
    port = HookInterceptPort(invoke=invoker)
    request = _request(timeout_ms=5_000)

    assert not port.prepare(request).ready
    decision = _decide(port, request)
    assert (decision.text, decision.outcome) == (opaque, "text")
    # The decision is memoized and the handler ran exactly once.
    assert port.prepare(request).text == opaque
    assert len(invoker.calls) == 1
    assert invoker.calls[0][1]["payload"] == {"deliveryId": "delivery-1"}
    assert port.close(2.0)


@pytest.mark.parametrize(
    ("behavior", "outcome"),
    [
        ("", "empty"),
        (None, "empty"),
        (RuntimeError("handler exploded"), "error"),
        (TimeoutError("no reply"), "timeout"),
    ],
)
def test_empty_error_and_timeout_replies_fail_open(
    behavior: object, outcome: str
) -> None:
    logs: list[str] = []
    port = HookInterceptPort(
        invoke=_Invoker(behavior),
        logger=lambda _level, _component, event, **_fields: logs.append(event),
    )
    request = _request(timeout_ms=5_000)

    assert not port.prepare(request).ready
    decision = _decide(port, request)
    assert (decision.text, decision.outcome) == (None, outcome)
    assert f"hooks.intercept_{outcome}" in logs
    assert port.close(2.0)


def test_missed_deadline_fails_open_without_an_outcome() -> None:
    clock = [0.0]
    release = threading.Event()
    port = HookInterceptPort(
        invoke=_Invoker(lambda _timeout: release.wait(2) and "late"),
        monotonic=lambda: clock[0],
    )
    request = _request(timeout_ms=50)
    try:
        assert not port.prepare(request).ready
        assert not port.prepare(request).ready  # still inside the budget
        clock[0] = 1.0
        decision = port.prepare(request)
        assert (decision.ready, decision.text, decision.outcome) == (
            True, None, "timeout",
        )
    finally:
        release.set()
        assert port.close(2.0)


def test_reply_landing_after_the_deadline_is_discarded() -> None:
    clock = [0.0]
    gate = threading.Event()
    done = threading.Event()

    def late(_timeout: float) -> str:
        assert gate.wait(2)
        done.set()
        return "late text"

    port = HookInterceptPort(invoke=_Invoker(late), monotonic=lambda: clock[0])
    request = _request(timeout_ms=50)
    assert not port.prepare(request).ready
    clock[0] = 1.0  # past the deadline; the reply has not landed yet
    gate.set()
    assert done.wait(2)
    time.sleep(0.02)  # let the outcome queue settle after `done`
    decision = port.prepare(request)
    assert (decision.text, decision.outcome) == (None, "timeout")
    assert port.close(2.0)


def test_mark_dispatched_pins_the_fail_open_answer_for_late_polls() -> None:
    release = threading.Event()
    port = HookInterceptPort(
        invoke=_Invoker(lambda _timeout: release.wait(2) and "late"),
        monotonic=lambda: 0.0,
    )
    request = _request(timeout_ms=50)
    try:
        assert not port.prepare(request).ready
        port.mark_dispatched(request.key)
        decision = port.prepare(request)
        assert (decision.ready, decision.text) == (True, None)
        release.set()
        # Even after the reply lands, a dispatched key repeats its answer.
        time.sleep(0.05)
        assert port.prepare(request).text is None
    finally:
        release.set()
        assert port.close(2.0)


def test_decided_text_survives_mark_dispatched_for_retries() -> None:
    invoker = _Invoker("context")
    port = HookInterceptPort(invoke=invoker)
    request = _request(timeout_ms=5_000)
    assert not port.prepare(request).ready
    assert _decide(port, request).text == "context"
    port.mark_dispatched(request.key)
    # A retry of the same delivery re-reads the SAME decision; the handler
    # must not run a second time for one delivery id.
    assert port.prepare(request).text == "context"
    assert len(invoker.calls) == 1
    assert port.close(2.0)


def test_forget_and_forget_missing_release_state() -> None:
    invoker = _Invoker("context")
    port = HookInterceptPort(invoke=invoker)
    first, second = _request("delivery-1"), _request("delivery-2")
    assert not port.prepare(first).ready
    assert not port.prepare(second).ready
    port.forget("delivery-1")
    port.forget_missing(first.owner, frozenset({"delivery-2"}))
    assert port._pending == {"delivery-2": port._pending["delivery-2"]}  # noqa: SLF001
    port.forget("delivery-2")
    assert port._pending == {}  # noqa: SLF001
    assert port.close(2.0)


def test_full_invocation_pool_fails_open() -> None:
    release = threading.Event()
    port = HookInterceptPort(
        invoke=_Invoker(lambda _timeout: release.wait(2) and "slow"),
        max_concurrent=1,
    )
    try:
        assert not port.prepare(_request("delivery-1", timeout_ms=5_000)).ready
        overflow = _request("delivery-2", timeout_ms=5_000)
        assert not port.prepare(overflow).ready
        decision = _decide(port, overflow)
        assert (decision.text, decision.outcome) == (None, "error")
    finally:
        release.set()
        assert port.close(2.0)


def test_closed_port_fails_open_and_joins_inflight_threads() -> None:
    release = threading.Event()
    port = HookInterceptPort(
        invoke=_Invoker(lambda _timeout: release.wait(2) and "slow")
    )
    assert not port.prepare(_request("delivery-1", timeout_ms=5_000)).ready
    release.set()
    assert port.close(2.0)
    decision = port.prepare(_request("delivery-new"))
    assert (decision.ready, decision.text, decision.outcome) == (
        True, None, "closed",
    )


def test_document_factory_resolves_on_the_invocation_thread() -> None:
    factory_threads: list[int] = []

    def factory() -> dict[str, object]:
        factory_threads.append(threading.get_ident())
        return {"event": "delivery.prepared", "context": {"recentRecaps": []}}

    invoker = _Invoker("context text")
    port = HookInterceptPort(invoke=invoker)
    request = InterceptRequest(
        key="delivery-1",
        owner="agent:owner:node:worker",
        handler="agent:owner:node:handler",
        timeout_ms=5_000,
        document=factory,
    )
    assert not port.prepare(request).ready
    assert _decide(port, request).text == "context text"
    caller = threading.get_ident()
    assert factory_threads and factory_threads[0] != caller
    assert invoker.calls[0][1]["context"] == {"recentRecaps": []}
    assert port.close(2.0)


def test_document_factory_failure_fails_open() -> None:
    def factory() -> dict[str, object]:
        raise RuntimeError("context gathering exploded")

    invoker = _Invoker("never reached")
    port = HookInterceptPort(invoke=invoker)
    request = InterceptRequest(
        key="delivery-1",
        owner="agent:owner:node:worker",
        handler="agent:owner:node:handler",
        timeout_ms=5_000,
        document=factory,
    )
    assert not port.prepare(request).ready
    decision = _decide(port, request)
    assert (decision.text, decision.outcome) == (None, "error")
    assert invoker.calls == []
    assert port.close(2.0)


def test_prepare_rejects_a_nonpositive_timeout() -> None:
    port = HookInterceptPort(invoke=_Invoker("unused"))
    with pytest.raises(ValueError, match="timeout_ms"):
        port.prepare(_request(timeout_ms=0))
    assert port.close(2.0)
