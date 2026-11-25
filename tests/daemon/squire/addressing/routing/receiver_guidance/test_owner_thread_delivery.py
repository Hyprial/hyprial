"""Receiver delivery of another owner's ``user:<owner>`` to that owner's push.

R2 (design ``user-im-binding-removal-tier2`` 8.1, ruling 9.1 item 2) and I7a
(contract i7-0 v2 section 4): the receiving machine hands the request to the
owner push the daemon wires (``_push_owner_copy``, which posts to its user
agent cell), else refuses ``TARGET_UNRESOLVED`` with nothing sent.  There is
no squire DM and no adapter registry.  The wire frames are unchanged.  The
push itself is covered by ``tests/daemon/user_agent/cell/test_cross_owner_receive.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.user_delivery.receiver import (
    NO_USER_AGENT_TARGET_MESSAGE,
    ReceiverUserDelivery,
    UserDeliveryLedger,
    UserDeliveryRequest,
    UserDeliveryResult,
    UserDeliveryTarget,
    is_user_target,
)
from hyprial.kernel import ipc_errors

from ..support import _request


def _owns(owner: str) -> bool:
    return owner == "bob"


def _receiver(tmp_path: Path, owns, push, events: list) -> ReceiverUserDelivery:
    return ReceiverUserDelivery(
        node_id="receiver-mac",
        owns=owns,
        ledger=UserDeliveryLedger(tmp_path / "state" / "user-deliveries.json"),
        owner_push=push,
        logger=lambda level, event, **fields: events.append((event, fields)),
    )


def test_an_owned_delivery_is_handed_to_the_owner_push_and_recorded_once(tmp_path: Path) -> None:
    handed: list[UserDeliveryRequest] = []

    def push(request: UserDeliveryRequest) -> UserDeliveryResult:
        handed.append(request)
        return UserDeliveryResult(request.message_id, True, native_message_id="cell-post")

    events: list = []
    receiver = _receiver(tmp_path, _owns, push, events)

    first = receiver.handle(_request())
    again = receiver.handle(_request())

    assert (first.accepted, first.native_message_id) == (True, "cell-post")
    assert (again.accepted, again.duplicate) == (True, True)
    assert handed == [_request()]
    assert [event for event, _ in events] == ["user-delivery.proxy"]


def test_no_user_agent_target_refuses_with_nothing_sent(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path, _owns, lambda request: None, [])

    result = receiver.handle(_request())

    assert (result.accepted, result.code, result.definitely_not_sent) == (
        False, ipc_errors.TARGET_UNRESOLVED, True,
    )
    assert result.message == NO_USER_AGENT_TARGET_MESSAGE


def test_a_person_this_node_does_not_accept_is_never_handed_off(tmp_path: Path) -> None:
    handed: list[UserDeliveryRequest] = []
    receiver = _receiver(
        tmp_path, lambda owner: False,
        lambda request: handed.append(request), [],
    )

    result = receiver.handle(_request())

    assert (result.accepted, result.code) == (False, ipc_errors.TARGET_UNRESOLVED)
    assert handed == []


def test_the_receiver_no_longer_mints_squire_specific_codes() -> None:
    import ast
    import inspect

    from hyprial.daemon.impl.adapters.user_delivery import receiver as addressing

    tree = ast.parse(inspect.getsource(addressing))
    minted = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("TARGET_SQUIRE_")
    }
    assert minted == set()


def test_mixed_versions_exchange_the_unchanged_v1_result_frame() -> None:
    """PIN (review r2): mixed-version user delivery.

    A receiver's generic code travels in the same v1 frame with the same
    keys.  S3b (2026-11-23) retired the pre-S10 ``TARGET_SQUIRE_*`` codes
    without compatibility reading, so no legacy frame is pinned here.
    """

    import json

    from hyprial.daemon.impl.adapters.user_delivery.receiver import (
        UserDeliveryResult,
        encode_user_result,
    )

    frame = json.loads(encode_user_result(UserDeliveryResult(
        "m-2", False, code=ipc_errors.TARGET_UNRESOLVED, definitely_not_sent=True,
    )))
    assert set(frame) == {
        "schema", "message_id", "accepted", "native_message_id", "duplicate",
        "code", "message", "definitely_not_sent",
    }
    assert frame["schema"] == "hyprial-user-delivery-result/v1"
    assert frame["code"] == "TARGET_UNRESOLVED"


def test_user_target_parser_accepts_first_class_owner_address() -> None:
    target = UserDeliveryTarget.parse("user:gregpaws")

    assert target.owner == "gregpaws"
    assert target.uri == "user:gregpaws"
    assert is_user_target("user:gregpaws") is True
    assert is_user_target("agent:gregpaws:mac:worker") is False
    with pytest.raises(ValueError, match="non-empty owner"):
        UserDeliveryTarget.parse("user:")
