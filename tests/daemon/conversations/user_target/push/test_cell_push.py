"""I3: a sender whose cell binds an adapter pushes the owner's copy to that cell's chats.

Decision notes (I3, contract i7-0 v2 sections 3.2 to 3.5, owner items A-6 and A-7):

- "Bound" is decided from the sender's own cell structure: any adapter-role
  member.  A bound cell's own data
  names the chats (``user_push_targets``) and every failure stays per target.
- The IM copy of a queued message is best-effort: one inline attempt, the
  send answers ok whatever the push does (``pushed``, ``via: cell``,
  ``pushCode``), and nothing retries it.
- Each case asserts the gateway calls, so no chat the document does not
  name is ever posted to.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError, LarkSdkError
from hyprial.daemon.impl.application.messaging.inbox_surface.send_targets import user as user_target
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.acceptance.acceptance_support import send
from tests.daemon.conversations.bypass.bypass_support import boot, settle, stored
from tests.daemon.conversations.user_target.user_target_support import boot_with_user_agent
from tests.support.golden.app import ADAPTER, ALPHA, OWNER_USER, golden_daemon as golden_daemon
from tests.support.identity.cell_push import edit_gateway, fail_chats, install_cell, push_cell

DM, GROUP = "oc_dm_chat", "oc_group_chat"
SCOPE = LarkApiError(
    "send message", 99991679, "scope", missing_scopes=("im:message",),
    authorization_url="https://open.example/auth",
)


def _alpha(app: Any) -> Any:
    return app.agents.require("alpha")


def _bind(app: Any, members: dict[str, tuple[str, str | None]] | None = None, **cell: Any) -> Any:
    agent = _alpha(app)
    members = members if members is not None else {
        agent.uri: ("harness", None), "im": ("adapter", ADAPTER),
    }
    document = push_cell(agent, members, **cell)
    install_cell(app, document)
    return document


def _booted(golden_daemon: Any, monkeypatch: pytest.MonkeyPatch, *, default_route: str | None = "dm"):
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    if default_route is not None:
        edit_gateway(booted.app, defaultRoute=default_route)
    return booted


def _key(cell: Any, member: str, route: str | None, key: str) -> str:
    return f"key-{key}:{OWNER_USER}:push:{cell.cell_id}:{member}:{route or ''}"


def _push_log(booted: Any) -> dict[str, Any]:
    [record] = booted.logs.named("send.user_push")
    return record


def test_a_bound_sender_is_pushed_to_its_adapters_default_route(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    cell = _bind(app)

    [delivery] = send(app, ALPHA, OWNER_USER, "bound")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["via"]) == (True, True, "cell")
    assert "pushCode" not in delivery
    assert booted.daemon.gateway.calls == [
        (DM, f"转述自 {ALPHA}：\n\nbody bound ☃", _key(cell, "im", None, "bound")),
    ]
    assert set(stored(app, delivery["messageId"])) == {"inbox", "sender_copies"}
    record = _push_log(booted)
    assert record["pushed"] is True
    assert record["targets"] == [{
        "member": "im", "adapter": ADAPTER, "route": "dm", "state": "sent",
        "code": None, "reason": None,
    }]


def test_explicit_user_connections_choose_the_chats_in_document_order(
    golden_daemon, monkeypatch
) -> None:
    booted = _booted(golden_daemon, monkeypatch, default_route=None)
    app = booted.app
    agent = _alpha(app)
    cell = _bind(app, connections=(
        {"from": agent.uri, "on": "user", "to": "im", "route": "group"},
        {"from": agent.uri, "on": "user", "to": "im", "route": "dm"},
    ))

    [delivery] = send(app, ALPHA, OWNER_USER, "two")["deliveries"]

    assert (delivery["pushed"], delivery["via"]) == (True, "cell")
    assert [(chat, key) for chat, _text, key in booted.daemon.gateway.calls] == [
        (GROUP, _key(cell, "im", "group", "two")),
        (DM, _key(cell, "im", "dm", "two")),
    ]


def test_one_failing_chat_of_two_answers_pushed_with_its_code(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch, default_route=None)
    app = booted.app
    agent = _alpha(app)
    _bind(app, connections=(
        {"from": agent.uri, "on": "user", "to": "im", "route": "group"},
        {"from": agent.uri, "on": "user", "to": "im", "route": "dm"},
    ))
    fail_chats(booted.daemon.gateway, monkeypatch, {GROUP: LarkApiError("send message", 230002, "x")})

    [delivery] = send(app, ALPHA, OWNER_USER, "half")["deliveries"]

    assert (delivery["pushed"], delivery["via"], delivery["pushCode"]) == (
        True, "cell", ipc_errors.USER_DELIVERY_FAILED,
    )
    assert [call[0] for call in booted.daemon.gateway.calls] == [GROUP, DM]


def test_a_user_connection_on_another_member_silences_this_one(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    agent = _alpha(app)
    _bind(
        app,
        {agent.uri: ("harness", None), "w2": ("harness", None), "im": ("adapter", ADAPTER)},
        entry=agent.uri,
        connections=({"from": "w2", "on": "user", "to": "im"},),
    )

    [delivery] = send(app, ALPHA, OWNER_USER, "silent")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.TARGET_UNRESOLVED,
    )
    assert booted.daemon.gateway.calls == []
    assert _push_log(booted)["pushSkipped"] == "sender-cell-silent"


def test_a_bound_cell_without_an_entry_member_pushes_nothing(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    agent = _alpha(app)
    _bind(app, {agent.uri: ("harness", None), "w2": ("harness", None), "im": ("adapter", ADAPTER)})

    [delivery] = send(app, ALPHA, OWNER_USER, "no-entry")["deliveries"]

    assert (delivery["pushed"], delivery["pushCode"]) == (False, ipc_errors.TARGET_UNRESOLVED)
    assert booted.daemon.gateway.calls == []
    assert _push_log(booted)["pushSkipped"] == "sender-member-unknown"


def test_a_queued_send_without_a_cell_runtime_answers_ok_and_hands_nothing_off(
    golden_daemon, monkeypatch
) -> None:
    """Row 6 on the queued path: the runtime is missing but no composition failed.

    Production reaches this only while another thread holds the composition
    lock past its wait; ``_cell_runtime_failed_at`` is not set, so the send
    is still queued.  The failed-composition state is the next test.
    """

    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    monkeypatch.setattr(user_target, "cell_runtime", lambda _app: None)

    [delivery] = send(app, ALPHA, OWNER_USER, "no-runtime")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.USER_DELIVERY_UNAVAILABLE,
    )
    assert booted.daemon.gateway.calls == []
    assert _push_log(booted)["pushSkipped"] == "cell-unavailable"


def test_after_a_failed_composition_every_local_agents_send_to_the_owner_is_refused(
    golden_daemon, monkeypatch
) -> None:
    """KP-129: a failed composition stops queueing, and row 6 refuses the push-only send.

    Bound or not, a local agent's cell cannot be read, so its copy is
    ``USER_DELIVERY_UNAVAILABLE``.
    """

    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    monkeypatch.setattr(app, "_cell_runtime", None, raising=False)
    monkeypatch.setattr(app, "_cell_runtime_failed_at", time.monotonic(), raising=False)

    with pytest.raises(DaemonRequestError) as refused:
        send(app, ALPHA, OWNER_USER, "composition-failed")

    assert refused.value.code == ipc_errors.USER_DELIVERY_UNAVAILABLE
    assert refused.value.data["target"] == OWNER_USER
    assert app._inbox.pending_messages(OWNER_USER) == ()
    assert booted.daemon.gateway.calls == []
    assert _push_log(booted)["pushSkipped"] == "cell-unavailable"


def test_a_cell_structure_that_cannot_be_parsed_never_fails_the_queued_send(
    golden_daemon, monkeypatch
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    _bind(app)

    def unparsable(_cell: Any) -> Any:
        raise TypeError("adapter must be a non-empty string")

    monkeypatch.setattr(user_target, "parse_structure", unparsable)

    [delivery] = send(app, ALPHA, OWNER_USER, "unparsable")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.USER_DELIVERY_UNAVAILABLE,
    )
    assert booted.daemon.gateway.calls == []
    assert _push_log(booted)["pushSkipped"] == "cell-unavailable"


def test_an_unparsable_agent_cell_leaves_the_reply_path_hint_false(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    _bind(app)
    assert user_target.agent_cell_names_a_chat(app) is True

    def unparsable(_cell: Any) -> Any:
        raise TypeError("adapter must be a non-empty string")

    monkeypatch.setattr(user_target, "parse_structure", unparsable)

    assert user_target.agent_cell_names_a_chat(app) is False


def test_an_exception_while_pushing_the_copy_never_fails_the_queued_send(
    golden_daemon, monkeypatch
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    _bind(app)

    def broken(_structure: Any, _member: str) -> Any:
        raise ValueError("broken connection row")

    monkeypatch.setattr(user_target, "user_push_targets", broken)

    [delivery] = send(app, ALPHA, OWNER_USER, "broken")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.USER_DELIVERY_UNAVAILABLE,
    )
    assert booted.daemon.gateway.calls == []
    record = _push_log(booted)
    assert (record["pushSkipped"], record["errorType"]) == ("cell-unavailable", "ValueError")


def test_an_adapter_member_without_its_datum_is_unresolved_not_handed_off(
    golden_daemon, monkeypatch
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    _bind(app, {_alpha(app).uri: ("harness", None), "im": ("adapter", None)})

    [delivery] = send(app, ALPHA, OWNER_USER, "datum-less")["deliveries"]

    assert (delivery["pushed"], delivery["pushCode"]) == (False, ipc_errors.TARGET_UNRESOLVED)
    assert booted.daemon.gateway.calls == []
    [target] = _push_log(booted)["targets"]
    assert (target["state"], target["reason"]) == ("not-sent", "member-adapter-missing")


def test_a_user_hop_between_harness_members_is_still_unbound(golden_daemon, monkeypatch) -> None:
    from tests.support.identity.cell_push import user_agent_cell

    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    agent = _alpha(app)
    install_cell(app, user_agent_cell(app, {"im": ADAPTER}))
    _bind(
        app,
        {agent.uri: ("harness", None), "w2": ("harness", None)},
        entry=agent.uri,
        connections=({"from": agent.uri, "on": "user", "to": "w2"},),
    )

    [delivery] = send(app, ALPHA, OWNER_USER, "hop")["deliveries"]

    assert (delivery["pushed"], delivery["via"]) == (True, "user-agent")
    assert [call[0] for call in booted.daemon.gateway.calls] == [DM]


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("adapter-unconfigured", "adapter-unconfigured"),
        ("no-default-route", "no-default-route"),
        ("route-unconfigured", "route-unconfigured"),
    ],
)
def test_configuration_gaps_are_unresolved_per_target(golden_daemon, monkeypatch, setup, reason) -> None:
    booted = _booted(golden_daemon, monkeypatch, default_route=None)
    app = booted.app
    agent = _alpha(app)
    if setup == "adapter-unconfigured":
        _bind(app, {agent.uri: ("harness", None), "im": ("adapter", "nobody")})
    elif setup == "no-default-route":
        _bind(app)
    else:
        _bind(app, connections=({"from": agent.uri, "on": "user", "to": "im", "route": "nope"},))

    [delivery] = send(app, ALPHA, OWNER_USER, setup)["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.TARGET_UNRESOLVED,
    )
    assert booted.daemon.gateway.calls == []
    [target] = _push_log(booted)["targets"]
    assert (target["state"], target["code"], target["reason"]) == (
        "not-sent", ipc_errors.TARGET_UNRESOLVED, reason,
    )


@pytest.mark.parametrize("cause", ["secret-missing", "secret-without-app-secret", "channels-invalid"])
def test_a_failure_before_any_request_is_unavailable(golden_daemon, monkeypatch, cause) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    _bind(app)
    secret = app.hyprial_home / "secrets" / f"lark-{ADAPTER}.json"
    if cause == "secret-missing":
        secret.unlink()
    elif cause == "secret-without-app-secret":
        secret.write_text("{}", encoding="utf-8")
    else:
        (app.hyprial_home / "channels.json").write_text("{not json", encoding="utf-8")

    [delivery] = send(app, ALPHA, OWNER_USER, cause)["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.USER_DELIVERY_UNAVAILABLE,
    )
    assert booted.daemon.gateway.calls == []
    [target] = _push_log(booted)["targets"]
    assert (target["state"], target["reason"]) == ("not-sent", "adapter-unavailable")


@pytest.mark.parametrize(
    ("failure", "lark_code"),
    [
        (SCOPE, 99991679),
        (LarkApiError("send message", 230002, "x"), 230002),
        (LarkApiError("send message", 230006, "x"), 230006),
        (LarkApiError("send message", 232011, "x"), 232011),
        (LarkApiError("send message", 999999, "x"), 999999),
    ],
    ids=["scope", "not-in-chat", "bot-disabled", "operator-not-in-chat", "catch-all"],
)
def test_a_provider_refusal_is_failed_and_logged_with_its_data(
    golden_daemon, monkeypatch, failure, lark_code
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    _bind(app)
    fail_chats(booted.daemon.gateway, monkeypatch, {DM: failure})

    [delivery] = send(app, ALPHA, OWNER_USER, "refused")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.USER_DELIVERY_FAILED,
    )
    [target] = _push_log(booted)["targets"]
    assert (target["state"], target["reason"], target["larkCode"]) == (
        "not-sent", "provider-refused", lark_code,
    )
    assert target["data"]["chatId"] == DM
    if failure is SCOPE:
        assert target["data"]["authorizationUrl"] == "https://open.example/auth"
        assert "authorizationUrl" not in str(delivery)


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("socket closed"), LarkSdkError("send message returned no message id")],
    ids=["transport", "no-message-id"],
)
def test_an_unknown_outcome_never_fails_the_queued_send(golden_daemon, monkeypatch, failure) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    _bind(app)
    fail_chats(booted.daemon.gateway, monkeypatch, {DM: failure})

    [delivery] = send(app, ALPHA, OWNER_USER, "unknown")["deliveries"]

    assert (delivery["accepted"], delivery["pushed"], delivery["pushCode"]) == (
        True, False, ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
    )
    assert set(stored(app, delivery["messageId"])) == {"inbox", "sender_copies"}
    settle(booted)
    [target] = _push_log(booted)["targets"]
    assert (target["state"], target["reason"]) == ("unknown", "outcome-unknown")


def test_sent_and_unknown_answers_pushed_with_the_unknown_code(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch, default_route=None)
    app = booted.app
    agent = _alpha(app)
    _bind(app, connections=(
        {"from": agent.uri, "on": "user", "to": "im", "route": "dm"},
        {"from": agent.uri, "on": "user", "to": "im", "route": "group"},
    ))
    fail_chats(booted.daemon.gateway, monkeypatch, {GROUP: RuntimeError("timeout")})

    [delivery] = send(app, ALPHA, OWNER_USER, "mixed")["deliveries"]

    assert (delivery["pushed"], delivery["pushCode"]) == (True, ipc_errors.SUBMIT_OUTCOME_UNKNOWN)


def test_a_same_key_replay_pushes_again_under_the_same_keys(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    _bind(app)

    first = send(app, ALPHA, OWNER_USER, "replay")["deliveries"][0]
    again = send(app, ALPHA, OWNER_USER, "replay")["deliveries"][0]

    assert first["messageId"] == again["messageId"]
    assert (again["pushed"], again["via"]) == (True, "cell")
    keys = [key for _chat, _text, key in booted.daemon.gateway.calls]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert len(app._inbox.pending_messages(OWNER_USER)) == 1


def test_a_workflow_report_from_a_bound_agent_is_pushed_once_even_when_unreachable(
    golden_daemon, monkeypatch
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    _bind(app)
    fail_chats(booted.daemon.gateway, monkeypatch, {DM: RuntimeError("offline")})

    assert app._workflow_deliver_user(OWNER_USER, "graph g1 node n9 completed", "workflow-9", ALPHA)

    assert [call[0] for call in booted.daemon.gateway.calls] == [DM]
    [row] = app._inbox.pending_messages(OWNER_USER)
    assert row.sender == ALPHA


def test_the_push_only_path_answers_or_refuses_from_the_cell_push(golden_daemon, monkeypatch) -> None:
    booted = boot(golden_daemon, monkeypatch)
    app = booted.app
    edit_gateway(app, defaultRoute="dm")
    _bind(app)

    [delivery] = send(app, ALPHA, OWNER_USER, "push-only")["deliveries"]

    assert (delivery["accepted"], delivery["queued"], delivery["via"]) == (True, False, "cell")
    assert "pushed" not in delivery
    assert [call[0] for call in booted.daemon.gateway.calls] == [DM]

    fail_chats(booted.daemon.gateway, monkeypatch, {DM: RuntimeError("offline")})
    with pytest.raises(DaemonRequestError) as unknown:
        send(app, ALPHA, OWNER_USER, "push-only-unknown")
    assert unknown.value.code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert unknown.value.data["target"] == OWNER_USER

    fail_chats(booted.daemon.gateway, monkeypatch, {DM: LarkApiError("send message", 230002, "x")})
    with pytest.raises(DaemonRequestError) as refused:
        send(app, ALPHA, OWNER_USER, "push-only-refused")
    assert refused.value.code == ipc_errors.USER_DELIVERY_FAILED


def test_a_reply_path_to_the_owner_is_available_when_an_agent_cell_binds_an_adapter(
    golden_daemon, monkeypatch
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    install_cell(app, push_cell(_alpha(app), {_alpha(app).uri: ("harness", None)}))
    assert app._reply_path_unavailable(OWNER_USER) is True

    _bind(app)

    assert app._reply_path_unavailable(OWNER_USER) is False


def test_a_member_address_is_decided_by_that_member_and_the_plain_address_by_the_entry(
    golden_daemon, monkeypatch
) -> None:
    """Row 2: a member launch speaks as ``<name>@<member>``; the plain address is the entry."""

    from hyprial.daemon.impl.cells.structure.lookup import PushTarget, user_push_targets
    from hyprial.kernel import canonical_agent_uri

    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    agent = _alpha(app)
    _bind(
        app,
        {agent.uri: ("harness", None), "w2": ("harness", None), "im": ("adapter", ADAPTER)},
        entry=agent.uri,
        connections=({"from": "w2", "on": "user", "to": "im", "route": "group"},),
    )

    member = user_target._sender_cell(app, canonical_agent_uri(app.owner, app.node_id, "alpha@w2"))
    entry = user_target._sender_cell(app, ALPHA)

    assert (member.kind, member.member) == ("bound", "w2")
    assert user_push_targets(member.structure, member.member) == (PushTarget("im", "group"),)
    assert (entry.kind, entry.member) == ("bound", agent.uri)
    assert user_push_targets(entry.structure, entry.member) == ()


def test_under_id_form_a_bound_sender_is_pushed_and_its_member_address_resolves(
    golden_daemon, monkeypatch
) -> None:
    """Id-form minting (the release default): the agent's address is its ``a.`` id,
    while a member launch keeps ``<name>@<member>`` (``_canonical_harness_uri``)."""

    from hyprial.kernel import HarnessLaunchSpec, canonical_agent_uri

    daemon = golden_daemon(id_form=True, user_agent=True, links=False)
    app = daemon.app
    edit_gateway(app, defaultRoute="dm")
    agent = _alpha(app)
    _bind(app)
    spec = HarnessLaunchSpec(harness="claude", name="alpha", member="w2", headless=True)
    member_address = app._canonical_harness_uri("alpha", spec)

    [delivery] = send(app, agent.uri, OWNER_USER, "id-form")["deliveries"]

    assert agent.uri != ALPHA
    assert (delivery["accepted"], delivery["queued"], delivery["via"]) == (True, False, "cell")
    assert [call[0] for call in daemon.gateway.calls] == [DM]
    assert member_address == canonical_agent_uri(app.owner, app.node_id, "alpha@w2")
    decided = user_target._sender_cell(app, member_address)
    assert (decided.kind, decided.member) == ("bound", "w2")


FANOUT_ROUTES = [
    {"name": "dm", "type": "direct", "nativeId": DM},
    {"name": "group", "type": "direct", "nativeId": GROUP},
    {"name": "both", "type": "fanout", "members": ["dm", "group"]},
    {"name": "nested", "type": "fanout", "members": ["dm", "both"]},
]


def test_a_fanout_route_posts_each_chat_under_its_member_key(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch, default_route=None)
    app = booted.app
    edit_gateway(app, routes=FANOUT_ROUTES)
    agent = _alpha(app)
    cell = _bind(app, connections=({"from": agent.uri, "on": "user", "to": "im", "route": "both"},))

    [delivery] = send(app, ALPHA, OWNER_USER, "fan")["deliveries"]

    assert (delivery["pushed"], delivery["via"]) == (True, "cell")
    base = _key(cell, "im", "both", "fan")
    assert [(chat, key) for chat, _text, key in booted.daemon.gateway.calls] == [
        (DM, f"{base}:dm"), (GROUP, f"{base}:group"),
    ]


def test_a_fanout_that_fails_after_a_posted_chat_is_sent_with_its_code(
    golden_daemon, monkeypatch
) -> None:
    booted = _booted(golden_daemon, monkeypatch, default_route=None)
    app = booted.app
    edit_gateway(app, routes=FANOUT_ROUTES)
    agent = _alpha(app)
    _bind(app, connections=({"from": agent.uri, "on": "user", "to": "im", "route": "both"},))
    fail_chats(booted.daemon.gateway, monkeypatch, {GROUP: LarkApiError("send message", 230002, "x")})

    [delivery] = send(app, ALPHA, OWNER_USER, "fan-partial")["deliveries"]

    assert (delivery["pushed"], delivery["pushCode"]) == (True, ipc_errors.USER_DELIVERY_FAILED)
    [target] = _push_log(booted)["targets"]
    assert (target["state"], target["reason"], target["route"]) == ("sent", "fanout-partial", "group")


def test_a_failing_first_chat_of_a_fanout_does_not_hide_the_next_one(
    golden_daemon, monkeypatch
) -> None:
    """Contract 3.3 row 10: every chat of a fanout is attempted, whatever the order."""

    booted = _booted(golden_daemon, monkeypatch, default_route=None)
    app = booted.app
    edit_gateway(app, routes=FANOUT_ROUTES)
    agent = _alpha(app)
    cell = _bind(app, connections=({"from": agent.uri, "on": "user", "to": "im", "route": "both"},))
    fail_chats(booted.daemon.gateway, monkeypatch, {DM: LarkApiError("send message", 230002, "x")})

    [delivery] = send(app, ALPHA, OWNER_USER, "fan-first")["deliveries"]

    assert (delivery["pushed"], delivery["via"], delivery["pushCode"]) == (
        True, "cell", ipc_errors.USER_DELIVERY_FAILED,
    )
    base = _key(cell, "im", "both", "fan-first")
    assert [(chat, key) for chat, _text, key in booted.daemon.gateway.calls] == [
        (DM, f"{base}:dm"), (GROUP, f"{base}:group"),
    ]
    [target] = _push_log(booted)["targets"]
    assert (target["state"], target["reason"], target["route"]) == ("sent", "fanout-partial", "dm")


def test_a_fanout_with_no_posted_chat_is_unknown_when_any_chat_is_unknown(
    golden_daemon, monkeypatch
) -> None:
    booted = _booted(golden_daemon, monkeypatch, default_route=None)
    app = booted.app
    edit_gateway(app, routes=FANOUT_ROUTES)
    agent = _alpha(app)
    _bind(app, connections=({"from": agent.uri, "on": "user", "to": "im", "route": "both"},))
    fail_chats(booted.daemon.gateway, monkeypatch, {
        DM: LarkApiError("send message", 230002, "x"), GROUP: RuntimeError("offline"),
    })

    [delivery] = send(app, ALPHA, OWNER_USER, "fan-none")["deliveries"]

    assert (delivery["pushed"], delivery["pushCode"]) == (False, ipc_errors.SUBMIT_OUTCOME_UNKNOWN)
    assert [call[0] for call in booted.daemon.gateway.calls] == [DM, GROUP]
    [target] = _push_log(booted)["targets"]
    assert (target["state"], target["reason"], target["route"]) == (
        "unknown", "outcome-unknown", "group",
    )


def test_a_fanout_with_an_invalid_member_is_unresolved(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch, default_route=None)
    app = booted.app
    edit_gateway(app, routes=FANOUT_ROUTES)
    agent = _alpha(app)
    _bind(app, connections=({"from": agent.uri, "on": "user", "to": "im", "route": "nested"},))

    [delivery] = send(app, ALPHA, OWNER_USER, "fan-invalid")["deliveries"]

    assert (delivery["pushed"], delivery["pushCode"]) == (False, ipc_errors.TARGET_UNRESOLVED)
    assert booted.daemon.gateway.calls == []
    [target] = _push_log(booted)["targets"]
    assert target["reason"] == "route-fanout-invalid"


def test_a_connection_to_a_harness_member_on_user_is_a_hop_not_a_target(
    golden_daemon, monkeypatch
) -> None:
    """Row 2 of the failure table at the push level: only adapter members are posted to."""

    from hyprial.daemon.impl.network.route_push import CopyTarget, push_user_copy

    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app

    [outcome] = push_user_copy(
        app, cell_id="c.test", text="t", sender=ALPHA, target_key="k",
        targets=(CopyTarget(member="w2", route=None, adapter=None, is_adapter_member=False),),
    )

    assert (outcome.state, outcome.code, outcome.reason) == (
        "not-sent", ipc_errors.TARGET_UNRESOLVED, "member-not-adapter",
    )
    assert booted.daemon.gateway.calls == []
