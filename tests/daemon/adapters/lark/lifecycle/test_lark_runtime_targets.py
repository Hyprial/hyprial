from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.lark import (
    HarnessReceipt,
    LarkAdapter,
    LarkInboundMessage,
    LarkStateStore,
)
from hyprial.daemon.impl.adapters.lark.worker import daemon_link as worker


class _InboundLark:
    def __init__(self) -> None:
        self.replies: list[str] = []

    def reply(self, message_id: str, text: str, *, idempotency_key: str) -> str:
        del message_id, idempotency_key
        self.replies.append(text)
        return "om_fake_reply"

    def add_reaction(self, message_id: str, emoji_type: str) -> None:
        del message_id, emoji_type


class _InboundHarness:
    def __init__(self) -> None:
        self.targets: list[str] = []

    def send_request(self, request: Any) -> HarnessReceipt:
        self.targets.append(request.to.actor_id)
        return HarnessReceipt(message_id=f"harness-{len(self.targets)}")


def _inbound(index: int) -> LarkInboundMessage:
    return LarkInboundMessage(
        event_id=f"evt-pin-hot-reload-{index}",
        message_id=f"om-pin-hot-reload-{index}",
        chat_id="oc_dm",
        chat_type="p2p",
        text=f"message {index}",
        sender_id="ou_sender",
        sender_type="user",
    )


def _adapter_with_worker_routes(
    tmp_path: Path, routes: worker._Routes, *, retry_budget: float = 120.0
) -> tuple[LarkAdapter, _InboundLark, _InboundHarness]:
    lark = _InboundLark()
    harness = _InboundHarness()
    adapter = LarkAdapter(
        state=LarkStateStore(tmp_path / "lark-state.sqlite3"),
        lark=lark,  # type: ignore[arg-type]
        harness=harness,  # type: ignore[arg-type]
        routes=routes,
        channel_actor_id="channel:lark:lark-main",
        transient_retry_budget=retry_budget,
    )
    return adapter, lark, harness


def test_worker_reads_runtime_targets_from_daemon_without_desired_state(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_ipc(
        socket_path: Path, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        del socket_path
        calls.append((method, params))
        return {
            "targets": [
                {
                    "targetKind": "agent",
                    "targetUri": "agent:alice:mac:worker",
                    "actor": "agent:alice:mac:worker",
                    "status": "online",
                },
                {
                    "targetKind": "channel_route",
                    "targetUri": "channel:#ops",
                    "agent": "agent:alice:mac:worker",
                    "status": "configured",
                },
            ]
        }

    monkeypatch.setattr(worker, "_ipc", fake_ipc)
    routes = worker._Routes(
        gateway_name="lark-main",
        socket_path=tmp_path / "daemon.sock",
    )

    targets = routes.list_runtime_targets()

    assert calls == [("targets", {"kind": "agent"})]
    assert len(targets) == 1
    assert targets[0].target_uri == "agent:alice:mac:worker"
    assert targets[0].alias == "worker"
    assert targets[0].status == "online"


def test_pinned_actor_asks_the_daemon_pin_index_for_its_own_gateway(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The worker never reads desired-state: the pin comes from the daemon's
    adapter->agent index, keyed by this worker's gateway name, and a pin is
    per adapter -- the conversation id must not matter."""

    calls: list[tuple[str, dict[str, object]]] = []

    def fake_ipc(
        socket_path: Path, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        del socket_path
        calls.append((method, params))
        return {
            "ok": True,
            "pins": {
                "lark-main": "agent:alice:mac:worker",
                "lark-other": "agent:alice:mac:other",
            },
        }

    monkeypatch.setattr(worker, "_ipc", fake_ipc)
    routes = worker._Routes(
        gateway_name="lark-main",
        socket_path=tmp_path / "daemon.sock",
    )

    first = routes.pinned_actor("lark:oc_dm:oc_dm")
    second = routes.pinned_actor("lark:oc_group:oc_group")

    assert calls == [("adapter.pins", {}), ("adapter.pins", {})]
    assert first is not None and first.actor_id == "agent:alice:mac:worker"
    assert second is not None and second.actor_id == "agent:alice:mac:worker"


def test_pinned_actor_is_none_when_this_gateway_has_no_pin(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        worker,
        "_ipc",
        lambda *_args, **_kwargs: {"ok": True, "pins": {"lark-other": "agent:x"}},
    )
    routes = worker._Routes(
        gateway_name="lark-main",
        socket_path=tmp_path / "daemon.sock",
    )

    assert routes.pinned_actor("lark:oc_dm:oc_dm") is None


def test_running_worker_routes_the_next_inbound_to_a_changed_pin(
    monkeypatch: Any, tmp_path: Path
) -> None:
    current = {"actor": "agent:alice:mac:first"}

    def fake_ipc(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"ok": True, "pins": {"lark-main": current["actor"]}}

    monkeypatch.setattr(worker, "_ipc", fake_ipc)
    routes = worker._Routes(
        gateway_name="lark-main",
        socket_path=tmp_path / "daemon.sock",
    )
    adapter, _lark, harness = _adapter_with_worker_routes(tmp_path, routes)

    first = adapter.handle_inbound(_inbound(1))
    current["actor"] = "agent:alice:mac:second"
    second = adapter.handle_inbound(_inbound(2))

    assert first.status == second.status == "forwarded"
    assert harness.targets == [
        "agent:alice:mac:first",
        "agent:alice:mac:second",
    ]


def test_running_worker_treats_successful_unpin_as_no_target_on_next_inbound(
    monkeypatch: Any, tmp_path: Path
) -> None:
    current: dict[str, str] = {"lark-main": "agent:alice:mac:first"}
    query_fails = False
    reports: list[dict[str, object]] = []

    def fake_ipc(*_args: object, **_kwargs: object) -> dict[str, object]:
        if query_fails:
            raise ConnectionRefusedError("daemon temporarily unavailable")
        return {"ok": True, "pins": dict(current)}

    monkeypatch.setattr(worker, "_ipc", fake_ipc)
    routes = worker._Routes(
        gateway_name="lark-main",
        socket_path=tmp_path / "daemon.sock",
        report=reports.append,
    )
    adapter, lark, harness = _adapter_with_worker_routes(
        tmp_path, routes, retry_budget=0.0
    )

    assert adapter.handle_inbound(_inbound(1)).status == "forwarded"
    current.clear()
    outcome = adapter.handle_inbound(_inbound(2))
    query_fails = True
    failed_query = adapter.handle_inbound(_inbound(3))

    assert outcome.status == "no-target"
    assert failed_query.status == "error"
    assert failed_query.ack_error == "TransientHarnessRetryExhausted"
    assert harness.targets == ["agent:alice:mac:first"]
    assert "No agent is bound" in lark.replies[-2]
    assert "ConnectionRefusedError" in lark.replies[-1]
    assert adapter.dead_letters()[-1].reason == "forward-retry-exhausted"
    assert reports == [
        {
            "status": "warning",
            "event": "lark.pin.query_failed",
            "errorType": "ConnectionRefusedError",
            "fallback": "last-successful-unpinned",
        }
    ]


def test_daemon_query_failure_uses_last_successful_pin_and_reports_loudly(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls = 0
    reports: list[dict[str, object]] = []

    def broken_ipc(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "ok": True,
                "pins": {"lark-main": "agent:alice:mac:cached"},
            }
        raise RuntimeError("daemon disconnected from Lark adapter")

    monkeypatch.setattr(worker, "_ipc", broken_ipc)
    routes = worker._Routes(
        gateway_name="lark-main",
        socket_path=tmp_path / "daemon.sock",
        report=reports.append,
    )
    adapter, _lark, harness = _adapter_with_worker_routes(tmp_path, routes)

    assert adapter.handle_inbound(_inbound(1)).status == "forwarded"
    assert adapter.handle_inbound(_inbound(2)).status == "forwarded"
    assert harness.targets == [
        "agent:alice:mac:cached",
        "agent:alice:mac:cached",
    ]
    assert reports == [
        {
            "status": "warning",
            "event": "lark.pin.query_failed",
            "errorType": "RuntimeError",
            "fallback": "last-successful-pin",
            "cachedActor": "agent:alice:mac:cached",
        }
    ]


# Class, not instance: a raised exception instance retains its __traceback__
# and pytest keeps parametrize values on the callspec all session, pinning
# every frame (and fd) the raise touched (card #77).
@pytest.mark.parametrize("failure_type", (NameError, ImportError))
def test_pin_query_does_not_swallow_programming_or_import_errors(
    monkeypatch: Any, tmp_path: Path, failure_type: type[Exception]
) -> None:
    monkeypatch.setattr(
        worker,
        "_ipc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure_type("missing")),
    )
    routes = worker._Routes(
        gateway_name="lark-main",
        socket_path=tmp_path / "daemon.sock",
    )
    adapter, _lark, _harness = _adapter_with_worker_routes(tmp_path, routes)

    with pytest.raises(failure_type, match="missing"):
        adapter.handle_inbound(_inbound(1))


def test_worker_resolution_prefers_exact_target_over_colliding_alias(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        worker,
        "_ipc",
        lambda *_args, **_kwargs: {
            "targets": [
                {
                    "targetKind": "agent",
                    "targetUri": "agent:alice:mac:worker",
                    "actor": "agent:alice:mac:worker",
                    "status": "online",
                },
                {
                    "targetKind": "agent",
                    "targetUri": "agent:bob:linux:worker",
                    "actor": "agent:bob:linux:worker",
                    "status": "online",
                },
            ]
        },
    )
    routes = worker._Routes(
        gateway_name="lark-main",
        socket_path=tmp_path / "daemon.sock",
    )

    exact = routes.resolve_runtime_target("agent:alice:mac:worker")
    alias = routes.resolve_runtime_target("worker")

    assert [item.actor_id for item in exact] == ["agent:alice:mac:worker"]
    assert [item.actor_id for item in alias] == [
        "agent:alice:mac:worker",
        "agent:bob:linux:worker",
    ]


def test_worker_accepts_exact_node_actor_and_sends_its_advertised_target_uri(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        worker,
        "_ipc",
        lambda *_args, **_kwargs: {
            "targets": [
                {
                    "targetKind": "agent",
                    "targetUri": "agent:node-2",
                    "actor": "node-2",
                    "status": "online",
                }
            ]
        },
    )
    routes = worker._Routes(
        gateway_name="lark-main",
        socket_path=tmp_path / "daemon.sock",
    )

    (target,) = routes.resolve_runtime_target("node-2")

    assert target.actor_id == "agent:node-2"
    assert target.actor_key == "node-2"


def test_worker_rejects_unsafe_or_oversized_runtime_target_fields(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        worker,
        "_ipc",
        lambda *_args, **_kwargs: {
            "targets": [
                {
                    "targetKind": "agent",
                    "targetUri": "agent:safe",
                    "actor": "safe",
                    "status": "online",
                },
                {
                    "targetKind": "agent",
                    "targetUri": "agent:forged\nDelivery failed",
                    "actor": "forged",
                    "status": "online",
                },
                {
                    "targetKind": "agent",
                    "targetUri": "agent:" + "x" * 5000,
                    "actor": "large",
                    "status": "online",
                },
            ]
        },
    )
    routes = worker._Routes(
        gateway_name="lark-main",
        socket_path=tmp_path / "daemon.sock",
    )

    targets = routes.list_runtime_targets()

    assert [target.target_uri for target in targets] == ["agent:safe"]
