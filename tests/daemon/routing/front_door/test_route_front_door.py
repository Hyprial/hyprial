"""I4b (D1, D2): a ``route:<adapter>:<route>`` send target is refused before any side effect.

Decision notes:

- The front door is gone.  ``message.send`` answers ``UNSUPPORTED_TARGET``
  with ``{"target": <the route: string>}`` for any ``route:`` target, whether
  or not the adapter and route exist, and posts nothing to the gateway and
  queues nothing.  The delivery primitive stays reachable only through
  ``push_to_adapter_route`` (see ``test_route_push_primitive``).
- Resources go to the owner's cell push; any other target that carries
  ``resourcePaths`` is ``UNSUPPORTED_TARGET`` too, naming the first such
  target.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from tests.daemon.routing.front_door.support import ADAPTER, Gateway, Inbox, make_app
from tests.support.runtime._owned_applications import owned_applications as owned_applications

ROUTE_TARGETS = [
    f"route:{ADAPTER}:dm",
    f"route:{ADAPTER}:both",
    f"route:{ADAPTER}:missing",
    f"route:{ADAPTER}:nested",
    "route:nobody:dm",
]


def _send(app: DaemonApplication, targets: list[str], **extra: Any) -> dict[str, Any]:
    sender = app.agents.require("worker").uri
    return app.handle("message.send", {
        "from": sender, "to": targets, "summary": "s", "message": "hello",
        "idempotencyKey": "op-1", **extra,
    })


def _refusal(app: DaemonApplication, targets: list[str], **extra: Any) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as raised:
        _send(app, targets, **extra)
    return raised.value


def _nothing_happened(app: DaemonApplication, gateway: Gateway) -> None:
    assert gateway.posted == 0
    inbox = app._inbox
    assert isinstance(inbox, Inbox)
    assert inbox.submitted == []


@pytest.mark.parametrize("target", ROUTE_TARGETS)
def test_a_route_target_is_refused_before_any_side_effect(tmp_path, monkeypatch, target) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)

    refusal = _refusal(app, [target])

    assert refusal.code == "UNSUPPORTED_TARGET"
    assert refusal.data == {"target": target}
    _nothing_happened(app, gateway)


def test_a_route_target_among_agent_targets_refuses_the_whole_send(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)
    own = app.agents.require("worker").uri

    refusal = _refusal(app, [own, f"route:{ADAPTER}:dm"])

    assert refusal.code == "UNSUPPORTED_TARGET"
    assert refusal.data == {"target": f"route:{ADAPTER}:dm"}
    _nothing_happened(app, gateway)


def test_a_route_target_with_a_resource_is_still_unsupported_target(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)
    attachment = tmp_path / "proof.txt"
    attachment.write_text("proof", encoding="utf-8")

    refusal = _refusal(
        app, [f"route:{ADAPTER}:dm"],
        resourcePaths=[{"path": str(attachment), "kind": "file", "mediaType": "text/plain"}],
    )

    assert refusal.code == "UNSUPPORTED_TARGET"
    assert refusal.data == {"target": f"route:{ADAPTER}:dm"}
    _nothing_happened(app, gateway)


def test_a_route_target_with_reply_to_is_refused_the_same_way(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)

    refusal = _refusal(app, [f"route:{ADAPTER}:dm"], replyTo="om_inbound")

    assert refusal.code == "UNSUPPORTED_TARGET"
    assert refusal.data == {"target": f"route:{ADAPTER}:dm"}
    _nothing_happened(app, gateway)


def test_a_retried_route_send_is_refused_again_and_posts_nothing(tmp_path, monkeypatch) -> None:
    gateway = Gateway()
    app = make_app(tmp_path, monkeypatch, gateway)

    for _ in range(2):
        assert _refusal(app, [f"route:{ADAPTER}:dm"]).code == "UNSUPPORTED_TARGET"
    _nothing_happened(app, gateway)
