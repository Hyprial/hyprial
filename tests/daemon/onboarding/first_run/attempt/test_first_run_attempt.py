"""P3 attempt storage: one send association, idempotent lifecycle, partitions."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.onboarding.first_run import FirstRunStoreError
from hyprial.daemon.impl.onboarding.first_run_attempt import (
    FIRST_RUN_ATTEMPT_CONFLICT,
    FIRST_RUN_ATTEMPT_NOT_BOUND,
    FIRST_RUN_ATTEMPT_STALE,
    FirstRunAttemptSelection,
    FirstRunAttemptStore,
    FirstRunCredentialBinding,
)
from hyprial.identity import OnboardingSubject

ISSUER = "https://issuer.example.test"
USER_ID = "user-123"
DEVICE_UID = "12345678123442348234123456789abc"
KEY = "opkey-0123456789abcdef"
KEY_2 = "opkey-fedcba9876543210"


def _subject(user_id: str = USER_ID) -> OnboardingSubject:
    return OnboardingSubject(issuer=ISSUER, user_id=user_id, device_uid=DEVICE_UID)


def _selection(
    *,
    login_epoch: int = 1,
    agent_created_at_ms: int = 1_760_000_000_000,
    config_revision: str | None = "rev-1",
) -> FirstRunAttemptSelection:
    return FirstRunAttemptSelection(
        login_epoch=login_epoch,
        agent_uri="agent:owner@node:squire",
        agent_created_at_ms=agent_created_at_ms,
        provider="deepseek",
        model="deepseek-flash",
        harness="pi",
        config_revision=config_revision,
        credential=FirstRunCredentialBinding(
            kind="key",
            grant_id="model-deepseek",
            entry_id="deepseek-official",
            entity_token="entity-1",
            revision=1,
            route_required=False,
        ),
    )


def _store(tmp_path: Path) -> FirstRunAttemptStore:
    return FirstRunAttemptStore(tmp_path / "onboarding")


def test_begin_records_a_server_send_key_and_repeats_idempotently(tmp_path):
    store = _store(tmp_path)
    first = store.begin(
        _subject(), _selection(), operation_key=KEY, receipt_revision=0,
        now="2026-11-18T00:00:00Z",
    )
    assert first.state == "pending"
    assert first.send_key.startswith("frs-")
    assert first.request_message_id is None

    repeated = store.begin(
        _subject(), _selection(), operation_key=KEY, receipt_revision=0,
        now="2026-11-18T01:00:00Z",
    )
    assert repeated == first


def test_same_operation_key_with_another_selection_is_a_conflict(tmp_path):
    store = _store(tmp_path)
    store.begin(
        _subject(), _selection(), operation_key=KEY, receipt_revision=0,
        now="2026-11-18T00:00:00Z",
    )
    with pytest.raises(FirstRunStoreError) as raised:
        store.begin(
            _subject(), _selection(config_revision="rev-2"), operation_key=KEY,
            receipt_revision=0, now="2026-11-18T01:00:00Z",
        )
    assert raised.value.code == FIRST_RUN_ATTEMPT_CONFLICT


def test_bind_records_the_authorized_request_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    attempt = store.begin(
        _subject(), _selection(), operation_key=KEY, receipt_revision=0,
        now="2026-11-18T00:00:00Z",
    )
    bound = store.bind(
        _subject(), attempt_id=attempt.attempt_id,
        expected_revision=attempt.revision, operation_key=KEY,
        request_message_id="m.request-0123456789",
        conversation_id="c.0123456789abcdef",
        now="2026-11-18T01:00:00Z",
    )
    assert bound.state == "bound"
    assert bound.request_message_id == "m.request-0123456789"
    assert bound.send_key == attempt.send_key

    repeated = store.bind(
        _subject(), attempt_id=attempt.attempt_id,
        expected_revision=bound.revision, operation_key=KEY,
        request_message_id="m.request-0123456789",
        conversation_id="c.0123456789abcdef",
        now="2026-11-18T02:00:00Z",
    )
    assert repeated == bound

    with pytest.raises(FirstRunStoreError) as raised:
        store.bind(
            _subject(), attempt_id=attempt.attempt_id,
            expected_revision=bound.revision, operation_key=KEY,
            request_message_id="m.other-request-0123456789",
            conversation_id="c.0123456789abcdef",
            now="2026-11-18T03:00:00Z",
        )
    assert raised.value.code == FIRST_RUN_ATTEMPT_CONFLICT


def test_a_stale_attempt_revision_cannot_bind(tmp_path):
    store = _store(tmp_path)
    attempt = store.begin(
        _subject(), _selection(), operation_key=KEY, receipt_revision=0,
        now="2026-11-18T00:00:00Z",
    )
    with pytest.raises(FirstRunStoreError) as raised:
        store.bind(
            _subject(), attempt_id=attempt.attempt_id, expected_revision=0,
            operation_key=KEY, request_message_id="m.request-0123456789",
            conversation_id="c.0123456789abcdef",
            now="2026-11-18T01:00:00Z",
        )
    assert raised.value.code == FIRST_RUN_ATTEMPT_STALE


def test_confirm_requires_a_bound_attempt_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    attempt = store.begin(
        _subject(), _selection(), operation_key=KEY, receipt_revision=0,
        now="2026-11-18T00:00:00Z",
    )
    with pytest.raises(FirstRunStoreError) as raised:
        store.mark_confirmed(
            _subject(), attempt_id=attempt.attempt_id,
            expected_revision=attempt.revision, operation_key=KEY,
            now="2026-11-18T01:00:00Z",
        )
    assert raised.value.code == FIRST_RUN_ATTEMPT_NOT_BOUND

    bound = store.bind(
        _subject(), attempt_id=attempt.attempt_id,
        expected_revision=attempt.revision, operation_key=KEY,
        request_message_id="m.request-0123456789",
        conversation_id="c.0123456789abcdef",
        now="2026-11-18T01:00:00Z",
    )
    confirmed = store.mark_confirmed(
        _subject(), attempt_id=attempt.attempt_id,
        expected_revision=bound.revision, operation_key=KEY,
        now="2026-11-18T02:00:00Z",
    )
    assert confirmed.state == "confirmed"
    repeated = store.mark_confirmed(
        _subject(), attempt_id=attempt.attempt_id,
        expected_revision=bound.revision, operation_key=KEY,
        now="2026-11-18T03:00:00Z",
    )
    assert repeated == confirmed


def test_a_new_begin_supersedes_the_previous_attempt(tmp_path):
    store = _store(tmp_path)
    old = store.begin(
        _subject(), _selection(), operation_key=KEY, receipt_revision=0,
        now="2026-11-18T00:00:00Z",
    )
    new = store.begin(
        _subject(), _selection(), operation_key=KEY_2, receipt_revision=0,
        now="2026-11-18T01:00:00Z",
    )
    assert new.attempt_id != old.attempt_id
    with pytest.raises(FirstRunStoreError) as raised:
        store.bind(
            _subject(), attempt_id=old.attempt_id,
            expected_revision=old.revision, operation_key=KEY,
            request_message_id="m.request-0123456789",
            conversation_id="c.0123456789abcdef",
            now="2026-11-18T02:00:00Z",
        )
    assert raised.value.code == FIRST_RUN_ATTEMPT_CONFLICT


def test_attempts_are_partitioned_by_subject(tmp_path):
    store = _store(tmp_path)
    store.begin(
        _subject(), _selection(), operation_key=KEY, receipt_revision=0,
        now="2026-11-18T00:00:00Z",
    )
    assert store.read(_subject("other-user")) is None


def test_native_auth_binding_is_persisted_without_key_fields(tmp_path):
    store = _store(tmp_path)
    selection = FirstRunAttemptSelection(
        login_epoch=1,
        agent_uri="agent:owner@node:squire",
        agent_created_at_ms=1_760_000_000_000,
        provider=None,
        model=None,
        harness="pi",
        config_revision=None,
        credential=FirstRunCredentialBinding(
            kind="native",
            import_id="import-0123456789",
            imported_at_ms=1_760_000_000_000,
            manifest="manifest-0123456789",
        ),
    )
    attempt = store.begin(
        _subject(), selection, operation_key=KEY, receipt_revision=0,
        now="2026-11-18T00:00:00Z",
    )
    assert attempt.credential.kind == "native"
    assert attempt.credential.import_id == "import-0123456789"


def test_a_corrupt_attempt_file_is_preserved(tmp_path):
    store = _store(tmp_path)
    attempt = store.begin(
        _subject(), _selection(), operation_key=KEY, receipt_revision=0,
        now="2026-11-18T00:00:00Z",
    )
    store.path.write_text("{not json", encoding="utf-8")
    before = store.path.read_bytes()
    assert store.read(_subject()) is None
    with pytest.raises(FirstRunStoreError):
        store.bind(
            _subject(), attempt_id=attempt.attempt_id,
            expected_revision=attempt.revision, operation_key=KEY,
            request_message_id="m.request-0123456789",
            conversation_id="c.0123456789abcdef",
            now="2026-11-18T01:00:00Z",
        )
    assert store.path.read_bytes() == before


def test_the_attempt_status_carries_no_secret_or_message_body(tmp_path):
    store = _store(tmp_path)
    attempt = store.begin(
        _subject(), _selection(), operation_key=KEY, receipt_revision=0,
        now="2026-11-18T00:00:00Z",
    )
    status = attempt.as_status()
    assert set(status) == {
        "attemptId", "revision", "state", "operationKey", "sendKey",
        "receiptRevision", "loginEpoch", "agentUri", "agentCreatedAtMs",
        "provider", "model", "harness", "configRevision", "credentialKind",
        "requestMessageId", "conversationId", "replyMessageId",
    }
    assert not any(name in status for name in ("message", "body", "text", "secret", "apiKey"))
