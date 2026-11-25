import threading
import time
from dataclasses import replace
from types import SimpleNamespace

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.adapters.user_delivery.receiver import UserDeliveryRequest, UserDeliveryResult
from hyprial.daemon.impl.adapters.user_delivery.actor import UserDeliveryActor


class _Gateways:
    """The per-owner native senders behind the fake owner push (the user agent cell)."""

    def __init__(self, gateways):
        self.gateways = dict(gateways)

    def push(self, request):
        gateway = self.gateways[request.owner]
        try:
            native = gateway.deliver(
                f"ou_{request.owner}", request.message,
                idempotency_key=request.idempotency_key,
            )
        except Exception as error:  # noqa: BLE001 - mirrors a native send failure
            return UserDeliveryResult(
                request.message_id, False, code="USER_DELIVERY_FAILED",
                message=str(error), definitely_not_sent=False,
            )
        return UserDeliveryResult(request.message_id, True, native_message_id=native)

    def close(self, timeout):
        return all(
            gateway.close(timeout)
            for gateway in self.gateways.values()
            if callable(getattr(gateway, "close", None))
        )


def _real_receiver(tmp_path, gateways):
    """A real receiver whose owner push (I7a) is a native send per owner."""

    from hyprial.daemon.impl.adapters.user_delivery.receiver import (
        ReceiverUserDelivery,
        UserDeliveryLedger,
    )

    native = _Gateways(gateways)
    receiver = ReceiverUserDelivery(
        node_id="receiver",
        owns=lambda owner: owner in gateways,
        ledger=UserDeliveryLedger(tmp_path / "deliveries.json"),
        owner_push=native.push,
    )
    receiver.adapters = native
    return receiver


def _wait_receipt(actor, attempt):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        receipt = actor.receipt(attempt)
        if receipt is not None:
            return receipt
        time.sleep(0.005)
    raise AssertionError(f"no receipt for {attempt}")


def test_real_receiver_blocked_gateway_does_not_block_other_owner(
    tmp_path, actor_host
):
    entered, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return "native-a"

    first = SimpleNamespace(deliver=blocked)
    second = SimpleNamespace(deliver=lambda *a, **kw: "native-b")
    receiver = _real_receiver(tmp_path, {"alice": first, "bob": second})
    actor = UserDeliveryActor(receiver, actor_host=actor_host)
    request = UserDeliveryRequest("a", "idem-a", "alice", "sender", "text", "c", "a")
    try:
        assert actor.admit(request) is AdmissionResult.ACCEPTED
        assert entered.wait(1)
        assert (
            actor.admit(
                replace(
                    request,
                    message_id="b",
                    idempotency_key="idem-b",
                    owner="bob",
                    attempt_id="b",
                )
            )
            is AdmissionResult.ACCEPTED
        )
        assert _wait_receipt(actor, "b").native_message_id == "native-b"
        # An expired SDK caller budget cannot manufacture a durable rejection
        # while that native delivery is still accepted and running.
        time.sleep(0.03)
        assert actor.receipt("a") is None
        assert actor._ledger_owner.get("idem-a") is None
        assert not actor.close(0.01)
    finally:
        release.set()
        assert actor.close(2)
        assert receiver.adapters.close(2)
    assert actor.receipt("a").accepted
    assert receiver.ledger.get("idem-a").native_message_id == "native-a"


def test_ledger_outcome_unknown_does_not_keep_delivery_pending(
    tmp_path, actor_host
):
    release = threading.Event()
    native_calls: list[str] = []
    record_calls: list[str] = []

    def send(*_args, **kwargs):
        native_calls.append(kwargs["idempotency_key"])
        return "native-once"

    gateway = SimpleNamespace(deliver=send)
    receiver = _real_receiver(tmp_path, {"alice": gateway})

    def blocked_record(key, _result):
        record_calls.append(key)
        assert release.wait(3.0)
        raise RuntimeError("late ledger failure")

    receiver.ledger.record = blocked_record
    actor = UserDeliveryActor(receiver, actor_host=actor_host)
    actor._ledger_owner._completion_timeout = 0.02
    request = UserDeliveryRequest(
        "message", "stable-key", "alice", "sender", "text", "conversation", "attempt"
    )
    try:
        assert actor.admit(request) is AdmissionResult.ACCEPTED
        receipt = _wait_receipt(actor, "attempt")
        assert not receipt.accepted
        assert receipt.code == "SUBMIT_OUTCOME_UNKNOWN"
        assert actor.snapshot().pending == 0
        assert native_calls == ["stable-key"]
        assert record_calls == ["stable-key"]
    finally:
        release.set()
        assert actor.close(2.0)
        assert receiver.adapters.close(2.0)


def test_native_delivery_stall_keeps_receipts_and_mailbox_responsive(actor_host):
    entered, release = threading.Event(), threading.Event()

    class Receiver:
        def owns(self, owner):
            return True

        def handle(self, request):
            entered.set()
            assert release.wait(3)
            return UserDeliveryResult(
                request.message_id, True, native_message_id="native"
            )

    actor = UserDeliveryActor(Receiver(), capacity=1, actor_host=actor_host)
    request = UserDeliveryRequest(
        "message", "idem", "owner", "sender", "text", "conversation", "attempt"
    )
    try:
        assert actor.admit(request) is AdmissionResult.ACCEPTED
        assert entered.wait(1)
        assert actor.receipt("attempt") is None
        assert actor.snapshot().pending == 1
        assert not actor.close(0.01)
        assert actor.admit(request) is AdmissionResult.CLOSED
    finally:
        release.set()
        assert actor.close(2)
    assert actor.receipt("attempt").accepted


def test_delivery_exception_settles_and_does_not_kill_authority(actor_host):
    class Receiver:
        def owns(self, owner):
            return True

        def handle(self, request):
            raise OSError("synthetic-secret should not reach receipt")

    actor = UserDeliveryActor(Receiver(), actor_host=actor_host)
    request = UserDeliveryRequest(
        "message", "idem", "owner", "sender", "text", "conversation", "attempt"
    )
    try:
        actor.admit(request)
        deadline = time.monotonic() + 1
        while actor.receipt("attempt") is None and time.monotonic() < deadline:
            time.sleep(0.005)
        receipt = actor.receipt("attempt")
        assert receipt is not None and not receipt.accepted
        assert receipt.message == "OSError"
        assert actor.snapshot().pending == 0
    finally:
        assert actor.close(2)


def test_endpoint_registration_failure_drains_allocated_authority(
    monkeypatch, actor_host
):
    import pytest
    from hyprial.daemon.impl.adapters.user_delivery import receiver as addressing

    owners = []
    closed = []
    real = addressing.UserDeliveryActor

    def construct(receiver, **kwargs):
        result = real(receiver, **kwargs)
        owners.append(result)
        return result

    def subscribe(*args):
        raise OSError("synthetic declaration failure")

    monkeypatch.setattr(addressing, "UserDeliveryActor", construct)
    session = SimpleNamespace(
        declare_queryable=lambda *args: SimpleNamespace(
            close=lambda: closed.append(True)
        ),
        subscribe=subscribe,
    )
    receiver = SimpleNamespace(owns=lambda owner: True, handle=lambda request: None)
    with pytest.raises(OSError, match="declaration failure"):
        addressing.ZenohUserDeliveryEndpoint(
            session, receiver, actor_host=actor_host
        )
    assert closed == [True]
    assert owners[0].snapshot().closed
    assert owners[0].close(0)


def test_native_success_ledger_failure_is_outcome_unknown_without_resend(
    tmp_path, monkeypatch, actor_host
):
    calls = []
    receiver = _real_receiver(tmp_path, {"alice": SimpleNamespace(
        deliver=lambda *a, **kw: calls.append(kw["idempotency_key"]) or "native"
    )})
    failed, allow_failure = threading.Event(), threading.Event()
    def record_after_recovery(key, result):
        failed.set()
        assert allow_failure.wait(3.0)
        raise OSError("synthetic unavailable disk")

    monkeypatch.setattr(receiver.ledger, "record", record_after_recovery)
    actor = UserDeliveryActor(receiver, actor_host=actor_host)
    request = UserDeliveryRequest("a", "stable", "alice", "sender", "text", "c", "a")
    try:
        actor.admit(request)
        assert failed.wait(1)
        actor.admit(replace(request, attempt_id="b"))
        deadline = time.monotonic() + 1.0
        while not actor._followers and time.monotonic() < deadline:
            time.sleep(0.005)
        assert actor._followers
        allow_failure.set()
        first = _wait_receipt(actor, "a")
        repeated = _wait_receipt(actor, "b")
        assert first.code == repeated.code == "SUBMIT_OUTCOME_UNKNOWN"
        assert repeated.duplicate
        assert calls == ["stable"]
    finally:
        allow_failure.set()
        assert actor.close(2)
        assert receiver.adapters.close(2)
    assert calls == ["stable"]
    assert receiver.ledger.get("stable") is None


def test_outcome_unknown_result_stays_exactly_once_through_actor(
    tmp_path, actor_host
):
    calls = []
    receiver = _real_receiver(tmp_path, {"alice": SimpleNamespace(
        deliver=lambda *a, **kw: "unused"
    )})
    def outcome_unknown(request):
        calls.append(request.idempotency_key)
        return UserDeliveryResult(
            request.message_id,
            False,
            code="SUBMIT_OUTCOME_UNKNOWN",
            definitely_not_sent=False,
        )

    receiver.owner_push = outcome_unknown
    actor = UserDeliveryActor(receiver, actor_host=actor_host)
    request = UserDeliveryRequest("a", "stable", "alice", "sender", "text", "c", "a")
    try:
        actor.admit(request)
        first = _wait_receipt(actor, "a")
        actor.admit(replace(request, attempt_id="b"))
        repeated = _wait_receipt(actor, "b")
    finally:
        assert actor.close(2)
        assert receiver.adapters.close(2)

    assert not first.accepted and not first.duplicate
    assert not repeated.accepted and repeated.duplicate
    assert calls == ["stable"]


def test_delivery_certainty_survives_receipt_wire():
    from hyprial.daemon.impl.adapters.user_delivery.receiver import encode_user_result, decode_user_result

    for certain in (False, True):
        result = UserDeliveryResult("id", False, definitely_not_sent=certain)
        assert decode_user_result(encode_user_result(result)) == result
    # Older receivers carry no proof of non-delivery.
    assert not decode_user_result(
        b'{"schema":"hyprial-user-delivery-result/v1","message_id":"id","accepted":false}'
    ).definitely_not_sent
