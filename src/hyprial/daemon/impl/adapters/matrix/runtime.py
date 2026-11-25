"""Matrix /sync, checkpoint, encryption and delivery state machine."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ..contracts import (
    AdapterCloseOutcome,
    AdapterHealth,
    AdapterInstanceRef,
    CustodyReceipt,
    HydratedEvent,
    IngressObservation,
    IngressSink,
    IngressUnit,
    NativeAlias,
    NativeCheckpoint,
    NativeSubmissionOutcome,
    NativeTargetResolution,
    OperationNode,
    PartSubmission,
    PlatformEventEnvelope,
    RuntimeSession,
    canonical_json_v1,
    freeze_json_v1,
    frozen_json_digest_v1,
)
from .codec import CAPABILITY_REVISION
from .errors import MatrixCryptoBlocked, MatrixDefinitelyFailed, MatrixRateLimited
from .models import (
    MatrixCredentialResolver,
    MatrixCredentials,
    MatrixCryptoBackend,
    MatrixOutboundRecord,
    MatrixStateStore,
    MatrixTransport,
    crypto_descriptor_is_reviewed,
    freeze_room_encryption_evidence,
    read_room_encryption_evidence,
)


MAX_SYNC_EVENTS = 1024
SYNC_TIMEOUT_MS = 30_000


def matrix_transaction_id(idempotency_key: str) -> str:
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("Matrix idempotency key is required")
    digest = hashlib.sha256(
        b"hyprial-matrix-transaction-v1\0" + idempotency_key.encode("utf-8")
    ).hexdigest()
    return f"hyprial-{digest}"


def _map(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Matrix {name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Matrix {name} is required")
    return value


def _thaw_frozen(value: object) -> object:
    return json.loads(canonical_json_v1(value))


def _event_semantic_kind(event: Mapping[str, object]) -> str:
    content = event.get("content")
    relation = content.get("m.relates_to") if isinstance(content, Mapping) else None
    relation_type = relation.get("rel_type") if isinstance(relation, Mapping) else None
    if relation_type == "m.replace":
        return "edit"
    if event.get("type") == "m.reaction" or relation_type == "m.annotation":
        return "reaction"
    return "create"


def _ciphertext_is_opaque(plaintext: object, ciphertext: object) -> bool:
    """Reject crypto results that preserve plaintext message structure or values."""

    if not isinstance(ciphertext, Mapping):
        return False
    required_fields = {
        "algorithm",
        "ciphertext",
        "device_id",
        "sender_key",
        "session_id",
    }
    if set(ciphertext) != required_fields:
        return False
    if ciphertext.get("algorithm") != "m.megolm.v1.aes-sha2":
        return False
    if any(
        not isinstance(ciphertext.get(name), str) or not ciphertext[name]
        for name in required_fields - {"algorithm"}
    ):
        return False
    plaintext_fields = {
        "body",
        "file",
        "format",
        "formatted_body",
        "info",
        "m.relates_to",
        "msgtype",
        "url",
    }

    def contains_plaintext_field(value: object) -> bool:
        if isinstance(value, Mapping):
            return any(
                key in plaintext_fields or contains_plaintext_field(child)
                for key, child in value.items()
            )
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return any(contains_plaintext_field(child) for child in value)
        return False

    def string_values(value: object) -> tuple[str, ...]:
        output: list[str] = []
        if isinstance(value, str) and value:
            output.append(value)
        elif isinstance(value, Mapping):
            for child in value.values():
                output.extend(string_values(child))
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for child in value:
                output.extend(string_values(child))
        return tuple(output)

    if contains_plaintext_field(ciphertext):
        return False
    plaintext_json = canonical_json_v1(plaintext)
    ciphertext_json = canonical_json_v1(ciphertext)
    if plaintext_json == ciphertext_json or plaintext_json in ciphertext_json:
        return False
    plaintext_values = string_values(plaintext)
    ciphertext_values = string_values(ciphertext)
    return not any(
        plaintext_value in ciphertext_value
        for plaintext_value in plaintext_values
        for ciphertext_value in ciphertext_values
    )


@dataclass(slots=True)
class _Batch:
    batch_id: str
    next_batch: str
    total: int
    committed: set[int] = field(default_factory=set)
    advanced: bool = False


@dataclass(frozen=True, slots=True)
class _PendingCheckpoint:
    batch_id: str
    index: int
    digest: str


class MatrixRuntime:
    """Auditable Matrix client runtime behind injected transport and crypto ports."""

    def __init__(
        self,
        transport: MatrixTransport,
        state: MatrixStateStore,
        credentials: MatrixCredentialResolver,
        credential_ref: str,
        *,
        crypto: MatrixCryptoBackend | None,
        now_ms: object | None = None,
    ) -> None:
        if not isinstance(credential_ref, str) or not credential_ref:
            raise ValueError("Matrix credential_ref is required")
        self._transport = transport
        self._state = state
        self._credential_resolver = credentials
        self._credential_ref = credential_ref
        self._crypto = crypto
        self._now_ms = now_ms if callable(now_ms) else lambda: int(time.time() * 1000)
        self._instance: AdapterInstanceRef | None = None
        self._credentials: MatrixCredentials | None = None
        self._sync_token: str | None = None
        self._queue: list[IngressUnit] = []
        self._batches: dict[str, _Batch] = {}
        self._pending: dict[str, _PendingCheckpoint] = {}
        self._state_name = "configured"
        self._blocked_reasons: set[str] = set()
        self._unknown_count = 0

    def _block(self, reason: str) -> MatrixCryptoBlocked:
        self._blocked_reasons.add(reason)
        self._state_name = "blocked"
        return MatrixCryptoBlocked(reason)

    def _fence(self, credentials: MatrixCredentials, instance: AdapterInstanceRef) -> None:
        if instance.kind != "matrix" or instance.variant != "client":
            raise ValueError("Matrix runtime requires a matrix/client instance")
        for name, actual, expected in (
            ("instance", credentials.instance_id, instance.instance_id),
            ("organization", credentials.organization_id, instance.organization_id),
            ("generation", credentials.generation, instance.generation),
        ):
            if actual != expected:
                raise ValueError(f"Matrix {name} mismatch")
        expected_fingerprint = hashlib.sha256(
            f"matrix\0{credentials.homeserver}\0{credentials.user_id}".encode()
        ).hexdigest()
        if instance.external_fingerprint != expected_fingerprint:
            raise ValueError("Matrix identity fingerprint mismatch")

    async def start(self, instance: AdapterInstanceRef, sink: IngressSink) -> RuntimeSession:
        del sink
        credentials = await self._credential_resolver.resolve(self._credential_ref)
        self._fence(credentials, instance)
        self._state_name = "starting"
        await self._transport.connect(credentials)
        self._instance = instance
        self._credentials = credentials
        self._sync_token = await self._state.load_sync_token(
            instance.instance_id, instance.generation
        )
        if self._crypto is None:
            self._blocked_reasons.add("matrix-e2e-dependency-unavailable")
            self._state_name = "degraded"
        elif not crypto_descriptor_is_reviewed(self._crypto):
            self._blocked_reasons.add("matrix-e2e-dependency-unreviewed")
            self._state_name = "degraded"
        elif not await self._crypto.prepare(instance.instance_id, instance.generation):
            self._blocked_reasons.add("matrix-e2e-keys-unavailable")
            self._state_name = "degraded"
        else:
            self._state_name = "online"
        return RuntimeSession(
            f"matrix:{instance.instance_id}:{instance.generation}",
            instance.instance_id,
            instance.generation,
            "pull",
        )

    def _require_started(self) -> tuple[AdapterInstanceRef, MatrixCredentials]:
        if self._instance is None or self._credentials is None:
            raise ValueError("Matrix runtime is not started")
        return self._instance, self._credentials

    def _timeline_events(self, response: Mapping[str, object]) -> list[dict[str, object]]:
        _, credentials = self._require_started()
        rooms = _map(response.get("rooms", {}), "sync rooms")
        joined = _map(rooms.get("join", {}), "joined rooms")
        output: list[dict[str, object]] = []
        for room_id, room_value in joined.items():
            if not isinstance(room_id, str) or not room_id:
                raise ValueError("Matrix room id is invalid")
            room = _map(room_value, "joined room")
            timeline = _map(room.get("timeline", {}), "room timeline")
            rows = timeline.get("events", ())
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
                raise ValueError("Matrix timeline events must be a sequence")
            direct = room.get("is_direct") is True
            room_name = room.get("name") if isinstance(room.get("name"), str) else None
            for row in rows:
                event = dict(_map(row, "timeline event"))
                event["_hyprial_room_id"] = room_id
                event["_hyprial_is_direct"] = direct
                event["_hyprial_room_name"] = room_name
                event["_hyprial_homeserver"] = credentials.homeserver
                event["_hyprial_user_id"] = credentials.user_id
                event["_hyprial_device_id"] = credentials.device_id
                event["_hyprial_encrypted"] = event.get("type") == "m.room.encrypted"
                output.append(event)
                if len(output) > MAX_SYNC_EVENTS:
                    raise ValueError("Matrix sync exceeds event bound")
        return output

    def _make_unit(
        self, event: Mapping[str, object], batch_id: str, index: int
    ) -> IngressUnit:
        instance, credentials = self._require_started()
        room_id = _text(event.get("_hyprial_room_id"), "room id")
        account_scope = f"{credentials.homeserver}|{credentials.user_id}"
        event_id = _text(event.get("event_id"), "event id")
        unsigned = event.get("unsigned")
        transaction_id = (
            unsigned.get("transaction_id") if isinstance(unsigned, Mapping) else None
        )
        semantic_kind = _event_semantic_kind(event)
        aliases = [
            NativeAlias(
                instance.instance_id,
                instance.generation,
                account_scope,
                room_id,
                semantic_kind,
                "event",
                event_id,
                "s:0",
                platform="matrix",
                chat_scope=room_id,
            )
        ]
        if semantic_kind == "create":
            aliases.append(
                NativeAlias(
                    instance.instance_id,
                    instance.generation,
                    account_scope,
                    room_id,
                    semantic_kind,
                    "message",
                    event_id,
                    "s:0",
                    platform="matrix",
                    chat_scope=room_id,
                )
            )
        if isinstance(transaction_id, str) and transaction_id:
            aliases.append(
                NativeAlias(
                    instance.instance_id,
                    instance.generation,
                    account_scope,
                    room_id,
                    semantic_kind,
                    "cursor-item",
                    transaction_id,
                    "s:0",
                    platform="matrix",
                    chat_scope=room_id,
                )
            )
        payload = freeze_json_v1(dict(event))
        digest = frozen_json_digest_v1(payload)
        checkpoint_token = f"matrix-sync:{batch_id}:{index}"
        self._pending[checkpoint_token] = _PendingCheckpoint(batch_id, index, digest)
        observation = IngressObservation(
            f"matrix-observation:{hashlib.sha256((batch_id + ':' + str(index)).encode()).hexdigest()}",
            tuple(aliases),
            "poll",
            f"sealed-matrix:{digest}",
            digest,
            self._now_ms(),
        )
        return IngressUnit(
            PlatformEventEnvelope(observation, payload),
            NativeCheckpoint("poll-cursor", checkpoint_token, None),
            event.get("type") == "m.room.encrypted",
        )

    async def _fill_queue(self, since: str | None) -> None:
        response = _map(
            await self._transport.sync(since, timeout_ms=SYNC_TIMEOUT_MS), "sync response"
        )
        next_batch = _text(response.get("next_batch"), "next_batch")
        events = self._timeline_events(response)
        batch_id = hashlib.sha256(
            f"{since or '-'}\0{next_batch}".encode("utf-8")
        ).hexdigest()
        batch = _Batch(batch_id, next_batch, len(events))
        self._batches[batch_id] = batch
        self._queue.extend(
            self._make_unit(event, batch_id, index) for index, event in enumerate(events)
        )
        if not events:
            instance, _ = self._require_started()
            await self._state.commit_sync_token(
                instance.instance_id, instance.generation, next_batch
            )
            batch.advanced = True
            self._sync_token = next_batch

    async def next_ingress(self) -> IngressUnit:
        if not self._queue:
            await self._fill_queue(self._sync_token)
        if not self._queue:
            raise StopAsyncIteration
        return self._queue.pop(0)

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        instance, _ = self._require_started()
        pending = self._pending.get(checkpoint.token or "")
        if pending is None:
            raise ValueError("unknown Matrix checkpoint")
        if custody.envelope_digest != pending.digest:
            raise ValueError("Matrix custody digest mismatch")
        batch = self._batches[pending.batch_id]
        batch.committed.add(pending.index)
        if not batch.advanced and len(batch.committed) == batch.total:
            await self._state.commit_sync_token(
                instance.instance_id, instance.generation, batch.next_batch
            )
            batch.advanced = True
            self._sync_token = batch.next_batch

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        event = _map(_thaw_frozen(envelope.payload), "timeline event")
        if event.get("type") != "m.room.encrypted":
            return HydratedEvent(envelope, event)
        room_id = _text(event.get("_hyprial_room_id"), "room id")
        if self._crypto is None:
            raise self._block("matrix-e2e-dependency-unavailable")
        if not crypto_descriptor_is_reviewed(self._crypto):
            raise self._block("matrix-e2e-dependency-unreviewed")
        if not await self._crypto.room_ready(room_id):
            raise self._block("matrix-e2e-keys-unavailable")
        try:
            decrypted = _map(await self._crypto.decrypt(room_id, event), "decrypted event")
        except Exception as exc:
            raise self._block("matrix-e2e-decryption-failed") from exc
        hydrated = dict(event)
        hydrated["type"] = _text(decrypted.get("type"), "decrypted event type")
        hydrated["content"] = dict(_map(decrypted.get("content"), "decrypted content"))
        hydrated["_hyprial_encrypted"] = True
        return HydratedEvent(envelope, hydrated)

    async def history(
        self, after: NativeCheckpoint | None, limit: int
    ) -> tuple[IngressUnit, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Matrix history limit must be 1..1000")
        token = self._sync_token if after is None else after.token
        if after is not None and after.kind != "poll-cursor":
            raise ValueError("Matrix history requires a poll cursor")
        await self._fill_queue(token)
        selected = tuple(self._queue[:limit])
        del self._queue[:limit]
        return selected

    async def resolve(self, target: object) -> NativeTargetResolution:
        instance, _ = self._require_started()
        chat_ref = target.chat_ref
        if chat_ref.instance != instance:
            return NativeTargetResolution(target, "unauthorized", None, None, "matrix-instance-mismatch")
        try:
            encrypted = await self._transport.room_encryption(chat_ref.native_chat_id)
        except Exception:
            return NativeTargetResolution(
                target,
                "unresolved",
                None,
                None,
                "matrix-room-encryption-state-unavailable",
            )
        if not isinstance(encrypted, bool):
            return NativeTargetResolution(
                target,
                "unresolved",
                None,
                None,
                "matrix-room-encryption-state-invalid",
            )
        if encrypted and (
            self._crypto is None
            or not crypto_descriptor_is_reviewed(self._crypto)
            or not await self._crypto.room_ready(chat_ref.native_chat_id)
        ):
            self._blocked_reasons.add("matrix-e2e-keys-unavailable")
            return NativeTargetResolution(
                target, "unsupported", None, None, "matrix-e2e-unavailable"
            )
        return NativeTargetResolution(
            target,
            "resolved",
            chat_ref.native_chat_id,
            chat_ref.native_thread_id,
            freeze_room_encryption_evidence(
                instance.instance_id,
                instance.generation,
                chat_ref.native_chat_id,
                encrypted,
            ),
        )

    def _sealed_request(self, operation: OperationNode | object) -> dict[str, object]:
        instance, _ = self._require_started()
        sealed = getattr(operation, "sealed_request", None)
        if sealed is None:
            raise ValueError("Matrix sealed request is required")
        request = _map(_thaw_frozen(sealed), "sealed request")
        expected = {
            "provider": "matrix",
            "organization_id": instance.organization_id,
            "instance_id": instance.instance_id,
            "generation": instance.generation,
        }
        for name, value in expected.items():
            if request.get(name) != value:
                raise ValueError(f"Matrix {name} mismatch")
        return dict(request)

    async def _record_outcome(
        self,
        room_id: str,
        transaction_id: str,
        event_id: str | None,
        payload_digest: str,
        outcome: str,
    ) -> None:
        instance, _ = self._require_started()
        await self._state.record_outbound(
            instance.instance_id,
            instance.generation,
            room_id,
            transaction_id,
            event_id,
            payload_digest,
            outcome,
        )

    @staticmethod
    def _payload_digest(
        room_id: str,
        event_type: str,
        content: object,
        transaction_id: str,
        evidence: str,
    ) -> str:
        return frozen_json_digest_v1(
            {
                "room_id": room_id,
                "event_type": event_type,
                "content": content,
                "transaction_id": transaction_id,
                "room_encryption_evidence": evidence,
            }
        )

    @staticmethod
    def _check_replay(
        prior: MatrixOutboundRecord,
        room_id: str,
        payload_digest: str,
    ) -> None:
        if prior.room_id != room_id:
            raise ValueError("Matrix transaction id room conflict")
        if prior.payload_digest != payload_digest:
            raise ValueError("Matrix transaction id payload conflict")

    async def _recheck_room_encryption(
        self, room_id: str, expected_encrypted: bool
    ) -> None:
        try:
            current = await self._transport.room_encryption(room_id)
        except Exception as exc:
            raise self._block("matrix-room-encryption-state-unavailable") from exc
        if not isinstance(current, bool) or current != expected_encrypted:
            raise self._block("matrix-room-encryption-state-mismatch")

    async def submit(self, operation: OperationNode | object) -> NativeSubmissionOutcome:
        instance, _ = self._require_started()
        request = self._sealed_request(operation)
        part_index = request.get("part_index")
        if isinstance(part_index, bool) or not isinstance(part_index, int) or part_index < 0:
            raise ValueError("Matrix part_index must be a non-negative integer")
        if getattr(operation, "kind", None) == "file-upload":
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index,
                        "unsupported",
                        None,
                        True,
                        "matrix-media-upload-port-required",
                    ),
                ),
                None,
            )
        room_id = _text(request.get("room_id"), "room id")
        event_type = _text(request.get("event_type"), "event type")
        content = request.get("content")
        evidence_value = request.get("room_encryption_evidence")
        encrypted = read_room_encryption_evidence(
            evidence_value,
            instance.instance_id,
            instance.generation,
            room_id,
        )
        if not isinstance(request.get("encrypted"), bool):
            raise ValueError("Matrix encrypted flag must be a boolean")
        if request["encrypted"] != encrypted:
            raise self._block("matrix-room-encryption-evidence-mismatch")
        transaction_id = matrix_transaction_id(getattr(operation, "idempotency_key"))
        evidence = _text(evidence_value, "room encryption evidence")
        payload_digest = self._payload_digest(
            room_id, event_type, content, transaction_id, evidence
        )
        prior = await self._state.lookup_outbound_transaction(
            instance.instance_id, instance.generation, transaction_id
        )
        if prior is not None:
            self._check_replay(prior, room_id, payload_digest)
            if prior.outcome == "unknown":
                return NativeSubmissionOutcome(
                    (
                        PartSubmission(
                            part_index,
                            "unknown",
                            None,
                            False,
                            "matrix-submit-unknown",
                        ),
                    ),
                    None,
                )
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index, "accepted", prior.event_id, False, None
                    ),
                ),
                None,
            )
        await self._recheck_room_encryption(room_id, encrypted)
        if encrypted:
            if self._crypto is None:
                raise self._block("matrix-e2e-dependency-unavailable")
            if not crypto_descriptor_is_reviewed(self._crypto):
                raise self._block("matrix-e2e-dependency-unreviewed")
            if not await self._crypto.room_ready(room_id):
                raise self._block("matrix-e2e-keys-unavailable")
            try:
                event_type, content = await self._crypto.encrypt(
                    room_id, event_type, content, transaction_id
                )
            except Exception as exc:
                raise self._block("matrix-e2e-encryption-failed") from exc
            if event_type != "m.room.encrypted":
                raise self._block("matrix-e2e-backend-plaintext-refused")
            if not _ciphertext_is_opaque(request.get("content"), content):
                raise self._block("matrix-e2e-backend-plaintext-refused")
        try:
            response = _map(
                await self._transport.send_event(
                    room_id, event_type, content, transaction_id
                ),
                "send response",
            )
            event_id = _text(response.get("event_id"), "send event id")
        except MatrixRateLimited as exc:
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index, "failed-retryable", None, True, "matrix-rate-limited"
                    ),
                ),
                exc.retry_after_ms,
            )
        except MatrixDefinitelyFailed as exc:
            return NativeSubmissionOutcome(
                (PartSubmission(part_index, "failed-final", None, True, exc.reason_code),),
                None,
            )
        except Exception:
            await self._record_outcome(
                room_id, transaction_id, None, payload_digest, "unknown"
            )
            self._unknown_count += 1
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index, "unknown", None, False, "matrix-submit-unknown"
                    ),
                ),
                None,
            )
        await self._record_outcome(
            room_id, transaction_id, event_id, payload_digest, "accepted"
        )
        return NativeSubmissionOutcome(
            (PartSubmission(part_index, "accepted", event_id, False, None),), None
        )

    async def reconcile(
        self, operation: OperationNode | object, prior: NativeSubmissionOutcome
    ) -> NativeSubmissionOutcome:
        instance, _ = self._require_started()
        request = self._sealed_request(operation)
        room_id = _text(request.get("room_id"), "room id")
        event_type = _text(request.get("event_type"), "event type")
        content = request.get("content")
        evidence_value = request.get("room_encryption_evidence")
        read_room_encryption_evidence(
            evidence_value,
            instance.instance_id,
            instance.generation,
            room_id,
        )
        transaction_id = matrix_transaction_id(getattr(operation, "idempotency_key"))
        payload_digest = self._payload_digest(
            room_id,
            event_type,
            content,
            transaction_id,
            _text(evidence_value, "room encryption evidence"),
        )
        recorded = await self._state.lookup_outbound_transaction(
            instance.instance_id, instance.generation, transaction_id
        )
        if recorded is not None:
            self._check_replay(recorded, room_id, payload_digest)
        if not prior.submissions or any(
            submission.state != "unknown" for submission in prior.submissions
        ):
            return prior
        if recorded is None:
            return prior
        if recorded.outcome == "accepted":
            part_index = prior.submissions[0].part_index
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index, "accepted", recorded.event_id, False, None
                    ),
                ),
                None,
            )
        try:
            response = await self._transport.reconcile_event(room_id, transaction_id)
            if response is None:
                return prior
            result = _map(response, "reconcile response")
            event_id = _text(result.get("event_id"), "reconcile event id")
        except Exception:
            return prior
        await self._record_outcome(
            room_id, transaction_id, event_id, payload_digest, "accepted"
        )
        self._unknown_count = max(0, self._unknown_count - 1)
        part_index = prior.submissions[0].part_index
        return NativeSubmissionOutcome(
            (PartSubmission(part_index, "accepted", event_id, False, None),), None
        )

    async def reconnect(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("Matrix reconnect reason is required")
        _, credentials = self._require_started()
        self._state_name = "reconnecting"
        await self._transport.close()
        await self._transport.connect(credentials)
        self._state_name = "online" if not self._blocked_reasons else "degraded"

    async def health(self) -> AdapterHealth:
        pending_count = sum(not batch.advanced for batch in self._batches.values())
        state = self._state_name
        if state == "blocked" or any(
            reason in self._blocked_reasons
            for reason in (
                "matrix-e2e-keys-unavailable",
                "matrix-e2e-encryption-failed",
                "matrix-e2e-decryption-failed",
                "matrix-e2e-backend-plaintext-refused",
            )
        ):
            state = "blocked"
        return AdapterHealth(
            state,
            CAPABILITY_REVISION,
            pending_count,
            self._unknown_count,
            tuple(sorted(self._blocked_reasons)),
        )

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        del deadline_ms
        await self._transport.close()
        self._state_name = "stopped"
        retained = sum(not batch.advanced for batch in self._batches.values())
        return AdapterCloseOutcome(
            "closed" if retained == 0 and self._unknown_count == 0 else "retained-pending",
            retained,
            self._unknown_count,
            None if retained == 0 and self._unknown_count == 0 else "matrix-work-retained",
        )


__all__ = ["MAX_SYNC_EVENTS", "MatrixRuntime", "SYNC_TIMEOUT_MS", "matrix_transaction_id"]
