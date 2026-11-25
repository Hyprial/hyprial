"""UA2: ``message.send`` to ``user:<owner>`` is an ordinary recorded message.

With the owner's user agent on this node the send takes the queue path: the
same submit, message id and conversation ids an agent target gets, with
``user:op`` kept as the envelope's target string.  The owner push (I7a:
through this node's user agent cell) follows best-effort and never fails the
send.

``DEFAULT_ALPHA_USER`` is the default conversation of ALPHA and the owner as
dev derives it today.  Each machine's user agent has its own registry-minted
id; ``FORMER_USER_AGENT_ID`` is the id an older build derived from the owner
for every machine.
"""

from __future__ import annotations

import pytest

from hyprial.kernel import DaemonRequestError, canonical_user_uri, ipc_errors
from hyprial.shell.impl.request_http.config import _actor as http_principal_actor
from tests.daemon.acceptance.acceptance_support import send, session
from tests.daemon.conversations.bypass.bypass_support import boot, settle, stored
from tests.daemon.conversations.user_target.user_target_support import (
    boot_with_user_agent,
    operator_send,
    user_agent_id_address,
    record_ends,
)
from tests.support.golden.app import ADAPTER, ALPHA, OWNER, OWNER_USER, golden_daemon as golden_daemon
from tests.support.identity.cell_push import bind_user_agent

DEFAULT_ALPHA_USER = "dc.xy6aor733a6q3lphrp6gaaxmay"
FORMER_USER_AGENT_ID = "a.vataro664zkhlfnya2ubzymqxq"


def _default_send(app, key: str, *, to: str = OWNER_USER) -> dict:
    return app.handle(
        "message.send",
        {**session(ALPHA), "to": [to], "summary": f"summary {key}",
         "message": f"body {key}", "idempotencyKey": f"key-{key}"},
    )


def test_send_to_own_user_is_queued_recorded_and_pushed(golden_daemon, monkeypatch) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    cell = bind_user_agent(app, ADAPTER)

    answer = send(app, ALPHA, OWNER_USER, "to-user", text="note for the owner")

    [delivery] = answer["deliveries"]
    assert {key: delivery[key] for key in ("target", "accepted", "queued", "pushed", "via")} == {
        "target": OWNER_USER, "accepted": True, "queued": False, "pushed": True,
        "via": "user-agent",
    }
    assert "receiverAdapter" not in delivery
    assert answer["messageId"] == delivery["messageId"]
    assert booted.daemon.user_delivery.requests == []
    [(_chat, _text, key)] = booted.daemon.gateway.calls
    assert key == f"key-to-user:{OWNER_USER}:push:{cell.cell_id}:im:"
    rows = stored(app, delivery["messageId"])
    assert {table: (row["sender"], row["recipient"]) for table, row in rows.items()} == {
        "inbox": (ALPHA, OWNER_USER), "sender_copies": (ALPHA, OWNER_USER),
    }
    settled = settle(booted)
    assert settled.sources(delivery["messageId"]) == ["inbox", "sender_copies"]
    assert record_ends(app, delivery["messageId"]) == {
        ("inbox", ALPHA, OWNER_USER), ("sender_copies", ALPHA, OWNER_USER),
    }
    assert app._inbox.pending_messages(OWNER_USER) == ()


def test_user_end_is_the_http_session_principal_byte_for_byte(golden_daemon, monkeypatch) -> None:
    """GUI line (PR #2885): the record's user end is the HTTP principal actor."""

    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    answer = send(booted.app, ALPHA, OWNER_USER, "principal")
    settle(booted)

    principal = http_principal_actor(canonical_user_uri(OWNER), OWNER)
    ends = record_ends(booted.app, answer["messageId"])
    assert {recipient for _, _, recipient in ends} == {principal}
    assert principal.encode() == b"user:op"


def test_default_conversation_ids_and_peers_are_dev_literals(golden_daemon, monkeypatch) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app

    to_user = _default_send(app, "default-to-user")
    from_user = operator_send(app, ALPHA, "default-from-user")

    assert to_user["conversationId"] == DEFAULT_ALPHA_USER
    assert from_user["conversationId"] == DEFAULT_ALPHA_USER
    assert to_user["deliveries"][0]["conversationId"] == DEFAULT_ALPHA_USER
    settle(booted)
    listed = app.handle("conversations.list", session(ALPHA))
    heads = [head for head in listed["conversations"]
             if head["legacyConversationId"] == DEFAULT_ALPHA_USER]
    assert [(head["peer"], head["kind"], head["messages"]) for head in heads] == [
        (OWNER_USER, "user", 2)
    ]


@pytest.mark.parametrize(
    "address",
    [
        "agent:op:node-9:hyprial-user",
        "agent:op:node-1:hyprial-user",
        "local-id-form",
        "hyprial-user",
    ],
)
def test_any_machines_user_agent_address_is_the_owner(golden_daemon, monkeypatch, address) -> None:
    """Another machine's name form and every spelling of this machine's instance land locally."""

    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    if address == "local-id-form":
        address = user_agent_id_address(app)
        assert address.startswith("agent:op:node-1:a.")

    answer = _default_send(app, "via-instance", to=address)

    [delivery] = answer["deliveries"]
    assert delivery["target"] == OWNER_USER
    assert answer["conversationId"] == DEFAULT_ALPHA_USER
    rows = stored(app, delivery["messageId"])
    assert rows["inbox"]["recipient"] == OWNER_USER


@pytest.mark.parametrize("which", ["former-derived", "this-machines-id"])
def test_pending_merge_another_machines_user_agent_id_form_is_an_ordinary_target(
    golden_daemon, monkeypatch, which
) -> None:
    """Pinned for the later merge step: no text rule maps an id form to the owner.

    Another machine's instance is recognised by its name form only, so its id
    form (the one an older build derived, or an id equal to this machine's
    own) is sent like any other remote agent address: never to ``user:op``.
    """

    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    address = (f"agent:op:node-9:{FORMER_USER_AGENT_ID}" if which == "former-derived"
               else user_agent_id_address(app, "node-9"))

    answer = _default_send(app, f"other-{which}", to=address)

    assert [delivery["target"] for delivery in answer["deliveries"]] == [address]
    assert answer["conversationId"] != DEFAULT_ALPHA_USER


def test_replay_returns_the_original_delivery_and_does_not_push_twice(
    golden_daemon, monkeypatch
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    bind_user_agent(app, ADAPTER)

    first = send(app, ALPHA, OWNER_USER, "replayed")
    again = send(app, ALPHA, OWNER_USER, "replayed")

    assert again["messageId"] == first["messageId"]
    assert (again["deliveries"][0]["accepted"], again["deliveries"][0]["pushed"]) == (True, True)
    [(_c1, _t1, first_key), (_c2, _t2, replay_key)] = booted.daemon.gateway.calls
    assert first_key == replay_key
    assert len(stored(app, first["messageId"])) == 2


def test_an_empty_user_agent_cell_never_fails_the_recorded_send(golden_daemon, monkeypatch) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app

    answer = send(app, ALPHA, OWNER_USER, "push-unconfigured")

    [delivery] = answer["deliveries"]
    assert answer["ok"] is True
    assert (delivery["accepted"], delivery["queued"], delivery["pushed"]) == (True, False, False)
    assert delivery["pushCode"] == "TARGET_UNRESOLVED"
    assert settle(booted).sources(delivery["messageId"]) == ["inbox", "sender_copies"]


def test_role_carrying_message_to_the_owner_is_refused(golden_daemon, monkeypatch) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    params = {**session(ALPHA), "to": [OWNER_USER], "summary": "s", "message": "do it",
              "idempotencyKey": "key-role", "role": "execute"}

    with pytest.raises(DaemonRequestError) as refused:
        booted.app.handle("message.send", params)

    assert refused.value.code == ipc_errors.DISPATCH_ROLE_MISMATCH
    assert booted.daemon.user_delivery.requests == []


def test_without_a_local_user_agent_the_send_is_refused(golden_daemon, monkeypatch) -> None:
    """I7a: nothing is queued and there is no user agent cell to push through."""

    booted = boot(golden_daemon, monkeypatch)

    with pytest.raises(DaemonRequestError) as refused:
        send(booted.app, ALPHA, OWNER_USER, "nowhere")

    assert refused.value.code == ipc_errors.TARGET_UNRESOLVED
    assert booted.daemon.user_delivery.requests == []


def test_another_owners_user_target_keeps_the_push_only_path(golden_daemon, monkeypatch) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)

    answer = send(booted.app, ALPHA, "user:someone-else", "other-owner")

    [delivery] = answer["deliveries"]
    assert "pushed" not in delivery
    assert stored(booted.app, delivery["messageId"]) == {}
    [request] = booted.daemon.user_delivery.requests
    assert request.owner == "someone-else"


def test_key_lookup_and_status_answer_for_a_message_to_the_owner(golden_daemon, monkeypatch) -> None:
    """``message.status`` speaks of the queue only: this node's inbox received
    the row, the answer a local agent target gets.  Whether the owner push
    went out is the send result's ``pushed``."""

    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app = booted.app
    operation = "lookup-operation-key-0001"
    answer = app.handle("message.send", {
        **session(ALPHA), "to": [OWNER_USER], "summary": "s", "message": "look me up",
        "idempotencyKey": operation,
    })
    message_id = answer["messageId"]

    looked = app.handle("message.send.key_lookup", {**session(ALPHA), "operationKey": operation})
    assert looked == {"status": "accepted", "commandId": operation,
                      "conversationId": answer["conversationId"], "messageId": message_id}

    def state() -> tuple[str, str]:
        status = app.handle("message.status", {**session(ALPHA), "messageId": message_id})
        [record] = status["records"]
        return status["state"], record["reason"]

    at_send = state()
    settle(booted)
    assert at_send == state() == ("fetched", "ACK_RECEIVED")


def test_targets_list_the_owner_and_hide_user_agent_rows(golden_daemon, monkeypatch) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)

    targets = booted.app.handle("targets", {})["targets"]

    users = [row for row in targets if row["targetKind"] == "user"]
    assert users == [{"targetKind": "user", "targetUri": OWNER_USER, "actor": OWNER_USER,
                      "status": "user-agent", "deliverable": True}]
    local = user_agent_id_address(booted.app)
    assert not any("hyprial-user" in row["targetUri"] or row["targetUri"] == local
                   for row in targets)
