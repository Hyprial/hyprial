"""message.send without a conversationId lands in the pair's default conversation.

Plan slice C1.  Every test runs the golden ``DaemonApplication`` booted like
``wiring/services.py`` with links enforced, real ``message.send`` and the
real custody inbox; conversation ids are read back from the stored queue
rows, not only from the response.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.daemon.impl.conversations.ids import (
    default_conversation_id as _id_of_keys,
    endpoint_key,
)
from tests.daemon.acceptance.acceptance_support import NAMES, session
from tests.daemon.conversations.bypass.bypass_support import Booted, boot, stored
from tests.support.golden.app import (
    ADAPTER,
    ALPHA,
    BETA,
    GAMMA,
    OWNER_USER,
    golden_daemon as golden_daemon,
)



def default_conversation_id(a: str, b: str) -> str:
    """The default conversation of two addresses, as message.send derives it."""

    return _id_of_keys(endpoint_key(a), endpoint_key(b))


def _send(
    app: Any,
    sender: str,
    to: tuple[str, ...],
    key: str,
    *,
    conversation: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = session(sender) if sender in NAMES else {"from": sender}
    params |= {
        "to": list(to),
        "summary": f"summary {key}",
        "message": f"body {key}",
        "idempotencyKey": f"key-{key}",
    }
    if conversation is not None:
        params["conversationId"] = conversation
    return app.handle("message.send", params)


def _stored_conversations(app: Any, message_id: str) -> set[str]:
    rows = stored(app, message_id)
    assert rows, f"message {message_id} has no queue row"
    return {str(row["conversation_id"]) for row in rows.values()}


def _stored_pair_default(app: Any, message_id: str) -> str:
    """The default conversation of the sender and recipient the queue stored."""

    rows = stored(app, message_id)
    [pair] = {(str(row["sender"]), str(row["recipient"])) for row in rows.values()}
    return default_conversation_id(*pair)


@pytest.fixture
def booted(golden_daemon, monkeypatch) -> Booted:
    return boot(golden_daemon, monkeypatch)


def test_sends_without_an_id_share_the_pair_default_in_both_directions(
    booted: Booted,
) -> None:
    app = booted.app

    first = _send(app, ALPHA, (BETA,), "ab-1")
    second = _send(app, ALPHA, (BETA,), "ab-2")
    back = _send(app, BETA, (ALPHA,), "ba-1")

    assert first["ok"] and second["ok"] and back["ok"]
    assert len({first["messageId"], second["messageId"], back["messageId"]}) == 3
    conversation = first["conversationId"]
    assert conversation == second["conversationId"] == back["conversationId"]
    assert conversation == default_conversation_id(ALPHA, BETA)
    for answer in (first, second, back):
        [delivery] = answer["deliveries"]
        assert delivery["conversationId"] == conversation
        message_id = str(answer["messageId"])
        assert _stored_conversations(app, message_id) == {conversation}
        assert _stored_pair_default(app, message_id) == conversation


def test_an_explicit_id_is_used_verbatim(booted: Booted) -> None:
    app = booted.app

    answer = _send(app, ALPHA, (BETA,), "explicit", conversation="topic-7")

    assert answer["conversationId"] == "topic-7"
    [delivery] = answer["deliveries"]
    assert delivery["conversationId"] == "topic-7"
    assert _stored_conversations(app, str(answer["messageId"])) == {"topic-7"}


def test_each_target_of_one_send_without_an_id_gets_its_pair_default(
    booted: Booted,
) -> None:
    app = booted.app

    answer = _send(app, ALPHA, (BETA, GAMMA), "fan")

    assert answer["ok"] is True
    to_beta, to_gamma = answer["deliveries"]
    assert (to_beta["target"], to_gamma["target"]) == (BETA, GAMMA)
    assert to_beta["conversationId"] == default_conversation_id(ALPHA, BETA)
    assert to_gamma["conversationId"] == default_conversation_id(ALPHA, GAMMA)
    assert to_beta["conversationId"] != to_gamma["conversationId"]
    # The top-level id of a send with several targets is the first target's.
    assert answer["conversationId"] == to_beta["conversationId"]
    for delivery in (to_beta, to_gamma):
        assert _stored_conversations(app, str(delivery["messageId"])) == {
            delivery["conversationId"]
        }


def test_an_explicit_id_is_shared_by_every_target(booted: Booted) -> None:
    app = booted.app

    answer = _send(app, ALPHA, (BETA, GAMMA), "fan-explicit", conversation="topic-8")

    assert answer["conversationId"] == "topic-8"
    for delivery in answer["deliveries"]:
        assert delivery["conversationId"] == "topic-8"
        assert _stored_conversations(app, str(delivery["messageId"])) == {"topic-8"}


def test_a_replayed_send_without_an_id_names_the_same_conversation(
    booted: Booted,
) -> None:
    app = booted.app

    first = _send(app, ALPHA, (BETA,), "replay")
    again = _send(app, ALPHA, (BETA,), "replay")

    assert again["messageId"] == first["messageId"]
    assert again["conversationId"] == first["conversationId"]
    [delivery] = again["deliveries"]
    assert delivery["conversationId"] == first["conversationId"]
    assert _stored_conversations(app, str(first["messageId"])) == {
        first["conversationId"]
    }


def test_a_user_target_is_handed_its_pair_default(booted: Booted) -> None:
    from tests.support.identity.cell_push import edit_gateway, install_cell, push_cell

    app = booted.app
    alpha = app.agents.require("alpha")
    edit_gateway(app, defaultRoute="dm")
    install_cell(app, push_cell(alpha, {alpha.uri: ("harness", None), "im": ("adapter", ADAPTER)}))

    answer = _send(app, ALPHA, (OWNER_USER,), "external")

    [to_user] = answer["deliveries"]
    assert to_user["conversationId"] == default_conversation_id(ALPHA, OWNER_USER)
    assert answer["conversationId"] == to_user["conversationId"]
    assert to_user["via"] == "cell"


def test_a_send_without_an_id_still_counts_as_opening_for_the_dispatch_gate(
    booted: Booted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every no-id send was a new conversation before C1; the gate keeps that."""

    app = booted.app
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(app, "_is_dispatch_candidate", lambda **_: True)
    monkeypatch.setattr(
        app,
        "_classify_dispatch_send",
        lambda message, *, conversation_is_new: seen.append(
            (message.conversation_id, conversation_is_new)
        ),
    )

    _send(app, ALPHA, (BETA,), "gate-1")
    _send(app, ALPHA, (BETA,), "gate-2")
    _send(app, ALPHA, (BETA,), "gate-3", conversation="topic-9")
    _send(app, ALPHA, (BETA,), "gate-4", conversation="topic-9")

    default = default_conversation_id(ALPHA, BETA)
    assert seen == [
        (default, True),
        (default, True),
        ("topic-9", True),
        ("topic-9", False),
    ]
