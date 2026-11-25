"""Every delivered message names both ends, and an adapter's sender generically.

Allen, 2026-09-25 (Feishu DM): 「将用户名明确代入所有传递的信息中，并添加测试，
确保from、to的信息在所有信息中都存在」.

Allen, 2026-11-19 (IM binding removal, tier 2): an adapter is transparent to
agents, and an IM sender is simply the person who owns that IM account.  So
the sender an agent sees is the same shape for every adapter kind: ``from`` is
the adapter address, and ``origin`` carries the platform facts the adapter
itself observed (account id, display name, chat kind, platform message id and
time).  hyprial does not judge whether that person is the owner: there is no
``owner``, ``standing`` or ``verified`` anywhere in the block or the hint line.

Every reader surface carries ``from`` and ``to``.  The surfaces are listed
explicitly below; a surface missing from the list is one that can silently
skip, so adding a surface means adding it here.
"""

from __future__ import annotations

from tests.support.runtime.cell_delivery import AdmittingCellDelivery

import sqlite3
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import inspect
import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark.inbound.adapter import LarkAdapter
from hyprial.daemon.impl.adapters.lark.state.records import Identity
from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore
from hyprial.daemon.impl.api import HarnessDelivery, delivery_prompt, describe_sender
from hyprial.daemon.impl.application import _message_origin

from tests.daemon.delivery.origin.test_inbound_origin import _app, _round_trip  # type: ignore[import-not-found]
from tests.daemon.adapters.lark.actors.adapter.support import BOT_OPEN_ID, _pin_group, actor as actor, make_adapter, sdk_event
from hyprial.daemon.impl.adapters.lark.contracts.wire import ActorTarget
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.runtime import DaemonEventBridge
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from tests.daemon.harnesses.actors.test_harness_reply_settlement import AGENT, NODE, _Transport, inbox as inbox

OWNER = "owner@example.com"
REPO = Path(__file__).resolve().parents[4]

SENDER_KEYS = {"kind", "platformId", "displayName"}
REMOVED_SENDER_KEYS = {
    "owner", "standing", "unionId", "userKey", "userKind", "nickname",
    "realName", "source", "candidateOwners", "candidateUsers", "lookupFailed",
}

LEGACY_SENDER = {
    "kind": "user",
    "platformId": "ou_x",
    "unionId": "on_x",
    "displayName": "Allen",
    "owner": OWNER,
    "standing": "verified",
    "source": "manual:test-roster",
    "userKey": "usr_1",
    "userKind": "member",
    "nickname": "al",
    "realName": "Allen Woods",
    "candidateOwners": [OWNER],
    "candidateUsers": ["usr_1"],
    "lookupFailed": True,
}


def _deliver(tmp_path: Path, actor: ActorTarget, **event: object):  # type: ignore[no-untyped-def]
    adapter, _lark, harness, routes, _events = make_adapter(tmp_path, actor)
    _pin_group(routes, actor, "oc_group")
    try:
        sdk_event(
            adapter.handle_sdk_event,
            event_id="evt_1",
            message_id="om_inbound",
            text="@_user_1 please investigate",
            mention=actor.display_name,
            **event,
        )
    finally:
        adapter.close()
    (request,) = harness.requests
    return request.provider_metadata


def test_the_lark_adapter_sends_only_platform_facts_for_a_dm(
    tmp_path: Path, actor: ActorTarget  # noqa: F811 - the imported fixture
) -> None:
    metadata = _deliver(tmp_path, actor)

    assert metadata["sender"] == {
        "kind": "user", "platformId": "ou_sender", "displayName": None,
    }
    assert metadata["chatKind"] == "dm"
    assert metadata["chatType"] == "p2p"
    assert metadata["messageId"] == "om_inbound"


def test_the_lark_adapter_reports_a_group_as_a_group(
    tmp_path: Path, actor: ActorTarget  # noqa: F811 - the imported fixture
) -> None:
    metadata = _deliver(
        tmp_path, actor, chat_type="group", chat_id="oc_group", mention_open_id=BOT_OPEN_ID,
    )

    assert metadata["chatKind"] == "group"
    assert set(metadata["sender"]) == SENDER_KEYS


def test_the_display_name_comes_from_the_adapters_own_observation_row(
    tmp_path: Path, actor: ActorTarget  # noqa: F811 - the imported fixture
) -> None:
    state = LarkStateStore(tmp_path / "lark-state.sqlite3")
    state.observe_identity("user", "ou_sender", display_name="王蒴", source="chat-members:oc_g")
    state.close()

    assert _deliver(tmp_path, actor)["sender"] == {
        "kind": "user", "platformId": "ou_sender", "displayName": "王蒴",
    }


def test_a_verified_row_with_an_owner_names_nobody_as_owner(
    tmp_path: Path, actor: ActorTarget  # noqa: F811 - the imported fixture
) -> None:
    """A historical verified row, here or under another adapter sharing the
    union id, is not a judgement the inbound path makes any more."""

    path = tmp_path / "lark-state.sqlite3"
    roster = LarkStateStore(path, adapter="lark:roster")
    roster.upsert_identity(Identity(
        kind="user", platform_id="ou_roster", union_id="on_sender",
        display_name="Allen", standing="verified", source="manual:test-roster",
    ))
    roster.close()
    historical = sqlite3.connect(path)
    try:
        historical.execute("UPDATE identities SET hyprial_owner = ?", (OWNER,))
        historical.commit()
    finally:
        historical.close()

    sender = _deliver(tmp_path, actor)["sender"]

    assert sender == {"kind": "user", "platformId": "ou_sender", "displayName": None}
    assert not REMOVED_SENDER_KEYS & set(sender)


def test_inbound_never_reaches_the_identity_resolver_or_the_user_store() -> None:
    """By construction: nothing to inject, nothing imported, nothing called."""

    parameters = inspect.signature(LarkAdapter.__init__).parameters
    assert "identity_resolver" not in parameters
    assert "users" not in parameters
    sources = [
        *sorted((REPO / "src/hyprial/daemon/impl/adapters/lark/inbound").glob("*.py")),
        REPO / "src/hyprial/daemon/impl/adapters/lark/credentials/identities.py",
        REPO / "src/hyprial/daemon/impl/adapters/lark/worker/entry.py",
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "daemon.impl.identity", "IdentityResolver", "UserStore",
            "resolve_sender(", "resolve_sender_identity", "is_owner_union",
        ):
            assert forbidden not in text, (path.name, forbidden)


def test_the_daemon_keeps_none_of_the_removed_sender_fields() -> None:
    origin = _message_origin({"chatKind": "dm", "sender": LEGACY_SENDER})

    assert origin is not None
    assert origin["sender"] == {"kind": "user", "platformId": "ou_x", "displayName": "Allen"}


def test_a_sender_block_without_an_account_id_is_dropped() -> None:
    assert _message_origin(
        {"chatKind": "group", "sender": {"displayName": "X", "standing": "verified"}}
    ) == {"chatKind": "group"}


@pytest.mark.parametrize("chat_type", ["p2p", "topic_group", "", 7])
def test_only_a_generic_chat_kind_is_carried(chat_type: object) -> None:
    origin = _message_origin(
        {"chatKind": chat_type, "sender": {"platformId": "ou_x"}}
    )
    assert origin is not None and "chatKind" not in origin


def test_two_adapter_kinds_produce_the_same_origin_shape() -> None:
    """Only Lark reaches ``message.send`` today, so the second kind is the
    same daemon seam fed by a non-Lark adapter's equivalent metadata."""

    def origin(provider: str, account: str) -> dict[str, object]:
        result = _message_origin({
            "provider": provider,
            "chatKind": "group",
            "messageId": f"{provider}-m-1",
            "createTime": "1793000000000",
            "sender": {"kind": "user", "platformId": account, "displayName": "Ann"},
        })
        assert result is not None
        return result

    lark = origin("lark", "ou_ann")
    slack = origin("slack", "U0ANN")

    assert set(lark) == set(slack) == {
        "provider", "chatKind", "platformMessageId", "platformCreatedAt", "sender",
    }
    assert set(lark["sender"]) == set(slack["sender"]) == SENDER_KEYS  # type: ignore[arg-type]
    assert slack["platformMessageId"] == "slack-m-1"


def test_two_adapter_kinds_reach_the_reader_in_the_same_shape(tmp_path: Path) -> None:
    sender = {"kind": "user", "platformId": "acct-1", "displayName": "Ann"}
    (tmp_path / "lark").mkdir()
    (tmp_path / "slack").mkdir()
    lark, _ = _round_trip(_app(tmp_path / "lark"), chatKind="dm", messageId="m1", sender=sender)
    slack, _ = _round_trip(
        _app(tmp_path / "slack"), provider="slack", chatKind="dm", messageId="m1", sender=sender,
    )

    assert {**lark["origin"], "provider": None} == {**slack["origin"], "provider": None}
    assert lark["origin"]["sender"] == sender


def test_the_reader_receives_the_sender_block_inside_origin(tmp_path: Path) -> None:
    row, _body = _round_trip(_app(tmp_path), chatKind="dm", sender=LEGACY_SENDER)

    assert row["origin"]["sender"] == {
        "kind": "user", "platformId": "ou_x", "displayName": "Allen",
    }
    assert row["origin"]["chatKind"] == "dm"


@pytest.mark.parametrize(
    ("chat_type", "display_name", "expected_sender"),
    [
        ("dm", "Allen", "Allen (via adapter:lark:main, account ou_x)"),
        ("group", None, "ou_x (via adapter:lark:main, account ou_x)"),
    ],
)
def test_dm_and_group_render_the_generic_hint_line(
    chat_type: str, display_name: str | None, expected_sender: str
) -> None:
    delivery = HarnessDelivery(
        delivery_id="d-1",
        conversation_id="c-1",
        sender="adapter:lark:main",
        recipient="agent:op:node-1:reader",
        message="hello",
        origin={
            "chatKind": chat_type,
            "sender": {"kind": "user", "platformId": "ou_x", "displayName": display_name},
        },
    )

    assert delivery_prompt(delivery) == (
        "[Harness Network message from adapter:lark:main"
        f" to agent:op:node-1:reader via {chat_type} chat;"
        f" sender: {expected_sender}]\nhello"
    )
    assert "verified" not in delivery_prompt(delivery)


def test_the_hint_line_ignores_any_legacy_owner_judgement() -> None:
    assert describe_sender({"sender": LEGACY_SENDER}, "adapter:slack:team") == (
        "Allen (via adapter:slack:team, account ou_x)"
    )


def test_no_sender_block_means_no_hint_line() -> None:
    assert describe_sender({"chatKind": "dm"}, "adapter:lark:main") is None
    assert describe_sender(None, "adapter:lark:main") is None


def test_surface_pending_list_row_names_from_and_to(tmp_path: Path) -> None:
    app = _app(tmp_path)
    row, _body = _round_trip(app)
    assert row["from"] == app.agents.get("driver").uri
    assert row["to"] == app.agents.get("reader").uri


def test_harness_delivery_freezes_nested_origin_at_construction() -> None:
    origin = {
        "chatKind": "dm",
        "sender": {"displayName": "Allen", "aliases": ["al"]},
    }
    delivery = HarnessDelivery("d-1", "c-1", "sender", "recipient", "hello", origin)

    origin["chatKind"] = "group"
    origin["sender"]["displayName"] = "Mallory"
    origin["sender"]["aliases"].append("mal")

    assert delivery.origin == {
        "chatKind": "dm",
        "sender": {"displayName": "Allen", "aliases": ("al",)},
    }
    with pytest.raises(TypeError):
        delivery.origin["chatKind"] = "group"
    with pytest.raises(TypeError):
        delivery.origin["sender"]["displayName"] = "Mallory"


def test_surface_headless_prompt_names_both_ends_without_an_origin() -> None:
    delivery = HarnessDelivery(
        delivery_id="d-1",
        conversation_id="c-1",
        sender="agent:op:node-1:driver",
        recipient="agent:op:node-1:reader",
        message="hello",
    )

    assert delivery_prompt(delivery) == (
        "[Harness Network message from agent:op:node-1:driver"
        " to agent:op:node-1:reader]\nhello"
    )


def test_surface_runtime_hands_the_worker_both_ends_and_the_origin(
    tmp_path: Path,
    inbox,  # noqa: F811 - the imported fixture
    *,
    actor_host,
) -> None:
    """The daemon runtime is where a stored row becomes a worker delivery;
    dropping ``origin`` there would leave every headless header nameless."""

    class _Capture:
        def __init__(self) -> None:
            self.deliveries: list[HarnessDelivery] = []

        def drain_results(self):  # type: ignore[no-untyped-def]
            return ()

        def streaming_actors(self) -> tuple[str, ...]:
            return ("sprite",)

        def dispatch(self, envelope) -> bool:  # type: ignore[no-untyped-def]
            self.deliveries.append(envelope.input)
            return True

    harnesses = _Capture()
    bridge = DaemonEventBridge(
        state_dir=tmp_path,
        node_id=NODE,
        desired_state=DesiredStateStore(tmp_path / "desired-state.json"),
        transport=_Transport(),  # type: ignore[arg-type]
        inbox=inbox,
        harnesses=harnesses,  # type: ignore[arg-type]
        harness_actor_uri=lambda name: AGENT,
        logger=lambda *args, **kwargs: None, actor_host=actor_host,
        cell_delivery=AdmittingCellDelivery(),
    )
    origin = {
        "chatKind": "dm",
        "provider": "lark",
        "sender": {"kind": "user", "platformId": "ou_x", "displayName": "Allen"},
    }
    assert inbox.receive(
        InboxMessage(
            message_id="m-1",
            conversation_id="c-1",
            sender="adapter:lark:cli-developer",
            recipient=AGENT,
            payload=json.dumps({"message": "hi", "origin": origin}).encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    ).accepted

    bridge._dispatch_harness_deliveries()

    (delivery,) = harnesses.deliveries
    assert delivery.sender == "adapter:lark:cli-developer"
    assert delivery.recipient == AGENT
    assert delivery.origin == origin

