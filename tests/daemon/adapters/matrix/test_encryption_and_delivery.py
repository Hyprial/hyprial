from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.contracts import NativeSubmissionOutcome, PartSubmission, freeze_json_v1
from hyprial.daemon.impl.adapters.matrix import (
    MatrixCryptoBlocked,
    MatrixRateLimited,
    MatrixRuntime,
    matrix_transaction_id,
)

from .support import Credentials, Crypto, StateStore, Transport, instance


def _room_evidence(room_id: str, encrypted: bool) -> str:
    state = "encrypted" if encrypted else "plaintext"
    digest = hashlib.sha256(
        (
            "hyprial-matrix-room-encryption-v1\0"
            f"matrix-main\0{3}\0{room_id}\0{state}"
        ).encode("utf-8")
    ).hexdigest()
    return f"matrix-room-encryption-v1:{state}:{digest}"


def _operation(
    *,
    encrypted: bool,
    key: str = "delivery-key",
    room_id: str | None = None,
    evidence_encrypted: bool | None = None,
    body: str = "secret words",
    content: object | None = None,
) -> SimpleNamespace:
    native_room_id = room_id or ("!encrypted:example" if encrypted else "!plain:example")
    evidence_state = encrypted if evidence_encrypted is None else evidence_encrypted
    return SimpleNamespace(
        kind="text-send",
        idempotency_key=key,
        sealed_request=freeze_json_v1(
            {
                "provider": "matrix",
                "organization_id": "org-a",
                "instance_id": "matrix-main",
                "generation": 3,
                "room_id": native_room_id,
                "event_type": "m.room.message",
                "content": (
                    {"msgtype": "m.text", "body": body}
                    if content is None
                    else content
                ),
                "encrypted": encrypted,
                "room_encryption_evidence": _room_evidence(native_room_id, evidence_state),
                "part_index": 0,
            }
        ),
    )


def test_encrypted_send_without_audited_backend_or_room_keys_fails_closed() -> None:
    async def scenario() -> None:
        for crypto in (None, Crypto(available=False), Crypto(ready=False)):
            transport = Transport([])
            runtime = MatrixRuntime(
                transport, StateStore(), Credentials(), "receiver:matrix", crypto=crypto
            )
            await runtime.start(instance(), object())
            with pytest.raises(MatrixCryptoBlocked):
                await runtime.submit(_operation(encrypted=True))
            assert transport.send_calls == []
            health = await runtime.health()
            assert health.state == "blocked"
            assert any("e2e" in reason or "keys" in reason for reason in health.reason_codes)

    asyncio.run(scenario())


def test_encrypted_send_never_passes_plaintext_to_transport() -> None:
    async def scenario() -> None:
        transport = Transport([])
        crypto = Crypto()
        runtime = MatrixRuntime(
            transport, StateStore(), Credentials(), "receiver:matrix", crypto=crypto
        )
        await runtime.start(instance(), object())
        outcome = await runtime.submit(_operation(encrypted=True))
        assert outcome.submissions[0].state == "accepted"
        assert len(crypto.encrypt_calls) == 1
        assert transport.send_calls[0][1] == "m.room.encrypted"
        assert "secret words" not in repr(transport.send_calls[0][2])

    asyncio.run(scenario())


def test_authoritative_encrypted_room_rejects_false_sealed_flag_before_send() -> None:
    async def scenario() -> None:
        transport = Transport([])
        runtime = MatrixRuntime(
            transport, StateStore(), Credentials(), "receiver:matrix", crypto=Crypto()
        )
        await runtime.start(instance(), object())
        operation = _operation(
            encrypted=False,
            room_id="!encrypted:example",
            evidence_encrypted=True,
        )
        with pytest.raises(MatrixCryptoBlocked, match="encryption"):
            await runtime.submit(operation)
        assert transport.send_calls == []

    asyncio.run(scenario())


def test_room_encryption_recheck_unavailable_or_changed_makes_zero_send_calls() -> None:
    async def scenario() -> None:
        for result in (TimeoutError("state unavailable"), False):
            transport = Transport([])
            transport.room_encryption_result = result
            runtime = MatrixRuntime(
                transport, StateStore(), Credentials(), "receiver:matrix", crypto=Crypto()
            )
            await runtime.start(instance(), object())
            with pytest.raises(MatrixCryptoBlocked, match="encryption"):
                await runtime.submit(_operation(encrypted=True))
            assert transport.room_encryption_calls == ["!encrypted:example"]
            assert transport.send_calls == []

    asyncio.run(scenario())


def test_plaintext_returning_crypto_is_refused_before_transport() -> None:
    class PlaintextCrypto(Crypto):
        async def encrypt(
            self, room_id: str, event_type: str, content: object, transaction_id: str
        ) -> tuple[str, object]:
            self.encrypt_calls.append((room_id, event_type, content, transaction_id))
            return "m.room.encrypted", content

    async def scenario() -> None:
        transport = Transport([])
        runtime = MatrixRuntime(
            transport,
            StateStore(),
            Credentials(),
            "receiver:matrix",
            crypto=PlaintextCrypto(),
        )
        await runtime.start(instance(), object())
        with pytest.raises(MatrixCryptoBlocked, match="plaintext"):
            await runtime.submit(_operation(encrypted=True))
        assert transport.send_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "ciphertext",
    (
        {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": {"body": "opaque"},
            "device_id": "DEVICE",
            "sender_key": "curve25519:sender",
            "session_id": "megolm-session",
        },
        {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "opaque",
            "device_id": "DEVICE",
            "sender_key": "curve25519:sender",
        },
        {
            "algorithm": "m.olm.v1.curve25519-aes-sha2",
            "ciphertext": "opaque",
            "device_id": "DEVICE",
            "sender_key": "curve25519:sender",
            "session_id": "megolm-session",
        },
        {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "opaque",
            "device_id": "DEVICE",
            "sender_key": "curve25519:sender",
            "session_id": "megolm-session",
            "metadata": {"body": "secret words"},
        },
        {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "opaque",
            "device_id": "DEVICE",
            "sender_key": "secret words",
            "session_id": "megolm-session",
        },
    ),
    ids=(
        "ciphertext-must-be-string",
        "required-session-id",
        "supported-algorithm-only",
        "nested-plaintext-field",
        "plaintext-value-in-required-field",
    ),
)
def test_invalid_or_leaky_megolm_ciphertext_is_refused_before_transport(
    ciphertext: object,
) -> None:
    class CiphertextCrypto(Crypto):
        async def encrypt(
            self, room_id: str, event_type: str, content: object, transaction_id: str
        ) -> tuple[str, object]:
            self.encrypt_calls.append((room_id, event_type, content, transaction_id))
            return "m.room.encrypted", ciphertext

    async def scenario() -> None:
        transport = Transport([])
        runtime = MatrixRuntime(
            transport,
            StateStore(),
            Credentials(),
            "receiver:matrix",
            crypto=CiphertextCrypto(),
        )
        await runtime.start(instance(), object())
        with pytest.raises(MatrixCryptoBlocked, match="plaintext"):
            await runtime.submit(_operation(encrypted=True))
        assert transport.send_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("ciphertext_field", "plaintext_fragment"),
    (
        ("ciphertext", "secret words"),
        ("device_id", "m.text"),
        ("sender_key", "$thread-root"),
        ("session_id", "nested-label"),
    ),
)
def test_wrapped_recursive_plaintext_is_refused_in_every_megolm_string_field(
    ciphertext_field: str,
    plaintext_fragment: str,
) -> None:
    plaintext = {
        "msgtype": "m.text",
        "body": "secret words",
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root"},
        "info": {"labels": ["outer-label", ["nested-label"]]},
    }

    class WrappedPlaintextCrypto(Crypto):
        async def encrypt(
            self, room_id: str, event_type: str, content: object, transaction_id: str
        ) -> tuple[str, object]:
            self.encrypt_calls.append((room_id, event_type, content, transaction_id))
            ciphertext = {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "opaque",
                "device_id": "DEVICE",
                "sender_key": "curve25519:sender",
                "session_id": "megolm-session",
            }
            ciphertext[ciphertext_field] = f"prefix:{plaintext_fragment}:suffix"
            return "m.room.encrypted", ciphertext

    async def scenario() -> None:
        transport = Transport([])
        runtime = MatrixRuntime(
            transport,
            StateStore(),
            Credentials(),
            "receiver:matrix",
            crypto=WrappedPlaintextCrypto(),
        )
        await runtime.start(instance(), object())
        with pytest.raises(MatrixCryptoBlocked, match="plaintext"):
            await runtime.submit(_operation(encrypted=True, content=plaintext))
        assert transport.send_calls == []

    asyncio.run(scenario())


def test_transaction_id_is_stable_rate_limit_is_retryable_and_unknown_only_reconciles() -> None:
    async def scenario() -> None:
        transport = Transport([])
        state = StateStore()
        runtime = MatrixRuntime(transport, state, Credentials(), "receiver:matrix", crypto=None)
        await runtime.start(instance(), object())
        operation = _operation(encrypted=False, key="same-durable-attempt")
        expected_txn = "hyprial-" + hashlib.sha256(
            b"hyprial-matrix-transaction-v1\0same-durable-attempt"
        ).hexdigest()
        assert matrix_transaction_id("same-durable-attempt") == expected_txn
        assert matrix_transaction_id("different-attempt") != expected_txn

        transport.send_result = MatrixRateLimited(2500)
        limited = await runtime.submit(operation)
        assert limited.submissions[0].state == "failed-retryable"
        assert limited.retry_after_ms == 2500
        assert transport.send_calls[-1][3] == expected_txn

        transport.send_result = TimeoutError("response lost after write")
        unknown = await runtime.submit(operation)
        assert unknown.submissions[0].state == "unknown"
        assert unknown.submissions[0].definitely_not_sent is False
        assert transport.send_calls[-1][3] == expected_txn

        transport.reconcile_result = {"event_id": "$accepted-late"}
        reconciled = await runtime.reconcile(operation, unknown)
        assert reconciled.submissions[0].state == "accepted"
        assert state.outbound_by_txn[expected_txn].event_id == "$accepted-late"
        assert len(transport.send_calls) == 2

        replay_runtime = MatrixRuntime(
            transport, state, Credentials(), "receiver:matrix", crypto=None
        )
        await replay_runtime.start(instance(), object())
        replay = await replay_runtime.reconcile(operation, unknown)
        assert replay.submissions[0].provider_message_id == "$accepted-late"
        assert len(transport.send_calls) == 2
        replay_submit = await replay_runtime.submit(operation)
        assert replay_submit.submissions[0].provider_message_id == "$accepted-late"
        assert len(transport.send_calls) == 2

    asyncio.run(scenario())


def test_transaction_replay_with_changed_payload_conflicts_without_send() -> None:
    async def scenario() -> None:
        transport = Transport([])
        state = StateStore()
        runtime = MatrixRuntime(
            transport, state, Credentials(), "receiver:matrix", crypto=None
        )
        await runtime.start(instance(), object())
        first = await runtime.submit(_operation(encrypted=False, key="same-key", body="first"))
        assert first.submissions[0].state == "accepted"
        with pytest.raises(ValueError, match="payload conflict"):
            await runtime.submit(_operation(encrypted=False, key="same-key", body="changed"))
        assert len(transport.send_calls) == 1

    asyncio.run(scenario())


def test_unknown_is_durable_and_restart_is_reconcile_only_never_resend() -> None:
    async def scenario() -> None:
        transport = Transport([])
        state = StateStore()
        transport.send_result = TimeoutError("response lost")
        operation = _operation(encrypted=False, key="restart-unknown")
        runtime = MatrixRuntime(
            transport, state, Credentials(), "receiver:matrix", crypto=None
        )
        await runtime.start(instance(), object())
        unknown = await runtime.submit(operation)
        assert unknown.submissions[0].state == "unknown"
        transaction_id = matrix_transaction_id("restart-unknown")
        stored = state.outbound_by_txn[transaction_id]
        assert stored.outcome == "unknown"
        assert stored.payload_digest

        transport.send_result = {"event_id": "$must-not-resend"}
        restarted = MatrixRuntime(
            transport, state, Credentials(), "receiver:matrix", crypto=None
        )
        await restarted.start(instance(), object())
        replay = await restarted.submit(operation)
        assert replay.submissions[0].state == "unknown"
        assert len(transport.send_calls) == 1

        transport.reconcile_result = {"event_id": "$found"}
        accepted = await restarted.reconcile(operation, replay)
        assert accepted.submissions[0].provider_message_id == "$found"
        assert len(transport.send_calls) == 1
        assert transport.reconcile_calls == [("!plain:example", transaction_id)]

    asyncio.run(scenario())


def test_accepted_prior_never_calls_external_reconcile_even_without_local_record() -> None:
    async def scenario() -> None:
        transport = Transport([])
        transport.reconcile_result = {"event_id": "$external"}
        runtime = MatrixRuntime(
            transport, StateStore(), Credentials(), "receiver:matrix", crypto=None
        )
        await runtime.start(instance(), object())
        accepted = NativeSubmissionOutcome(
            (PartSubmission(0, "accepted", "$already", False, None),), None
        )
        assert await runtime.reconcile(_operation(encrypted=False), accepted) == accepted
        assert transport.reconcile_calls == []

    asyncio.run(scenario())


def test_reconcile_preserves_partial_unknown_when_provider_has_no_evidence() -> None:
    async def scenario() -> None:
        transport = Transport([])
        runtime = MatrixRuntime(
            transport, StateStore(), Credentials(), "receiver:matrix", crypto=None
        )
        await runtime.start(instance(), object())
        prior = NativeSubmissionOutcome(
            (PartSubmission(0, "unknown", None, False, "matrix-submit-unknown"),), None
        )
        assert await runtime.reconcile(_operation(encrypted=False), prior) == prior
        assert transport.send_calls == []
        assert transport.reconcile_calls == []

    asyncio.run(scenario())


def test_empty_outcome_never_calls_external_reconcile() -> None:
    async def scenario() -> None:
        transport = Transport([])
        state = StateStore()
        runtime = MatrixRuntime(
            transport, state, Credentials(), "receiver:matrix", crypto=None
        )
        await runtime.start(instance(), object())
        operation = _operation(encrypted=False, key="empty-reconcile")
        transport.send_result = TimeoutError("response lost")
        unknown = await runtime.submit(operation)
        assert unknown.submissions[0].state == "unknown"

        transport.reconcile_result = None
        empty = NativeSubmissionOutcome((), None)
        assert await runtime.reconcile(operation, empty) == empty
        assert transport.reconcile_calls == []

    asyncio.run(scenario())
