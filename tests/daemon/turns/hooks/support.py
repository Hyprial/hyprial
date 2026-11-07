"""Shared fakes for the inbox hook-exchange and delivery-intercept tests.

The v1 ``turn-hooks.json`` recap machinery moved to ``biz/hooks/recap``
(H2); what remains here is the invoker mechanism plus the delivery-hook
fixtures the H3 intercept tests drive.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import (
    HarnessDelivery,
)
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.hooks import DaemonHookService, DeliveryHookService
from hyprial.daemon.impl.inbox.contracts.api import (
    AckResult,
    DeliveryLifecycle,
    InboxMessage,
    SubmissionResult,
)
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.kernel import HookBus, HookInterceptPort
from hyprial.kernel.impl.contracts.daemon.daemon_teardown import (
    TURN_HOOK_CLOSE_TIMEOUT_SECONDS,
)

HANDLER = "agent:owner:node:handler"
RECIPIENT = "agent:owner:node:worker"


@pytest.fixture(autouse=True)
def _close_owned_hook_services(monkeypatch, _fd_hygiene):
    """Settle this test's hook owners before the descriptor hygiene check."""
    delivery_services = []
    buses = []
    initialize_delivery = DeliveryHookService.__init__
    initialize_bus = HookBus.__init__

    def tracked_initialize_delivery(service, *args, **kwargs):
        initialize_delivery(service, *args, **kwargs)
        delivery_services.append(service)

    def tracked_initialize_bus(bus, *args, **kwargs):
        initialize_bus(bus, *args, **kwargs)
        buses.append(bus)

    monkeypatch.setattr(DeliveryHookService, "__init__", tracked_initialize_delivery)
    monkeypatch.setattr(HookBus, "__init__", tracked_initialize_bus)
    yield
    for service in reversed(delivery_services):
        assert service.close(timeout=TURN_HOOK_CLOSE_TIMEOUT_SECONDS), (
            "test-owned delivery hook service did not settle before teardown"
        )
    for bus in reversed(buses):
        assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS), (
            "test-owned hook bus did not settle before descriptor teardown"
        )


class _Invoker:
    def __init__(self, behavior: object) -> None:
        self.behavior = behavior
        self.calls: list[dict[str, object]] = []

    def invoke(
        self,
        handler: str,
        event: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> str | None:
        assert handler == HANDLER
        self.calls.append(event)
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        if callable(self.behavior):
            return self.behavior(timeout_seconds)
        return self.behavior if isinstance(self.behavior, str) else None


class _ExchangeInbox:
    def __init__(self, callback: str) -> None:
        self.callback = callback
        self.request: InboxMessage | None = None
        self.reply: InboxMessage | None = None
        self.acked: list[str] = []

    def submit(self, message: InboxMessage, *, now_ms=None) -> SubmissionResult:  # type: ignore[no-untyped-def]
        self.request = message
        self.reply = InboxMessage(
            message_id="reply",
            conversation_id=message.conversation_id,
            sender=message.recipient,
            recipient=self.callback,
            payload=b'{"message":"opaque reply"}',
            intent="reply",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=2,
        )
        return SubmissionResult(message.message_id, accepted=True)

    def pending_messages(self, actor: str) -> tuple[InboxMessage, ...]:
        return (self.reply,) if actor == self.callback and self.reply else ()

    def ack(self, recipient: str, message_id: str) -> AckResult:
        assert recipient == self.callback
        self.acked.append(message_id)
        self.reply = None
        return AckResult(message_id, acknowledged=True)


class _TimeoutInbox:
    def __init__(self, callback: str) -> None:
        self.callback = callback
        self.request: InboxMessage | None = None
        self.pending: list[InboxMessage] = [
            InboxMessage(
                message_id="stale-reply",
                conversation_id="turn-hook:expired",
                sender=HANDLER,
                recipient=callback,
                payload=b'{"message":"late"}',
                intent="reply",
                lifecycle=DeliveryLifecycle.ONLINE_ONLY,
                created_at_ms=1,
            )
        ]
        self.acked: list[tuple[str, str]] = []

    def submit(self, message: InboxMessage, *, now_ms=None) -> SubmissionResult:  # type: ignore[no-untyped-def]
        self.request = message
        return SubmissionResult(message.message_id, accepted=True)

    def pending_messages(self, actor: str) -> tuple[InboxMessage, ...]:
        return tuple(self.pending) if actor == self.callback else ()

    def ack(self, recipient: str, message_id: str) -> AckResult:
        self.acked.append((recipient, message_id))
        self.pending = [item for item in self.pending if item.message_id != message_id]
        return AckResult(message_id, acknowledged=True)


class _TimeoutRequestAckFailureInbox(_TimeoutInbox):
    def __init__(self, callback: str) -> None:
        super().__init__(callback)
        self.pending = []
        self.request_ack_attempted = False

    def ack(self, recipient: str, message_id: str) -> AckResult:
        if recipient == HANDLER:
            self.request_ack_attempted = True
            self.pending.append(
                InboxMessage(
                    message_id="raced-stale-reply",
                    conversation_id="turn-hook:raced-expired",
                    sender=HANDLER,
                    recipient=self.callback,
                    payload=b'{"message":"late"}',
                    intent="reply",
                    lifecycle=DeliveryLifecycle.ONLINE_ONLY,
                    created_at_ms=1,
                )
            )
            raise RuntimeError("request ack failed")
        return super().ack(recipient, message_id)


class _MeshPresence:
    def __init__(
        self, online: tuple[str, ...], *, mailbox: str | None = None
    ) -> None:
        self.online = frozenset(online)
        self.mailbox = mailbox

    def actor_online(self, actor: str) -> bool:
        return actor in self.online

    def online_mailboxes(self) -> tuple[str, ...]:
        return () if self.mailbox is None else (self.mailbox,)


class _Inbox:
    def __init__(self, message: InboxMessage) -> None:
        self.message = message

    def pending_messages(self, actor: str) -> tuple[InboxMessage, ...]:
        return (self.message,) if actor == RECIPIENT else ()


class _ManyInbox:
    def __init__(self, messages: tuple[InboxMessage, ...]) -> None:
        self.messages = messages

    def pending_messages(self, actor: str) -> tuple[InboxMessage, ...]:
        return self.messages if actor == RECIPIENT else ()


class _Harnesses:
    def __init__(self) -> None:
        self.deliveries: list[HarnessDelivery] = []

    def streaming_actors(self) -> tuple[str, ...]:
        return (RECIPIENT,)

    def dispatch(self, actor: str, delivery: HarnessDelivery) -> bool:
        assert actor == RECIPIENT
        self.deliveries.append(delivery)
        return True


class _Transport:
    pass


def _message(
    message_id: str = "delivery-1", message: str = "original body"
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-1",
        sender="agent:owner:node:sender",
        recipient=RECIPIENT,
        payload=json.dumps({"message": message}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _write_hooks_config(
    home: Path,
    *,
    handler: str = HANDLER,
    timeout_ms: int = 50,
    options: dict[str, object] | None = None,
    entries: list[dict[str, object]] | None = None,
) -> Path:
    """Write a hooks.json v2 with one delivery.prepared intercept entry."""

    config = home / "config" / "hooks.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    if entries is None:
        entry: dict[str, object] = {
            "events": ["delivery.prepared"],
            "mode": "intercept",
            "handler": handler,
            "timeoutMs": timeout_ms,
        }
        if options is not None:
            entry["options"] = options
        entries = [entry]
    config.write_text(
        json.dumps({"schemaVersion": 2, "hooks": entries}), encoding="utf-8"
    )
    return config


def _delivery_hooks(
    home: Path,
    invoker,
    *,
    monotonic=time.monotonic,
    port_monotonic=None,
    logger=None,
    consumers=None,
    supports=None,
    intercept_context=None,
    config_wait_seconds: float = 2.0,
    hooks_monotonic=time.monotonic,
):
    """One DeliveryHookService over a private bus reading <home>/config/hooks.json."""

    bus = HookBus()
    hooks = DaemonHookService(
        bus=bus,
        config_path_for_agent=lambda _actor: home / "config" / "hooks.json",
        consumers=dict(consumers) if consumers else {},
        handler_notifier=lambda *_args, **_kwargs: None,
        logger=logger,
        monotonic=hooks_monotonic,
    )
    port = HookInterceptPort(
        invoke=invoker.invoke,
        logger=logger,
        monotonic=port_monotonic if port_monotonic is not None else monotonic,
    )
    return DeliveryHookService(
        hooks=hooks,
        port=port,
        supports_injection=supports,
        intercept_context=intercept_context,
        logger=logger,
        monotonic=monotonic,
        config_wait_seconds=config_wait_seconds,
    )


def _prepare_until_decided(
    service: DeliveryHookService, delivery: HarnessDelivery, timeout: float = 2.0
):
    """Poll prepare_delivery across simulated ticks until it stops holding."""

    deadline = time.monotonic() + timeout
    while True:
        prepared = service.prepare_delivery(delivery)
        if prepared is not None:
            return prepared
        assert time.monotonic() < deadline
        time.sleep(0.005)


def _runtime(
    tmp_path: Path, service: DeliveryHookService | None
) -> tuple[DaemonEventBridge, _Harnesses]:
    harnesses = _Harnesses()
    runtime = DaemonEventBridge(
        state_dir=tmp_path / "daemon-state",
        node_id="node",
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=_Inbox(_message()),  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        delivery_hooks=service,
    )
    return runtime, harnesses


def _dispatch_until_ready(runtime: DaemonEventBridge, timeout: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while runtime._dispatch_harness_deliveries() == 0:  # noqa: SLF001
        assert time.monotonic() < deadline
        time.sleep(0.005)


def _loopback_endpoint() -> str:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"


def _quarantine_xfail(tag: str, detail: str) -> None:
    """Record a quarantined failure where CI keeps it."""

    pytest.xfail(f"{tag} {detail}"[:20_000])


def _cross_node_diagnostics(
    worker: threading.Thread,
    stages: list[str],
    errors: list[BaseException],
    origin,  # type: ignore[no-untyped-def]
    remote,  # type: ignore[no-untyped-def]
    callback: str,
    handler: str,
) -> str:
    """Where a cross-node hook exchange stopped, so a CI-only stall is diagnosable."""

    frames = sys._current_frames()  # noqa: SLF001
    stacks = "".join(
        f"--- thread {thread.name!r}\n"
        + "".join(traceback.format_stack(frames[thread.ident]))
        for thread in threading.enumerate()
        if thread.ident in frames
    )
    return (
        f"stages={stages} worker_alive={worker.is_alive()} "
        f"worker_errors={[repr(error) for error in errors]} "
        f"remote_pending={len(remote.pending_messages(handler))} "
        f"origin_custody={origin.custody_count()} "
        f"origin_pending={len(origin.pending_messages(callback))}\n{stacks}"
    )


# Shared builders and fixture registrations used by the focused modules.
__all__ = [
    'HANDLER',
    'RECIPIENT',
    '_ExchangeInbox',
    '_Harnesses',
    '_Inbox',
    '_Invoker',
    '_ManyInbox',
    '_MeshPresence',
    '_TimeoutInbox',
    '_TimeoutRequestAckFailureInbox',
    '_Transport',
    '_close_owned_hook_services',
    '_cross_node_diagnostics',
    '_delivery_hooks',
    '_dispatch_until_ready',
    '_loopback_endpoint',
    '_message',
    '_prepare_until_decided',
    '_quarantine_xfail',
    '_runtime',
    '_write_hooks_config',
]
