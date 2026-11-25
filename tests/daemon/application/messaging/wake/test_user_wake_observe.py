"""The S5 human-wake gate, observe mode, on the squire/user wake paths (Q6).

Integration line I1.  ``_workflow_deliver_user`` (and ``_deliver_report_to_user``
through it) and both owner watchdogs ask the ``ObserveWakeGate`` about the
message they are about to put in front of a person.  Phase A final integration adds the operator alarm owner DM and
the OrgFS resident-replica owner notice, both daemon notices.  A SYSTEM message logs ``delivery.wake.would_suppress``; every wake
still goes out: observe mode never suppresses.  R2 (owner rulings
2026-11-19): the watchdogs, the operator alarm, the OrgFS notice and the
restart notice no longer reach the user (they are logged), so they are no
longer wakes; a workflow report is still observed before its owning agent
sends it, or before it is logged as unsent.  I7a (contract i7-0 v2
section 4): the user-proxy hand-off and its ``proxy`` observation site are
gone; another owner's copy is posted to the user agent cell, not a wake site.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import hyprial.daemon.impl.application.messaging.delivery.wake as wake_module
from hyprial.daemon.impl.application.messaging.delivery.sends import _UserDeliveryMixin
from hyprial.daemon.impl.delivery import WOULD_SUPPRESS_EVENT
from hyprial.daemon.impl.adapters.user_delivery import UserDeliveryRequest, UserDeliveryResult
from hyprial.kernel import DISPATCH_MESSAGE_PREFIX

OWNER = "op"
NODE = "node-1"
DISPATCH_ID = f"{DISPATCH_MESSAGE_PREFIX}12345678-1234-4234-8234-123456789abc"


@dataclass
class _UserDelivery:
    requests: list[UserDeliveryRequest] = field(default_factory=list)

    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        self.requests.append(request)
        return UserDeliveryResult(request.message_id, True, native_message_id="om-1")


class _App(_UserDeliveryMixin):
    def __init__(self) -> None:
        self.owner = OWNER
        self.node_id = NODE
        self._user_delivery = _UserDelivery()
        self._user_delivery_settled = True
        self.logs: list[tuple[str, str, str, dict[str, Any]]] = []
        self.sent: list[dict[str, Any]] = []
        self._inbox = object()
        self._dispatch_service_actor = f"agent:{OWNER}:{NODE}:dispatch-coordinator"

    def _log(self, level: str, component: str, event: str, **fields: Any) -> None:
        self.logs.append((level, component, event, fields))

    def _managed_worker_running(self, recipient: str, desired: object) -> bool:
        return True

    def handle(self, method: str, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.sent.append(params)
        return {"deliveries": [{"accepted": True, "messageId": "m-sent"}]}

    def notices(self) -> list[dict[str, Any]]:
        """What reached the owner: a send to ``user:`` or a logged notice."""

        logged = [
            fields for _level, _component, event, fields in self.logs
            if event.endswith(".notice") or event == "workflow.user_report.unsent"
        ]
        return [params for params in self.sent if params["to"] == [f"user:{OWNER}"]] + logged

    def would_suppress(self) -> list[dict[str, Any]]:
        return [
            {"level": level, "component": component, **fields}
            for level, component, event, fields in self.logs
            if event == WOULD_SUPPRESS_EVENT
        ]


def test_workflow_user_delivery_observes_a_system_message_and_still_delivers() -> None:
    app = _App()
    assert app._deliver_report_to_user(f"user:{OWNER}", "done", DISPATCH_ID, "pac:graph")
    assert [params["idempotencyKey"] for params in app.notices()] == [
        f"workflow-escalate:{DISPATCH_ID}"
    ]
    assert app.would_suppress() == [
        {
            "level": "info",
            "component": "delivery",
            "messageId": DISPATCH_ID,
            "recipient": f"user:{OWNER}",
            "path": "user.workflow",
            "messageClass": "system",
            "mode": "observe",
        }
    ]


def test_an_interaction_wake_logs_nothing() -> None:
    app = _App()
    assert app._workflow_deliver_user(f"user:{OWNER}", "hi", "m-plain", f"user:{OWNER}")
    assert len(app.notices()) == 1
    assert app.would_suppress() == []


def test_a_failing_gate_never_blocks_a_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(message: object) -> object:
        raise RuntimeError("class accessor down")

    monkeypatch.setattr(wake_module, "message_class_of", broken)
    app = _App()
    assert app._workflow_deliver_user(f"user:{OWNER}", "done", DISPATCH_ID, "pac:graph")
    assert app._quota_watchdog_deliver("k", "t").delivered is True
    assert len(app.notices()) == 2
    assert app.would_suppress() == []


def test_a_failing_gate_is_logged_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(message: object) -> object:
        raise RuntimeError("class accessor down")

    monkeypatch.setattr(wake_module, "message_class_of", broken)
    app = _App()
    assert app._workflow_deliver_user(f"user:{OWNER}", "done", DISPATCH_ID, "pac:graph")
    assert app._quota_watchdog_deliver("k", "t").delivered is True
    failures = [
        (level, component, fields)
        for level, component, event, fields in app.logs
        if event == wake_module.OBSERVE_FAILED_EVENT
    ]
    assert failures == [
        (
            "warn",
            "delivery",
            {"path": "user.workflow", "errorType": "RuntimeError"},
        )
    ]
    assert len(app.notices()) == 2


class _Emitter:
    def emit(self, alarm: Any, *, terminal: bool) -> Any:
        return SimpleNamespace(status="delivered", claimed=True)


def _wait_for_requests(app: _App, count: int) -> None:
    deadline = time.monotonic() + 5.0
    while len(app.notices()) < count:
        assert time.monotonic() < deadline, app.sent
        time.sleep(0.01)


