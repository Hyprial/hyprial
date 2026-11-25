"""P3 attempt service: send association, current facts and compensation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from hyprial.daemon.impl.application.onboarding.first_run.service import (
    AgentSelection,
    FirstRunAttemptService,
    FirstRunAuthorityError,
    RequestBinding,
)
from hyprial.daemon.impl.onboarding.first_run import (
    FirstRunReply,
    FirstRunStore,
    FirstRunStoreError,
    FirstRunVerificationError,
    FIRST_RUN_CONFIGURATION_CHANGED,
    FIRST_RUN_REPLY_UNVERIFIED,
)
from hyprial.daemon.impl.onboarding.first_run_attempt import (
    FIRST_RUN_ATTEMPT_CONFLICT,
    FirstRunAttemptStore,
    FirstRunCredentialBinding,
)
from hyprial.identity import IdentityTransactionBusy, IdentityTransactionLock, OnboardingSubject

ISSUER = "https://issuer.example.test"
USER_ID = "user-123"
DEVICE_UID = "12345678123442348234123456789abc"
SENDER = f"user:{USER_ID}"
AGENT = "agent:owner@node:squire"
CONVERSATION = "c.0123456789abcdef"
REQUEST = "m.request-0123456789"
REPLY = "m.reply-0123456789"
KEY = "opkey-0123456789abcdef"
NEW_KEY = "opkey-fedcba9876543210"


def _subject() -> OnboardingSubject:
    return OnboardingSubject(issuer=ISSUER, user_id=USER_ID, device_uid=DEVICE_UID)


def _key_credential() -> FirstRunCredentialBinding:
    return FirstRunCredentialBinding(
        kind="key",
        grant_id="model-deepseek",
        entry_id="deepseek-official",
        entity_token="entity-1",
        revision=1,
        route_required=False,
    )


def _selection(credential: FirstRunCredentialBinding | None = None) -> AgentSelection:
    return AgentSelection(
        agent_uri=AGENT,
        agent_created_at_ms=1_760_000_000_000,
        provider="deepseek",
        model="deepseek-flash",
        harness="pi",
        config_revision="rev-1",
        credential=credential or _key_credential(),
    )


def _reply(**overrides) -> FirstRunReply:
    values = {
        "message_id": REPLY,
        "reply_to": REQUEST,
        "sender": AGENT,
        "recipient": SENDER,
        "conversation_id": CONVERSATION,
        "intent": "reply",
        "received_at_ms": 1_760_000_000_100,
    }
    values.update(overrides)
    return FirstRunReply(**values)


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        root = tmp_path / "onboarding"
        self.receipts = FirstRunStore(root)
        self.attempts = FirstRunAttemptStore(root)
        self.epoch = 1
        self.selection: AgentSelection | None = _selection()
        self.bindings: dict[tuple[str, str, str, str], RequestBinding] = {}
        self.replies: dict[str, FirstRunReply] = {}
        self.sent: list[str] = []
        self.service = FirstRunAttemptService(
            home=tmp_path,
            onboarding_root=root,
            attempts=self.attempts,
            receipts=self.receipts,
            login_epoch=lambda _home: self.epoch,
            select_agent=lambda _uri: self.selection,
            send_binding=lambda sender, recipient, conversation, key: self.bindings.get(
                (sender, recipient, conversation, key)
            ),
            reply_for=lambda _sender, _recipient, request, _conversation: self.replies.get(request),
        )

    def begin(self, operation_key: str = KEY):
        return self.service.begin(
            _subject(), operation_key=operation_key, agent_uri=AGENT,
        )

    def bind(self, attempt, *, key: str = KEY, request: str = REQUEST):
        self.bindings[(SENDER, AGENT, CONVERSATION, attempt.send_key)] = RequestBinding(
            message_id=request,
            conversation_id=CONVERSATION,
            idempotency_key=attempt.send_key,
        )
        return self.service.bind(
            _subject(),
            trusted_sender=SENDER,
            attempt_id=attempt.attempt_id,
            expected_revision=attempt.revision,
            operation_key=key,
            conversation_id=CONVERSATION,
            request_message_id=request,
        )

    def confirm(self, attempt, *, key: str = KEY, receipt_revision: int = 0):
        return self.service.confirm(
            _subject(),
            trusted_sender=SENDER,
            attempt_id=attempt.attempt_id,
            expected_attempt_revision=attempt.revision,
            expected_receipt_revision=receipt_revision,
            operation_key=key,
        )


def _ready(tmp_path: Path) -> tuple[_Harness, object, object]:
    harness = _Harness(tmp_path)
    begun = harness.begin()
    bound = harness.bind(begun)
    harness.replies[REQUEST] = _reply()
    return harness, begun, bound


@pytest.mark.parametrize("stage", ["receipt", "attempt"])
def test_identity_writer_cannot_advance_epoch_during_confirmation(tmp_path, monkeypatch, stage):
    harness, _begun, bound = _ready(tmp_path)
    owner = harness.receipts if stage == "receipt" else harness.attempts
    method = "confirm_many" if stage == "receipt" else "mark_confirmed"
    original = getattr(owner, method)
    writer_blocked = []

    def try_relogin():
        try:
            with IdentityTransactionLock.acquire(tmp_path, timeout=0):
                harness.epoch = 2
        except IdentityTransactionBusy:
            writer_blocked.append(True)

    def commit(*args, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(try_relogin).result(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, method, commit)
    result = harness.confirm(bound)

    assert writer_blocked == [True]
    assert harness.epoch == 1
    assert result.attempt.state == "confirmed"
    with IdentityTransactionLock.acquire(tmp_path, timeout=0):
        harness.epoch = 2
    with pytest.raises(FirstRunVerificationError) as stale:
        harness.confirm(bound)
    assert stale.value.code == FIRST_RUN_CONFIGURATION_CHANGED


@pytest.mark.parametrize("recover_receipt", [False, True])
def test_confirmation_refuses_an_active_identity_writer(tmp_path, recover_receipt):
    harness, _begun, bound = _ready(tmp_path)
    if recover_receipt:
        harness.receipts.confirm_many(
            _subject(), steps=("configuration", "reply"), expected_revision=0, operation_key=KEY,
        )
    before = harness.receipts.read(_subject())
    with IdentityTransactionLock.acquire(tmp_path, timeout=0):
        with pytest.raises(FirstRunVerificationError) as busy:
            harness.confirm(bound)
        assert busy.value.code == FIRST_RUN_CONFIGURATION_CHANGED
        assert harness.receipts.read(_subject()) == before
        assert harness.attempts.read(_subject()) == bound
        harness.epoch = 2
    with pytest.raises(FirstRunVerificationError):
        harness.confirm(bound)
    assert harness.receipts.read(_subject()) == before
    assert harness.attempts.read(_subject()) == bound


def test_begin_bind_confirm_verifies_the_durable_reply(tmp_path):
    harness, begun, bound = _ready(tmp_path)
    result = harness.confirm(bound)

    assert result.first_run.confirmed == ("configuration", "reply")
    assert result.attempt.state == "confirmed"
    assert result.attempt.reply_message_id == REPLY
    assert result.request_message_id == REQUEST
    assert result.reply_message_id == REPLY
    assert begun.send_key == bound.send_key


def test_bind_rejects_a_request_without_the_attempt_send_key(tmp_path):
    harness = _Harness(tmp_path)
    begun = harness.begin()
    harness.bindings[(SENDER, AGENT, CONVERSATION, "frs-other-0123456789")] = RequestBinding(
        message_id=REQUEST,
        conversation_id=CONVERSATION,
        idempotency_key="frs-other-0123456789",
    )
    with pytest.raises(FirstRunAuthorityError) as raised:
        harness.service.bind(
            _subject(),
            trusted_sender=SENDER,
            attempt_id=begun.attempt_id,
            expected_revision=begun.revision,
            operation_key=KEY,
            conversation_id=CONVERSATION,
            request_message_id=REQUEST,
        )
    assert raised.value.code == FIRST_RUN_REPLY_UNVERIFIED
    assert harness.attempts.read(_subject()).state == "pending"


def test_confirm_rejects_a_historical_receipt_from_another_operation(tmp_path):
    harness = _Harness(tmp_path)
    harness.receipts.confirm_many(
        _subject(),
        steps=("configuration", "reply"),
        expected_revision=0,
        operation_key=NEW_KEY,
        now="2026-11-18T00:00:00Z",
    )
    begun = harness.begin(operation_key=KEY)
    bound = harness.bind(begun)
    harness.replies[REQUEST] = _reply()

    with pytest.raises(FirstRunStoreError) as raised:
        harness.confirm(bound)
    assert raised.value.code == FIRST_RUN_ATTEMPT_CONFLICT
    assert harness.attempts.read(_subject()) == bound
    assert harness.receipts.read(_subject()).revision == 1


def test_confirm_rejects_a_reply_that_answers_an_older_request(tmp_path):
    harness = _Harness(tmp_path)
    begun = harness.begin()
    bound = harness.bind(begun)
    harness.replies[REQUEST] = _reply(reply_to="m.older-request-0123456789")

    with pytest.raises(FirstRunVerificationError) as raised:
        harness.confirm(bound)
    assert raised.value.code == FIRST_RUN_REPLY_UNVERIFIED
    assert harness.receipts.read(_subject()).state == "none"
    assert harness.attempts.read(_subject()).state == "bound"


def test_both_message_reads_receive_the_exact_trusted_agent(tmp_path):
    harness = _Harness(tmp_path)
    begun = harness.begin()
    request_selectors = []
    reply_selectors = []
    read_request = harness.service._send_binding
    read_reply = harness.service._reply_for

    def request(*selectors):
        request_selectors.append(selectors)
        return read_request(*selectors)

    def reply(*selectors):
        reply_selectors.append(selectors)
        return read_reply(*selectors)

    harness.service._send_binding = request
    harness.service._reply_for = reply
    bound = harness.bind(begun)
    harness.replies[REQUEST] = _reply()
    harness.confirm(bound)
    harness.confirm(bound)
    assert request_selectors == [(SENDER, AGENT, CONVERSATION, begun.send_key)]
    assert reply_selectors == [(AGENT, SENDER, REQUEST, CONVERSATION)] * 2


def test_same_account_relogin_invalidates_the_attempt(tmp_path):
    harness, _begun, bound = _ready(tmp_path)
    harness.epoch = 2

    with pytest.raises(FirstRunVerificationError) as raised:
        harness.confirm(bound)
    assert raised.value.code == FIRST_RUN_CONFIGURATION_CHANGED
    assert harness.receipts.read(_subject()).state == "none"


def test_agent_incarnation_or_configuration_change_invalidates(tmp_path):
    harness, _begun, bound = _ready(tmp_path)
    harness.selection = AgentSelection(
        agent_uri=AGENT,
        agent_created_at_ms=1_760_000_000_001,
        provider="deepseek",
        model="deepseek-flash",
        harness="pi",
        config_revision="rev-1",
        credential=_key_credential(),
    )
    with pytest.raises(FirstRunVerificationError) as raised:
        harness.confirm(bound)
    assert raised.value.code == FIRST_RUN_CONFIGURATION_CHANGED


def test_native_auth_selection_is_snapshotted_and_verified(tmp_path):
    harness = _Harness(tmp_path)
    credential = FirstRunCredentialBinding(
        kind="native",
        import_id="import-0123456789",
        imported_at_ms=1_760_000_000_000,
        manifest="manifest-0123456789",
    )
    harness.selection = AgentSelection(
        agent_uri=AGENT,
        agent_created_at_ms=1_760_000_000_000,
        provider=None,
        model=None,
        harness="pi",
        config_revision=None,
        credential=credential,
    )
    begun = harness.begin()
    bound = harness.bind(begun)
    harness.replies[REQUEST] = _reply()

    result = harness.confirm(bound)
    assert result.first_run.confirmed == ("configuration", "reply")
    assert result.attempt.credential.kind == "native"


def test_response_loss_retry_after_the_receipt_write_recovers(tmp_path):
    harness, _begun, bound = _ready(tmp_path)
    harness.receipts.confirm_many(
        _subject(),
        steps=("configuration", "reply"),
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )

    result = harness.confirm(bound)
    assert result.first_run.confirmed == ("configuration", "reply")
    assert result.attempt.state == "confirmed"
    assert result.attempt.reply_message_id == REPLY


def test_timeout_without_a_reply_never_confirms(tmp_path):
    harness = _Harness(tmp_path)
    begun = harness.begin()
    bound = harness.bind(begun)

    with pytest.raises(FirstRunVerificationError) as raised:
        harness.confirm(bound)
    assert raised.value.code == FIRST_RUN_REPLY_UNVERIFIED
    assert harness.receipts.read(_subject()).state == "none"
    assert harness.attempts.read(_subject()).state == "bound"


def test_a_new_begin_does_not_resend_and_keeps_the_same_send_key(tmp_path):
    harness = _Harness(tmp_path)
    first = harness.begin()
    second = harness.begin()
    assert second.attempt_id == first.attempt_id
    assert second.send_key == first.send_key
    assert harness.sent == []


def test_confirm_after_cancel_is_refused(tmp_path):
    harness, begun, bound = _ready(tmp_path)
    harness.service.cancel(
        _subject(),
        attempt_id=begun.attempt_id,
        expected_revision=bound.revision,
        operation_key=KEY,
    )
    with pytest.raises(FirstRunStoreError):
        harness.confirm(bound)
    assert harness.receipts.read(_subject()).state == "none"


def test_cancel_cannot_relabel_a_partially_committed_confirmation(tmp_path):
    harness, _begun, bound = _ready(tmp_path)
    harness.receipts.confirm_many(
        _subject(), steps=("configuration", "reply"),
        expected_revision=0, operation_key=KEY,
    )
    with pytest.raises(FirstRunStoreError) as raised:
        harness.service.cancel(
            _subject(), attempt_id=bound.attempt_id,
            expected_revision=bound.revision, operation_key=KEY,
        )
    assert raised.value.code == FIRST_RUN_ATTEMPT_CONFLICT
    assert harness.attempts.read(_subject()) == bound


def test_confirmation_recovery_requires_durable_receipt_flush(tmp_path, monkeypatch):
    harness, _begun, bound = _ready(tmp_path)
    harness.receipts.confirm_many(
        _subject(), steps=("configuration", "reply"),
        expected_revision=0, operation_key=KEY,
    )
    before = harness.receipts.path.read_bytes()
    flush = harness.receipts._flush_root

    def fail_flush():
        raise FirstRunStoreError("FIRST_RUN_STORE_COMMIT_UNCONFIRMED", "injected flush failure")

    monkeypatch.setattr(harness.receipts, "_flush_root", fail_flush)
    with pytest.raises(FirstRunStoreError) as raised:
        harness.confirm(bound)
    assert raised.value.code == "FIRST_RUN_STORE_COMMIT_UNCONFIRMED"
    assert harness.attempts.read(_subject()) == bound
    monkeypatch.setattr(harness.receipts, "_flush_root", flush)
    assert harness.confirm(bound).attempt.state == "confirmed"
    assert harness.receipts.path.read_bytes() == before


@pytest.mark.parametrize('interleaving', ['cancel', 'supersede', 'relogin'])
def test_confirmation_does_not_commit_after_its_context_is_invalidated(tmp_path, interleaving):
    harness, _begun, bound = _ready(tmp_path)

    def read_reply(*_args):
        if interleaving == 'cancel':
            harness.service.cancel(
                _subject(), attempt_id=bound.attempt_id,
                expected_revision=bound.revision, operation_key=KEY,
            )
        elif interleaving == 'supersede':
            harness.begin(operation_key=NEW_KEY)
        else:
            harness.epoch += 1
        return _reply()

    harness.service._reply_for = read_reply
    try:
        harness.confirm(bound)
    except (FirstRunStoreError, FirstRunVerificationError):
        pass
    assert harness.receipts.read(_subject()).state == 'none'


@pytest.mark.parametrize('interleaving', ['cancel', 'supersede', 'relogin'])
def test_concurrent_invalidation_settled_before_reply_read_prevents_receipt(tmp_path, interleaving):
    harness, _begun, bound = _ready(tmp_path)
    entered = Event()
    release = Event()
    invalidated = Event()

    def read_reply(*_args):
        entered.set()
        assert release.wait(10)
        return _reply()

    def confirm():
        try:
            harness.confirm(bound)
        except (FirstRunStoreError, FirstRunVerificationError):
            pass

    def invalidate():
        try:
            if interleaving == 'cancel':
                harness.service.cancel(
                    _subject(), attempt_id=bound.attempt_id,
                    expected_revision=bound.revision, operation_key=KEY,
                )
            elif interleaving == 'supersede':
                harness.begin(operation_key=NEW_KEY)
            else:
                harness.epoch += 1
            invalidated.set()
        except (FirstRunStoreError, FirstRunVerificationError):
            pass

    harness.service._reply_for = read_reply
    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmation = pool.submit(confirm)
        assert entered.wait(5)
        invalidation = pool.submit(invalidate)
        settled_before_reply = invalidated.wait(1)
        release.set()
        confirmation.result(timeout=5)
        invalidation.result(timeout=5)
    if settled_before_reply:
        assert harness.receipts.read(_subject()).state == 'none'
