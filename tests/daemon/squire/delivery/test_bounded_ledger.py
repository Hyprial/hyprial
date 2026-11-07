from __future__ import annotations

import threading

from hyprial.daemon.impl.squire.addressing import (
    UserDeliveryLedger,
    UserDeliveryResult,
)
from hyprial.daemon.impl.squire.actors.ledger import (
    LedgerCommandReceipt,
    LedgerOutcomeUnknown,
    UserDeliveryLedgerAuthority,
)
from tests.support.network_owner_context import OwnerContextTracer


def test_ledger_timeout_keeps_one_command_and_recovers_its_durable_receipt(
    tmp_path,
) -> None:
    store = UserDeliveryLedger(tmp_path / "user-deliveries.json")
    authority = UserDeliveryLedgerAuthority(store)
    authority._completion_timeout = 0.05
    result = UserDeliveryResult("message", True, native_message_id="native")
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    original = store.record

    def blocked_record(key: str, value: UserDeliveryResult):
        calls.append(key)
        entered.set()
        assert release.wait(3.0)
        return original(key, value)

    store.record = blocked_record
    outcome: list[object] = []

    def record() -> None:
        try:
            outcome.append(authority.record("stable-key", result))
        except BaseException as error:  # noqa: BLE001 - asserted as receipt below
            outcome.append(error)

    caller = threading.Thread(target=record)
    try:
        with OwnerContextTracer().installed():
            caller.start()
            assert entered.wait(1.0)
            caller.join(0.5)
        assert not caller.is_alive(), "ledger completion waited without a bound"
        assert len(outcome) == 1
        error = outcome[0]
        assert isinstance(error, TimeoutError)
        receipt = getattr(error, "receipt", None)
        assert receipt is not None
        assert receipt.key == "stable-key"
        assert calls == ["stable-key"]

        release.set()
        caller.join(3.0)
        assert authority.close(3.0)

        restarted = UserDeliveryLedgerAuthority(store)
        try:
            assert restarted.recover(receipt) == result
            assert calls == ["stable-key"]
        finally:
            assert restarted.close(3.0)
    finally:
        release.set()
        caller.join(3.0)
        authority.close(3.0)


def test_mark_duplicate_recovery_does_not_accept_a_nonduplicate_projection(
    tmp_path,
) -> None:
    store = UserDeliveryLedger(tmp_path / "user-deliveries.json")
    original = UserDeliveryResult("message", True, native_message_id="native")
    store.record("stable-key", original)
    authority = UserDeliveryLedgerAuthority(store)
    try:
        receipt = LedgerCommandReceipt(
            "unknown-mark-operation", "mark-duplicate", "stable-key"
        )
        try:
            authority.recover(receipt)
        except LedgerOutcomeUnknown as error:
            assert error.receipt == receipt
        else:
            raise AssertionError("nonduplicate projection certified mark completion")
    finally:
        assert authority.close(3.0)


def test_record_recovery_requires_the_exact_intended_result(tmp_path) -> None:
    store = UserDeliveryLedger(tmp_path / "user-deliveries.json")
    existing = UserDeliveryResult("message", True, native_message_id="first")
    intended = UserDeliveryResult("message", True, native_message_id="second")
    store.record("stable-key", existing)
    authority = UserDeliveryLedgerAuthority(store)
    try:
        receipt = LedgerCommandReceipt(
            "unknown-record-operation", "record", "stable-key", intended
        )
        try:
            authority.recover(receipt)
        except LedgerOutcomeUnknown as error:
            assert error.receipt == receipt
        else:
            raise AssertionError("different record certified operation completion")
    finally:
        assert authority.close(3.0)
