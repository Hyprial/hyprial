"""UA2b item 10a: two machines of one owner derive one owner conversation.

Two golden daemons of the same owner on different nodes (``node-1`` and
``node-2``), each with the owner's user agent and a composed recorder.  The
owner's end is ``user:op`` on both, so the default conversation of the owner
and an agent is the same whichever machine computes it, and a message
addressed to the other machine's user agent instance lands with the local
``user:op``.
"""

from __future__ import annotations

from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
from hyprial.kernel import canonical_agent_uri
from tests.daemon.acceptance.acceptance_support import attach_cell_orgfs
from tests.daemon.conversations.bypass.bypass_support import stored
from tests.daemon.conversations.user_target.user_target_support import user_agent_id_address
from tests.support.golden.app import (
    ALPHA,
    OWNER,
    OWNER_USER,
    SESSIONS,
    golden_daemon as golden_daemon,
)

DEFAULT_ALPHA_USER = "dc.xy6aor733a6q3lphrp6gaaxmay"
SECOND = "node-2"
SECOND_ALPHA = canonical_agent_uri(OWNER, SECOND, "alpha")


def _machine(golden_daemon, monkeypatch, **options):
    daemon = golden_daemon(user_agent=True, links=False, **options)
    attach_cell_orgfs(daemon.app, monkeypatch)
    assert compose_conversation_recorder(daemon.app, start=False, clock_ms=daemon.clock.ms)
    return daemon.app


def _send(app, sender: str, to: str, key: str) -> dict:
    params = {"from": sender}
    if sender != OWNER_USER:
        params["sessionRef"] = SESSIONS["alpha"]
    return app.handle("message.send", {
        **params, "to": [to], "summary": "s", "message": f"body {key}",
        "idempotencyKey": f"key-{key}",
    })


def test_both_machines_derive_the_same_owner_conversation(golden_daemon, monkeypatch) -> None:
    first = _machine(golden_daemon, monkeypatch)
    second = _machine(golden_daemon, monkeypatch, node=SECOND)
    assert (first.node_id, second.node_id) == ("node-1", SECOND)

    agent_to_owner = _send(first, ALPHA, OWNER_USER, "agent-to-owner")
    owner_to_agent = _send(second, OWNER_USER, ALPHA, "owner-to-agent")

    assert agent_to_owner["conversationId"] == DEFAULT_ALPHA_USER
    assert owner_to_agent["conversationId"] == DEFAULT_ALPHA_USER


def test_the_other_machines_instance_lands_with_the_local_owner(golden_daemon, monkeypatch) -> None:
    first = _machine(golden_daemon, monkeypatch)
    second = _machine(golden_daemon, monkeypatch, node=SECOND)
    direct = _send(second, SECOND_ALPHA, OWNER_USER, "direct")

    via_first = _send(second, SECOND_ALPHA, f"agent:{OWNER}:node-1:hyprial-user", "via-first")
    via_second = _send(first, ALPHA, f"agent:{OWNER}:{SECOND}:hyprial-user", "via-second")

    assert via_first["deliveries"][0]["target"] == OWNER_USER
    assert via_first["conversationId"] == direct["conversationId"]
    assert stored(second, via_first["messageId"])["inbox"]["recipient"] == OWNER_USER
    assert via_second["conversationId"] == DEFAULT_ALPHA_USER
    assert stored(first, via_second["messageId"])["inbox"]["recipient"] == OWNER_USER


def test_each_machines_own_user_agent_id_form_lands_with_the_local_owner(
    golden_daemon, monkeypatch
) -> None:
    """Each machine recognises its own instance by its registry marker; the end stays ``user:op``.

    The golden daemon pins ``uuid4``, so both machines mint the same token
    here; that two homes get different ids is pinned against real registries
    in ``tests/daemon/user_agent/test_user_agent_ensure.py``.
    """

    first = _machine(golden_daemon, monkeypatch)
    second = _machine(golden_daemon, monkeypatch, node=SECOND)
    mine, theirs = user_agent_id_address(first), user_agent_id_address(second)

    via_own_id = _send(first, ALPHA, mine, "via-own-id")
    via_their_id = _send(second, SECOND_ALPHA, theirs, "via-their-id")

    assert via_own_id["deliveries"][0]["target"] == OWNER_USER
    assert via_own_id["conversationId"] == DEFAULT_ALPHA_USER
    assert via_their_id["deliveries"][0]["target"] == OWNER_USER
    assert stored(second, via_their_id["messageId"])["inbox"]["recipient"] == OWNER_USER
