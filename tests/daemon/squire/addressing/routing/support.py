from __future__ import annotations

import socket
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import AckResult, InboxMessage, SubmissionResult
from hyprial.daemon.impl.adapters.user_delivery.receiver import (
    UserDeliveryRequest,
    UserDeliveryResult,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.runtime.environment import UNIT_USER_ID


_TEST_APPLICATIONS: list[DaemonApplication] = []


@pytest.fixture(autouse=True)
def _close_test_applications():  # type: ignore[no-untyped-def]
    yield
    while _TEST_APPLICATIONS:
        app = _TEST_APPLICATIONS.pop()
        if app._adapters is not None and not callable(getattr(app._adapters, "stop", None)):
            app._adapters = None
        app._close()
        app._close_home_lease()


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request(*, owner: str = "bob") -> UserDeliveryRequest:
    return UserDeliveryRequest(
        message_id="msg-user-1",
        idempotency_key=f"operation-user-1:{owner}",
        owner=owner,
        sender="agent:alice:sender-mac:worker",
        message="please review the release",
        conversation_id=f"user:alice:{owner}",
    )


















class FakeInbox:
    def __init__(self) -> None:
        self.submitted: list[InboxMessage] = []

    def pending_messages(self, _actor: str) -> tuple[InboxMessage, ...]:
        return ()

    def prune_inbox(self):  # type: ignore[no-untyped-def]
        return []

    def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
        self.submitted.append(message)
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, _actor: str, message_id: str) -> AckResult:
        return AckResult(message_id, acknowledged=True)


class FakeHarnessStatus:
    def __init__(self, *, running: bool) -> None:
        self.running = running

    def status(self) -> tuple[dict[str, object], ...]:
        return (
            {
                "runtime": "user-proxy",
                "name": "bob-proxy",
                "running": self.running,
            },
        )










class FakeUserDelivery:
    def __init__(self, result: UserDeliveryResult) -> None:
        self.result = result
        self.requests: list[UserDeliveryRequest] = []

    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        self.requests.append(request)
        return self.result


def _application(tmp_path: Path, delivery: FakeUserDelivery) -> tuple[DaemonApplication, FakeInbox]:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id="sender-mac",
        owner="alice", user_id=UNIT_USER_ID,
    **_deep_module_daemon_dependencies())
    _TEST_APPLICATIONS.append(app)
    inbox = FakeInbox()
    app._inbox = inbox
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    app._user_delivery = delivery
    # resolve-or-reject (Allen b): the sender persona is a registered identity.
    app.handle("agent.create", {"name": "worker"})
    return app, inbox


















class _PendingFakeInbox(FakeInbox):
    def __init__(self, pending: InboxMessage) -> None:
        super().__init__()
        self._pending = pending

    def pending_messages(self, _actor: str) -> tuple[InboxMessage, ...]:
        return (self._pending,)


























# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'FakeHarnessStatus',
    'FakeInbox',
    'FakeUserDelivery',
    '_PendingFakeInbox',
    '_TEST_APPLICATIONS',
    '_application',
    '_close_test_applications',
    '_port',
    '_request',
]
