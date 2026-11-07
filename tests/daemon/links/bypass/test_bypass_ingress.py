"""Emergency bypass (``HYPRIAL_LINKS_BYPASS``): remote ingress and dispatch.

Today a remote agent sender is authorized at ingress by its link file in the
shared org space (LINK_UNKNOWN otherwise).  Under the bypass that proof is
replaced by org membership: the sender URI's owner must be a member (or the
owner) of a link space this node reads.  Non-members, bare (node-shaped)
senders and unparseable agents stay refused.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.kernel import ipc_errors

from tests.support.links.org import FakeOrgFs, LogRecorder, make_service

A = "agent:o:n1:a"
MEMBER = "agent:p:n2:x"
STRANGER = "agent:q:n3:y"
NODE = "n1"
RESERVED = frozenset({"dispatch-coordinator", "_turn-hooks"})


def _members(space_id: str) -> frozenset[str]:
    return frozenset({"user:o", "user:p"}) if space_id == "s1" else frozenset()


def harness(*, bypass: bool, members_of=_members, logger=None):
    fs = FakeOrgFs()
    fs.add_space("s1")
    h = make_service(
        fs=fs, local_names=("a",), bypass=bypass, members_of=members_of,
        logger=logger,
    )
    h.tick()
    return h


# -- (b) sender_known ------------------------------------------------------


def test_member_without_a_link_file_is_unknown_with_the_switch_off() -> None:
    h = harness(bypass=False)
    assert h.service.sender_known(recipient=A, sender=MEMBER) is False


def test_member_without_a_link_file_is_known_with_the_switch_on() -> None:
    h = harness(bypass=True)
    assert h.service.sender_known(recipient=A, sender=MEMBER) is True


def test_non_member_stays_unknown_with_the_switch_on() -> None:
    h = harness(bypass=True)
    assert h.service.sender_known(recipient=A, sender=STRANGER) is False


def test_unreadable_membership_fails_closed() -> None:
    def broken(space_id: str) -> frozenset[str]:
        raise RuntimeError("members unreadable")

    h = harness(bypass=True, members_of=broken)
    assert h.service.sender_known(recipient=A, sender=MEMBER) is False


def test_ingress_bypass_is_logged_from_the_service_thread_only() -> None:
    logs = LogRecorder()
    h = harness(bypass=True, logger=logs)
    assert h.service.sender_known(recipient=A, sender=MEMBER) is True
    # The inbox actor thread must not do log I/O: the line is queued and the
    # next pass writes it.
    assert logs.fields_for("link.bypass_admitted") == []
    h.tick()
    fields = logs.fields_for("link.bypass_admitted")
    assert len(fields) == 1
    assert fields[0]["actor"] == A
    assert fields[0]["peer"] == MEMBER
    assert fields[0]["wouldRefuseCode"] == ipc_errors.LINK_UNKNOWN
    assert fields[0]["direction"] == "ingress"


# -- dispatch (recipient side) ------------------------------------------


def test_unlinked_dispatch_holds_off_and_admits_on() -> None:
    for bypass in (False, True):
        h = harness(bypass=bypass)
        admitted = h.service.admit_dispatch(
            recipient=A, sender=MEMBER, message_id="d1",
            now_ms=h.clock.now_ms(),
        )
        assert admitted is bypass


# -- (b) end to end through the real inbox ingress ----------------------


def _message(message_id: str, sender: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender=sender,
        recipient=A,
        payload=json.dumps({"message": "hi"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


@pytest.fixture(params=[False, True], ids=["bypass-off", "bypass-on"])
def assembled(
    request, tmp_path: Path
) -> Iterator[tuple[bool, DeliveryCustodyFacade]]:
    bypass = bool(request.param)
    h = harness(bypass=bypass)
    slot = LinkAdmission(internal_actors=RESERVED)
    slot.bind(h.service)
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        type("_E", (), {"publish": lambda self, event: None})(),
        node_id=NODE,
        service_options={"link_admission": slot},
    )
    facade = DeliveryCustodyFacade(coordinator, tmp_path / "inbox.sqlite3")
    try:
        yield bypass, facade
    finally:
        facade.close()


def test_ingress_member_is_refused_off_and_admitted_on(assembled) -> None:
    bypass, facade = assembled
    result = facade.receive(_message("m-member", MEMBER), now_ms=10)
    if bypass:
        assert result.accepted is True
        assert facade.has_received("m-member") is True
    else:
        assert result.accepted is False
        assert result.code == ipc_errors.LINK_UNKNOWN


def test_ingress_non_member_is_refused_either_way(assembled) -> None:
    _bypass, facade = assembled
    result = facade.receive(_message("m-stranger", STRANGER), now_ms=10)
    assert result.accepted is False
    assert result.code == ipc_errors.LINK_UNKNOWN


def test_ingress_bare_sender_is_refused_either_way(assembled) -> None:
    _bypass, facade = assembled
    result = facade.receive(_message("m-bare", "n2"), now_ms=10)
    assert result.accepted is False
    assert facade.has_received("m-bare") is False
