"""S3c: one node accepts a cross-owner user delivery; every other node stays silent.

Decision notes: the acceptor rule is ``owner == app.owner`` (strict) and this
node's user agent cell yields at least one push target.  The predicate is
``accepts_owner_copy(app, owner)`` in ``send_targets/user.py``; the transport
wiring passes it as ``ReceiverUserDelivery(owns=...)``.  A node that does not
accept answers nothing, so the sender times out exactly as it does for a home
that never had a profile.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.user_delivery.actor import UserDeliveryActor
from hyprial.daemon.impl.adapters.user_delivery.receiver import (
    ReceiverUserDelivery,
    UserDeliveryLedger,
    UserDeliveryRequest,
    UserDeliveryResult,
)
from hyprial.daemon.impl.application.messaging.inbox_surface.send_targets.user import (
    accepts_owner_copy,
)
from hyprial.daemon.impl.cells.model import CellMember
from hyprial.kernel.impl.actor_runtime import AdmissionResult
from tests.daemon.conversations.user_target.user_target_support import boot_with_user_agent
from tests.support.golden.app import ADAPTER, golden_daemon as golden_daemon
from tests.support.identity.cell_push import edit_gateway, install_cell, user_agent_cell

DM = "oc_dm_chat"
SENDER = "agent:bob:bob-mac:helper"


def _booted(golden_daemon: Any, monkeypatch: pytest.MonkeyPatch, **cell: Any) -> Any:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    edit_gateway(booted.app, defaultRoute="dm")
    members = cell.pop("members", {"im": ADAPTER})
    document = user_agent_cell(booted.app, members, **cell)
    install_cell(booted.app, document)
    booted.cell = document
    return booted


def _receiver(app: Any, tmp_path: Path, owns: Any, pushes: list[str]) -> ReceiverUserDelivery:
    def push(request: UserDeliveryRequest) -> UserDeliveryResult | None:
        pushes.append(request.idempotency_key)
        return app._push_owner_copy(request)

    return ReceiverUserDelivery(
        node_id=app.node_id,
        ledger=UserDeliveryLedger(tmp_path / "user-deliveries.json"),
        owner_push=push,
        owns=owns,
    )


def _record_logs(app: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    logged: list[tuple[Any, ...]] = []

    def record(level: str, component: str, event: str, **fields: Any) -> None:
        logged.append((level, component, event, fields))

    monkeypatch.setattr(app, "_log", record)
    return logged


def _not_accepted(logged: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [entry for entry in logged if entry[2] == "user-delivery.not-accepted"]


def _request(owner: str, key: str = "so-1") -> UserDeliveryRequest:
    return UserDeliveryRequest(
        message_id=f"m-{key}", idempotency_key=key, owner=owner,
        sender=SENDER, message="hello", conversation_id="conv-so", attempt_id=key,
    )


def _wait_receipt(actor: UserDeliveryActor, attempt: str, timeout: float = 3.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        receipt = actor.receipt(attempt)
        if receipt is not None:
            return receipt
        time.sleep(0.005)
    return None


def _settle(actor: UserDeliveryActor) -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and actor.snapshot().pending:
        time.sleep(0.005)
    time.sleep(0.1)


def test_own_owner_with_an_adapter_target_accepts(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch)

    assert accepts_owner_copy(booted.app, booted.app.owner) is True


def test_own_owner_without_a_target_is_silent(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch, members={})

    assert accepts_owner_copy(booted.app, booted.app.owner) is False


def test_foreign_owner_is_silent_even_with_targets(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch)

    assert accepts_owner_copy(booted.app, "bob") is False
    assert accepts_owner_copy(booted.app, booted.app.owner.upper() + "x") is False
    assert accepts_owner_copy(booted.app, "") is False


def test_unreadable_cell_is_silent(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    runtime = booted.app._cell_runtime

    def boom(_cell_id: str) -> Any:
        raise OSError("cell document unreadable")

    monkeypatch.setattr(runtime, "lookup", boom)
    logged = _record_logs(booted.app, monkeypatch)

    assert accepts_owner_copy(booted.app, booted.app.owner) is False
    assert _not_accepted(logged) == [("warn", "user-delivery", "user-delivery.not-accepted",
                                      {"owner": booted.app.owner, "reason": "cell-unavailable"})]


def test_failing_agent_listing_is_silent_and_logged(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch)

    def boom() -> Any:
        raise OSError("agents registry unreadable")

    monkeypatch.setattr(booted.app.agents, "list", boom)
    logged = _record_logs(booted.app, monkeypatch)

    assert accepts_owner_copy(booted.app, booted.app.owner) is False
    assert _not_accepted(logged) == [("warn", "user-delivery", "user-delivery.not-accepted",
                                      {"owner": booted.app.owner, "reason": "cell-unavailable",
                                       "errorType": "OSError"})]


def test_missing_target_and_foreign_owner_log_nothing(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch, members={})
    logged = _record_logs(booted.app, monkeypatch)

    assert accepts_owner_copy(booted.app, booted.app.owner) is False
    assert accepts_owner_copy(booted.app, "bob") is False
    assert _not_accepted(logged) == []


def test_cell_with_a_non_adapter_member_is_silent(golden_daemon, monkeypatch) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    broken = replace(booted.cell, members=(*booted.cell.members, CellMember("w", "worker")))
    install_cell(booted.app, broken)

    assert accepts_owner_copy(booted.app, booted.app.owner) is False


def test_accepted_delivery_is_pushed_once_through_the_actor(
    golden_daemon, monkeypatch, tmp_path, actor_host
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    pushes: list[str] = []
    receiver = _receiver(app, tmp_path, lambda owner: accepts_owner_copy(app, owner), pushes)
    actor = UserDeliveryActor(receiver, actor_host=actor_host)
    try:
        assert actor.admit(_request(app.owner)) is AdmissionResult.ACCEPTED
        receipt = _wait_receipt(actor, "so-1")
        assert receipt is not None and receipt.accepted

        assert actor.admit(replace(_request(app.owner), attempt_id="so-1b")) is AdmissionResult.ACCEPTED
        again = _wait_receipt(actor, "so-1b")
        assert again is not None and again.duplicate
    finally:
        assert actor.close(3)

    assert pushes == ["so-1"]
    assert len(booted.daemon.gateway.calls) == 1
    assert booted.daemon.gateway.calls[0][0] == DM


def test_own_owner_without_target_gives_no_receipt_and_no_push(
    golden_daemon, monkeypatch, tmp_path, actor_host
) -> None:
    booted = _booted(golden_daemon, monkeypatch, members={})
    app = booted.app
    pushes: list[str] = []
    receiver = _receiver(app, tmp_path, lambda owner: accepts_owner_copy(app, owner), pushes)
    actor = UserDeliveryActor(receiver, actor_host=actor_host)
    try:
        actor.admit(_request(app.owner))
        _settle(actor)
        assert actor.receipt("so-1") is None
    finally:
        assert actor.close(3)

    assert pushes == []
    assert booted.daemon.gateway.calls == []
    assert receiver.ledger.get("so-1") is None


def test_foreign_owner_gives_no_receipt_and_no_push_even_with_targets(
    golden_daemon, monkeypatch, tmp_path, actor_host
) -> None:
    booted = _booted(golden_daemon, monkeypatch)
    app = booted.app
    pushes: list[str] = []
    receiver = _receiver(app, tmp_path, lambda owner: accepts_owner_copy(app, owner), pushes)
    actor = UserDeliveryActor(receiver, actor_host=actor_host)
    try:
        actor.admit(_request("bob"))
        _settle(actor)
        assert actor.receipt("so-1") is None
    finally:
        assert actor.close(3)

    assert pushes == []
    assert booted.daemon.gateway.calls == []


def test_receiver_owns_is_exactly_the_injected_predicate(tmp_path) -> None:
    seen: list[str] = []

    def owns(owner: str) -> bool:
        seen.append(owner)
        return owner == "alice"

    receiver = ReceiverUserDelivery(
        node_id="n", ledger=UserDeliveryLedger(tmp_path / "d.json"),
        owner_push=lambda request: None, owns=owns,
    )

    assert receiver.owns("alice") is True
    assert receiver.owns("bob") is False
    assert seen == ["alice", "bob"]
    assert not hasattr(receiver, "profiles")


def test_prepare_delivery_passes_the_owner_through_without_profiles(
    tmp_path, actor_host
) -> None:
    from hyprial.daemon.impl.adapters.user_delivery.actor import (
        DeliveryPrepared,
        PrepareDelivery,
    )

    receiver = ReceiverUserDelivery(
        node_id="n", ledger=UserDeliveryLedger(tmp_path / "d.json"),
        owner_push=lambda request: None, owns=lambda owner: owner == "alice",
    )
    actor = UserDeliveryActor(receiver, actor_host=actor_host)
    try:
        class Command:
            request = _request("alice")

        assert actor._execute(PrepareDelivery(Command)) == DeliveryPrepared("alice")
        Command.request = _request("bob")
        assert actor._execute(PrepareDelivery(Command)) == DeliveryPrepared(None)
    finally:
        assert actor.close(3)
