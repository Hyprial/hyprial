"""Characterization (plan slice P1): sends to ``user:`` and ``route:`` targets.

Discussion record section 13.1 rows 1 and 2.  Behaviour after slice UA2:
``message.send`` to ``user:<owner>`` with the owner's user agent on this
node is submitted to the inbox like any other message and recorded
(``tests/daemon/conversations/user_target`` covers the push).  A
``route:<adapter>:<route>`` target is refused (I4b).
"""

from __future__ import annotations

import pytest

from hyprial.kernel import DaemonRequestError
from tests.daemon.acceptance.acceptance_support import send, session
from tests.daemon.conversations.bypass.bypass_support import boot, settle, stored
from tests.support.golden.app import (
    ADAPTER,
    ALPHA,
    OWNER_USER,
    golden_daemon as golden_daemon,
)

ROUTE = f"route:{ADAPTER}:group"


def _conversation_peers(app, actor_params) -> set[str]:
    listed = app.handle("conversations.list", actor_params)
    return {str(head["peer"]) for head in listed["conversations"]}


def test_send_to_user_target_is_queued_recorded_and_pushed(
    golden_daemon, monkeypatch
) -> None:
    """Flipped by UA2: a ``user:`` send is a queued, recorded message.

    R2: no copy is pushed, and the user-delivery transport is not used for
    this owner.
    """

    booted = boot(golden_daemon, monkeypatch, user_agent=True)
    app = booted.app

    answer = send(app, ALPHA, OWNER_USER, "to-user", text="note for the owner")

    [delivery] = answer["deliveries"]
    assert delivery["accepted"] is True
    assert delivery["queued"] is False
    assert (delivery["pushed"], delivery["pushCode"]) == (False, "TARGET_UNRESOLVED")
    assert booted.daemon.user_delivery.requests == []

    assert set(stored(app, str(delivery["messageId"]))) == {"inbox", "sender_copies"}
    settled = settle(booted)
    assert settled.recorded_messages() == {booted.control, delivery["messageId"]}
    assert OWNER_USER in _conversation_peers(app, session(ALPHA))
    assert ALPHA in _conversation_peers(app, {"from": OWNER_USER})


def test_a_route_target_is_refused_and_writes_nothing(golden_daemon, monkeypatch) -> None:
    """I4b: ``route:`` is no send address; nothing is posted, queued or recorded."""

    booted = boot(golden_daemon, monkeypatch)
    app = booted.app

    with pytest.raises(DaemonRequestError) as refused:
        send(app, ALPHA, ROUTE, "to-route", text="note for the group")

    assert refused.value.code == "UNSUPPORTED_TARGET"
    assert refused.value.data == {"target": ROUTE}
    assert booted.daemon.gateway.calls == []
    settled = settle(booted)
    assert settled.recorded_messages() == {booted.control}
    assert ROUTE not in _conversation_peers(app, session(ALPHA))
