"""I7a: an unbound sender's copy for this node's owner goes through the user agent cell.

Design ``user-im-binding-removal-tier2`` 8.1 to 8.3 and contract i7-0 v2
sections 3.2 to 3.4 and 4 (owner items A-1, A-7 and A-8).  A message to
``user:<owner>`` is queued and recorded first; the push is an extra copy:

- a sender whose cell has no adapter-role member (an unbound agent, the
  operator) is pushed by the daemon to the targets of this node's user
  agent cell (``cell_push_targets``), ``via: user-agent``;
- an empty user agent cell answers ``pushed: false``, ``TARGET_UNRESOLVED``
  (``pushSkipped: no-user-agent-target``) with no wait;
- a user agent cell that cannot be read answers ``USER_DELIVERY_UNAVAILABLE``
  (``pushSkipped: cell-unavailable``);
- a bound sender is never rescued by the user agent cell: its own cell's
  data decides, even when that data names no chat;
- with nothing queued here (no user agent on this node), the push is the
  only copy: no user agent is a refusal with ``TARGET_UNRESOLVED``;
- a message whose sender is an adapter address is never pushed.

The user-delivery transport (the Zenoh hand-off with its receipt wait) is
replaced by a recording port: a push for the same owner never reaches it.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkSdkError
from hyprial.daemon.impl.adapters.user_delivery import UserDeliveryRequest, UserDeliveryResult
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.acceptance.acceptance_support import send
from tests.daemon.conversations.bypass.bypass_support import boot, settle, stored
from tests.daemon.conversations.user_target.user_target_support import (
    boot_with_user_agent,
    operator_send,
    record_ends,
)
from tests.support.golden.app import ADAPTER, ALPHA, OWNER_USER, golden_daemon as golden_daemon
from tests.support.identity.cell_push import (
    edit_gateway,
    fail_chats,
    install_cell,
    push_cell,
    user_agent_cell,
)

DM, GROUP = "oc_dm_chat", "oc_group_chat"


class RecordingPort:
    """A user-delivery transport that records every push it is handed."""

    def __init__(self) -> None:
        self.requests: list[UserDeliveryRequest] = []

    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        self.requests.append(request)
        return UserDeliveryResult(request.message_id, True, native_message_id="om-zenoh")


def _boot(golden_daemon: Any, monkeypatch: pytest.MonkeyPatch, *, user_agent: bool = True):
    booted = (
        boot_with_user_agent(golden_daemon, monkeypatch) if user_agent
        else boot(golden_daemon, monkeypatch)
    )
    port = RecordingPort()
    booted.app._user_delivery = port
    edit_gateway(booted.app, defaultRoute="dm")
    return booted, port


def _user_agent_binds(app: Any, **cell: Any) -> Any:
    document = user_agent_cell(app, {"im": ADAPTER}, **cell)
    install_cell(app, document)
    return document


def _key(cell: Any, key: str, route: str | None = None) -> str:
    return f"key-{key}:{OWNER_USER}:push:{cell.cell_id}:im:{route or ''}"


def _push_log(booted: Any) -> dict[str, Any]:
    [record] = booted.logs.named("send.user_push")
    return record


def test_an_unbound_agent_is_pushed_through_the_user_agent_cell(golden_daemon, monkeypatch) -> None:
    booted, port = _boot(golden_daemon, monkeypatch)
    app = booted.app
    cell = _user_agent_binds(app)

    [delivery] = send(app, ALPHA, OWNER_USER, "unbound")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["via"]) == (
        True, True, "user-agent",
    )
    assert "pushCode" not in delivery
    assert booted.daemon.gateway.calls == [
        (DM, f"转述自 {ALPHA}：\n\nbody unbound ☃", _key(cell, "unbound")),
    ]
    assert port.requests == []
    assert set(stored(app, delivery["messageId"])) == {"inbox", "sender_copies"}
    record = _push_log(booted)
    assert record["targets"] == [{
        "member": "im", "adapter": ADAPTER, "route": "dm", "state": "sent",
        "code": None, "reason": None,
    }]


def test_the_operator_is_pushed_through_the_user_agent_cell(golden_daemon, monkeypatch) -> None:
    booted, port = _boot(golden_daemon, monkeypatch)
    app = booted.app
    _user_agent_binds(app)

    [delivery] = operator_send(app, OWNER_USER, "operator-note")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["via"]) == (
        True, True, "user-agent",
    )
    assert [call[0] for call in booted.daemon.gateway.calls] == [DM]
    assert port.requests == []


def test_an_operator_on_behalf_of_an_unbound_agent_is_pushed_through_the_user_agent_cell(
    golden_daemon, monkeypatch
) -> None:
    booted, port = _boot(golden_daemon, monkeypatch)
    app = booted.app
    _user_agent_binds(app)

    answer = app.handle("message.send", {
        "from": "user:op", "onBehalfOf": ALPHA, "to": [OWNER_USER], "summary": "obo",
        "message": "on behalf", "idempotencyKey": "key-obo",
    })

    [delivery] = answer["deliveries"]
    assert (delivery["accepted"], delivery["pushed"], delivery["via"]) == (
        True, True, "user-agent",
    )
    assert [call[0] for call in booted.daemon.gateway.calls] == [DM]
    assert port.requests == []


def test_user_connections_of_the_user_agent_cell_choose_the_chats(golden_daemon, monkeypatch) -> None:
    booted, _port = _boot(golden_daemon, monkeypatch)
    app = booted.app
    cell = _user_agent_binds(app, connections=(
        {"from": "im", "on": "user", "to": "im", "route": "group"},
        {"from": "im", "on": "user", "to": "im", "route": "dm"},
    ))

    [delivery] = send(app, ALPHA, OWNER_USER, "two")["deliveries"]

    assert (delivery["pushed"], delivery["via"]) == (True, "user-agent")
    assert [(chat, key) for chat, _text, key in booted.daemon.gateway.calls] == [
        (GROUP, _key(cell, "two", "group")),
        (DM, _key(cell, "two", "dm")),
    ]


def test_an_empty_user_agent_cell_is_not_pushed_and_does_not_wait(golden_daemon, monkeypatch) -> None:
    booted, port = _boot(golden_daemon, monkeypatch)
    app = booted.app
    install_cell(app, user_agent_cell(app, {}))

    [delivery] = send(app, ALPHA, OWNER_USER, "empty")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.TARGET_UNRESOLVED,
    )
    assert "via" not in delivery
    assert booted.daemon.gateway.calls == []
    assert port.requests == []
    assert _push_log(booted)["pushSkipped"] == "no-user-agent-target"
    settle(booted)
    assert record_ends(app, delivery["messageId"]) == {
        ("inbox", ALPHA, OWNER_USER), ("sender_copies", ALPHA, OWNER_USER),
    }


def test_an_unreadable_user_agent_cell_is_unavailable(golden_daemon, monkeypatch) -> None:
    booted, _port = _boot(golden_daemon, monkeypatch)
    app = booted.app
    cell = _user_agent_binds(app)
    runtime = app._cell_runtime
    lookup = runtime.lookup
    monkeypatch.setattr(
        runtime, "lookup", lambda cell_id: None if cell_id == cell.cell_id else lookup(cell_id),
    )

    [delivery] = send(app, ALPHA, OWNER_USER, "unreadable")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.USER_DELIVERY_UNAVAILABLE,
    )
    assert booted.daemon.gateway.calls == []
    assert _push_log(booted)["pushSkipped"] == "cell-unavailable"


def test_an_unknown_outcome_through_the_user_agent_cell_answers_ok(golden_daemon, monkeypatch) -> None:
    booted, _port = _boot(golden_daemon, monkeypatch)
    app = booted.app
    _user_agent_binds(app)
    fail_chats(booted.daemon.gateway, monkeypatch, {DM: LarkSdkError("returned no message id")})

    [delivery] = send(app, ALPHA, OWNER_USER, "unknown")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
    )
    assert len(booted.daemon.gateway.calls) == 1


@pytest.mark.parametrize(
    "members,connections",
    [
        (
            {"w2": ("harness", None), "im": ("adapter", ADAPTER)},
            ({"from": "w2", "on": "user", "to": "im"},),
        ),
        ({"im": ("adapter", None)}, ()),
    ],
    ids=["silenced-by-its-own-cell", "adapter-member-without-datum"],
)
def test_a_bound_sender_is_never_rescued_by_the_user_agent_cell(
    golden_daemon, monkeypatch, members, connections
) -> None:
    booted, _port = _boot(golden_daemon, monkeypatch)
    app = booted.app
    _user_agent_binds(app)
    alpha = app.agents.require("alpha")
    install_cell(app, push_cell(
        alpha, {alpha.uri: ("harness", None), **members},
        entry=alpha.uri, connections=connections,
    ))

    [delivery] = send(app, ALPHA, OWNER_USER, "bound")["deliveries"]

    assert (delivery["pushed"], delivery["pushCode"]) == (False, ipc_errors.TARGET_UNRESOLVED)
    assert booted.daemon.gateway.calls == []


def test_without_a_user_agent_here_an_unbound_send_is_refused(golden_daemon, monkeypatch) -> None:
    booted, port = _boot(golden_daemon, monkeypatch, user_agent=False)
    app = booted.app

    with pytest.raises(DaemonRequestError) as refused:
        send(app, ALPHA, OWNER_USER, "no-user-agent")

    assert refused.value.code == ipc_errors.TARGET_UNRESOLVED
    assert booted.daemon.gateway.calls == []
    assert port.requests == []


@pytest.mark.parametrize(
    "sender",
    [f"adapter:lark:{ADAPTER}", f"adapter:lark:{ADAPTER}:oc_x"],
    ids=["adapter", "adapter-chat"],
)
def test_a_message_from_an_adapter_address_is_never_pushed(
    golden_daemon, monkeypatch, sender
) -> None:
    booted, _port = _boot(golden_daemon, monkeypatch)
    app = booted.app
    _user_agent_binds(app)

    answer = app.handle("message.send", {
        "from": sender, "to": [OWNER_USER], "summary": "loop",
        "message": "loop", "idempotencyKey": "key-loop",
    }, _trusted_message_origin="daemon")

    [delivery] = answer["deliveries"]
    assert delivery.get("pushed") is not True
    assert booted.daemon.gateway.calls == []
    record = _push_log(booted)
    assert (record["code"], record["pushSkipped"]) == (
        ipc_errors.TARGET_UNRESOLVED, "sender-is-adapter",
    )
