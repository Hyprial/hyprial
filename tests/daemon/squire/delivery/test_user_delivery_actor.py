import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.squire.addressing import UserDeliveryRequest, UserDeliveryResult
from hyprial.daemon.impl.squire.actors.delivery import UserDeliveryActor


PROFILE_OWNER = "owner.person@example.test"


def _real_receiver(tmp_path, gateways):
    from hyprial.identity.impl.users.profile import UserProfileStore
    from hyprial.daemon.impl.squire.addressing import (
        ReceiverUserDelivery,
        UserAdapterRegistry,
        UserDeliveryLedger,
    )

    profiles = UserProfileStore(tmp_path / "users.json")
    adapters = UserAdapterRegistry()
    for owner, gateway in gateways.items():
        profiles.ensure(
            owner=owner,
            owner_key=f"key-{owner}",
            login_name=owner,
            machine="receiver",
            machine_key="receiver-key",
        )
        channel = f"channel:{owner}:receiver:squire"
        profiles.set_squire_channel(f"key-{owner}", channel)
        profiles.bind_owner_open_id(
            f"key-{owner}", channel=channel, open_id=f"ou_{owner}"
        )
        adapters.register(channel, gateway)
    return ReceiverUserDelivery(
        node_id="receiver",
        profiles=profiles,
        adapters=adapters,
        ledger=UserDeliveryLedger(tmp_path / "deliveries.json"),
    )


def _wait_receipt(actor, attempt):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        receipt = actor.receipt(attempt)
        if receipt is not None:
            return receipt
        time.sleep(0.005)
    raise AssertionError(f"no receipt for {attempt}")


def _profile(
    owner: str, owner_key: str, squire_channel: str | None = None
) -> dict[str, object]:
    profile = {
        "owner": owner,
        "ownerKey": owner_key,
        "loginName": owner,
        "agents": [],
        "notificationRules": [],
        "preferredReceiver": {
            "machine": "receiver",
            "machineKey": "receiver-key",
        },
    }
    if squire_channel is not None:
        profile["squireChannel"] = squire_channel
    return profile


def _receipt_after_profile_corruption(tmp_path, users):
    receiver = _real_receiver(
        tmp_path,
        {"alice": SimpleNamespace(send_owner_dm=lambda *args, **kwargs: "unused")},
    )
    (tmp_path / "users.json").write_text(
        json.dumps({"version": 1, "users": users}), encoding="utf-8"
    )
    actor = UserDeliveryActor(receiver)
    completions = []
    complete = actor._effects._complete

    def capture(event):
        completions.append(event)
        return complete(event)

    actor._effects._complete = capture
    try:
        request = UserDeliveryRequest(
            "corrupt", "corrupt-key", PROFILE_OWNER, "sender", "text", "c", "corrupt"
        )
        actor.admit(request)
        return _wait_receipt(actor, "corrupt"), completions[-1]
    finally:
        assert actor.close(2)
        assert receiver.adapters.close(2)


def test_two_real_profile_corruptions_have_distinct_details_and_keep_class_name(tmp_path):
    duplicate_receipt, duplicate_completion = _receipt_after_profile_corruption(
        tmp_path / "duplicate-keys",
        [
            _profile(PROFILE_OWNER, "first-key"),
            _profile("other@example.test", "first-key"),
        ],
    )
    legacy_receipt, legacy_completion = _receipt_after_profile_corruption(
        tmp_path / "legacy-channel",
        [_profile(PROFILE_OWNER, "first-key", squire_channel="legacy")],
    )

    for receipt, completion in (
        (duplicate_receipt, duplicate_completion),
        (legacy_receipt, legacy_completion),
    ):
        assert receipt.code == "USER_DELIVERY_FAILED"
        assert receipt.message == "UserProfileError"
        assert completion.error == "UserProfileError"

    assert duplicate_completion.detail == "user profile data contains duplicate owner keys"
    assert (
        legacy_completion.detail
        == "users[0].squireChannel must be a channel:<owner>:<machine>:<channel> URI"
    )


def test_ambiguous_owner_detail_masks_the_owner_email(tmp_path):
    receipt, completion = _receipt_after_profile_corruption(
        tmp_path / "ambiguous-owner",
        [
            _profile(PROFILE_OWNER, "first-key"),
            _profile(PROFILE_OWNER, "second-key"),
        ],
    )

    assert receipt.code == "USER_DELIVERY_FAILED"
    assert receipt.message == "UserProfileError"
    assert completion.error == "UserProfileError"
    assert completion.detail == "user owner <redacted> is ambiguous"
    assert PROFILE_OWNER not in completion.detail


def test_real_receiver_blocked_gateway_does_not_block_other_owner(tmp_path):
    from hyprial.daemon.impl.adapters.lark.outbound.gateway import GatewayIoAuthority

    entered, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return "native-a"

    first = GatewayIoAuthority(SimpleNamespace(send_owner_dm=blocked), timeout=0.01)
    second = GatewayIoAuthority(
        SimpleNamespace(send_owner_dm=lambda *a, **kw: "native-b")
    )
    receiver = _real_receiver(tmp_path, {"alice": first, "bob": second})
    actor = UserDeliveryActor(receiver)
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


def test_ledger_outcome_unknown_does_not_keep_delivery_pending(tmp_path):
    from hyprial.daemon.impl.adapters.lark.outbound.gateway import GatewayIoAuthority

    release = threading.Event()
    native_calls: list[str] = []
    record_calls: list[str] = []

    def send(*_args, **kwargs):
        native_calls.append(kwargs["idempotency_key"])
        return "native-once"

    gateway = GatewayIoAuthority(SimpleNamespace(send_owner_dm=send))
    receiver = _real_receiver(tmp_path, {"alice": gateway})

    def blocked_record(key, _result):
        record_calls.append(key)
        assert release.wait(3.0)
        raise RuntimeError("late ledger failure")

    receiver.ledger.record = blocked_record
    actor = UserDeliveryActor(receiver)
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


def test_canonical_owner_aliases_join_one_native_delivery(tmp_path):
    from hyprial.daemon.impl.adapters.lark.outbound.gateway import GatewayIoAuthority

    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked(*args, **kwargs):
        calls.append(kwargs["idempotency_key"])
        entered.set()
        assert release.wait(4)
        return "native-once"

    gateway = GatewayIoAuthority(SimpleNamespace(send_owner_dm=blocked))
    receiver = _real_receiver(tmp_path, {"alice": gateway})
    actor = UserDeliveryActor(receiver)
    request = UserDeliveryRequest("a", "same", "alice", "sender", "text", "c", "a")
    try:
        actor.admit(request)
        assert entered.wait(1)
        actor.admit(replace(request, owner="key-alice", attempt_id="alias"))
        deadline = time.monotonic() + 1
        while actor.snapshot().pending < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert actor.snapshot().pending == 2
        release.set()
        assert _wait_receipt(actor, "a").accepted
        duplicate = _wait_receipt(actor, "alias")
        assert duplicate.accepted and duplicate.duplicate
        assert duplicate.native_message_id == "native-once"
    finally:
        release.set()
        assert actor.close(2)
        assert receiver.adapters.close(2)
    assert calls == ["same"]
    assert receiver.ledger.get("same").duplicate


def test_native_delivery_stall_keeps_receipts_and_mailbox_responsive():
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

    actor = UserDeliveryActor(Receiver(), capacity=1)
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


def test_delivery_exception_settles_and_does_not_kill_authority():
    class Receiver:
        def owns(self, owner):
            return True

        def handle(self, request):
            raise OSError("synthetic-secret should not reach receipt")

    actor = UserDeliveryActor(Receiver())
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


def test_endpoint_registration_failure_drains_allocated_authority(monkeypatch):
    import pytest
    from hyprial.daemon.impl.squire import addressing

    owners = []
    closed = []
    real = addressing.UserDeliveryActor

    def construct(receiver):
        result = real(receiver)
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
        addressing.ZenohUserDeliveryEndpoint(session, receiver)
    assert closed == [True]
    assert owners[0].snapshot().closed
    assert owners[0].close(0)


def test_native_success_ledger_failure_is_outcome_unknown_without_resend(
    tmp_path, monkeypatch
):
    calls = []
    receiver = _real_receiver(tmp_path, {"alice": SimpleNamespace(
        send_owner_dm=lambda *a, **kw: calls.append(kw["idempotency_key"]) or "native"
    )})
    failed, allow_failure = threading.Event(), threading.Event()
    def record_after_recovery(key, result):
        failed.set()
        assert allow_failure.wait(3.0)
        raise OSError("synthetic unavailable disk")

    monkeypatch.setattr(receiver.ledger, "record", record_after_recovery)
    actor = UserDeliveryActor(receiver)
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


def test_outcome_unknown_result_stays_exactly_once_through_actor(tmp_path):
    calls = []
    receiver = _real_receiver(tmp_path, {"alice": SimpleNamespace(
        send_owner_dm=lambda *a, **kw: "unused"
    )})
    receiver.profiles.set_delivery_agent("key-alice", "alice-proxy")

    def outcome_unknown(_agent, request):
        calls.append(request.idempotency_key)
        return UserDeliveryResult(
            request.message_id,
            False,
            code="SUBMIT_OUTCOME_UNKNOWN",
            definitely_not_sent=False,
        )

    receiver.delivery_agent_delivery = outcome_unknown
    actor = UserDeliveryActor(receiver)
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
    from hyprial.daemon.impl.squire.addressing import encode_user_result, decode_user_result

    for certain in (False, True):
        result = UserDeliveryResult("id", False, definitely_not_sent=certain)
        assert decode_user_result(encode_user_result(result)) == result
    # Older receivers carry no proof of non-delivery.
    assert not decode_user_result(
        b'{"schema":"hyprial-user-delivery-result/v1","message_id":"id","accepted":false}'
    ).definitely_not_sent
