"""Durable provider-attempt custody and settlement algebra."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from hyprial.daemon.impl.adapters.contracts import (
    ChatAuthorizationResourceV1,
    InstanceAuthorizationResourceV1,
    PartSubmission,
    RequiredAuthorizationV1,
    ServiceAuthorizationRefV1,
    TargetReceipt,
    derive_target_receipt,
    reconcile_part_indexes,
    retry_part_indexes,
)
from hyprial.identity import OrganizationAuthorizationLeaseV1


class AuthorizationDenied(ValueError):
    pass


class DeliveryConflict(ValueError):
    pass


class ServiceAuthorizationVerifier(Protocol):
    def __call__(
        self,
        lease: OrganizationAuthorizationLeaseV1,
        *,
        now_ms: int,
        required_scope: tuple[str, ...],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    attempt_id: str
    operation_id: str
    plan_id: str
    node_id: str
    idempotency_key: str
    state: str
    replay: bool = False
    request_digest: str = ""


def _digest(values: object) -> str:
    payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"hyprial-chat-attempt-v1\0" + payload).hexdigest()


def _canonical_parts(required_parts: tuple[int, ...]) -> bytes:
    return json.dumps(required_parts, separators=(",", ":")).encode("ascii")


class DeliveryStore:
    """One ledger for authorization use, attempt identity, and part outcomes."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        service_authorization_verifier: ServiceAuthorizationVerifier | None = None,
    ) -> None:
        self.connection = connection
        self.service_authorization_verifier = service_authorization_verifier
        self._lock = threading.RLock()
        connection.execute("PRAGMA busy_timeout=5000")
        for statement in (
            """CREATE TABLE IF NOT EXISTS chat_authorization_operation_uses (
                lease_id TEXT NOT NULL,
                lease_generation INTEGER NOT NULL,
                use_nonce TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state='decided'),
                PRIMARY KEY(lease_id,lease_generation,use_nonce)
            )""",
            """CREATE TABLE IF NOT EXISTS chat_delivery_attempts (
                attempt_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL UNIQUE,
                plan_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                instance_generation INTEGER NOT NULL,
                binding_generation INTEGER NOT NULL,
                lease_generation INTEGER NOT NULL,
                authorization_use_nonce TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'ready','entering','accepted','failed-final','failed-retryable',
                    'unknown','held-authorization'
                )),
                provider_entries INTEGER NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS chat_delivery_parts (
                attempt_id TEXT NOT NULL REFERENCES chat_delivery_attempts(attempt_id),
                part_index INTEGER NOT NULL,
                state TEXT NOT NULL,
                provider_message_id TEXT,
                definitely_not_sent INTEGER NOT NULL,
                reason_code TEXT,
                PRIMARY KEY(attempt_id,part_index)
            )""",
            """CREATE TABLE IF NOT EXISTS chat_delivery_partitions (
                attempt_id TEXT PRIMARY KEY REFERENCES chat_delivery_attempts(attempt_id),
                required_parts_json BLOB NOT NULL
            )""",
        ):
            connection.execute(statement)
        connection.commit()

    def _existing(self, *, operation_id: str, idempotency_key: str, digest: str) -> DeliveryAttempt | None:
        row = self.connection.execute(
            "SELECT attempt_id,operation_id,plan_id,node_id,idempotency_key,state,request_digest "
            "FROM chat_delivery_attempts WHERE operation_id=? OR idempotency_key=?",
            (operation_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if str(row[6]) == digest:
            return DeliveryAttempt(
                *map(str, row[:6]), replay=True, request_digest=str(row[6])
            )
        if str(row[5]) == "unknown":
            raise DeliveryConflict("CHAT_TARGET_UNKNOWN_RECONCILE_ONLY")
        raise DeliveryConflict("CHAT_DELIVERY_ATTEMPT_CONFLICT")

    def reserve_attempt(
        self,
        *,
        operation_id: str,
        plan_id: str,
        node_id: str,
        idempotency_key: str,
        instance_generation: int,
        binding_generation: int,
        lease_generation: int,
        authorization_use_nonce: str,
        request_digest: str = "",
    ) -> DeliveryAttempt:
        values = (
            operation_id,
            plan_id,
            node_id,
            idempotency_key,
            instance_generation,
            binding_generation,
            lease_generation,
            authorization_use_nonce,
            request_digest,
        )
        digest = _digest(values)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._existing(
                    operation_id=operation_id, idempotency_key=idempotency_key,
                    digest=digest,
                )
                if existing is not None:
                    self.connection.commit()
                    return existing
                attempt_id = f"attempt:{uuid.uuid4()}"
                self.connection.execute(
                "INSERT INTO chat_delivery_attempts "
                "(attempt_id,operation_id,plan_id,node_id,idempotency_key,instance_generation,"
                "binding_generation,lease_generation,authorization_use_nonce,request_digest,state) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'ready')",
                    (attempt_id, *values[:-1], digest),
                )
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
        return DeliveryAttempt(
            attempt_id,
            operation_id,
            plan_id,
            node_id,
            idempotency_key,
            "ready",
            request_digest=digest,
        )

    def authorize_and_reserve(
        self,
        *,
        operation_id: str,
        plan_id: str,
        node_id: str,
        idempotency_key: str,
        instance_generation: int,
        current_instance_generation: int,
        binding_generation: int,
        current_binding_generation: int,
        service_lease_bytes: bytes,
        service_authorization_ref: ServiceAuthorizationRefV1,
        required_service_authorizations: tuple[RequiredAuthorizationV1, ...],
        now_ms: int,
    ) -> DeliveryAttempt:
        if instance_generation != current_instance_generation:
            raise AuthorizationDenied("ADAPTER_GENERATION_STALE")
        if binding_generation != current_binding_generation:
            raise AuthorizationDenied("ADAPTER_BINDING_STALE")
        lease = self._verify_service_authorization(
            service_lease_bytes=service_lease_bytes,
            service_authorization_ref=service_authorization_ref,
            required_service_authorizations=required_service_authorizations,
            plan_id=plan_id,
            instance_generation=instance_generation,
            now_ms=now_ms,
        )
        authorization_use_nonce = service_authorization_ref.use_nonce
        values = (
            operation_id,
            plan_id,
            node_id,
            idempotency_key,
            instance_generation,
            binding_generation,
            lease.generation,
            authorization_use_nonce,
            lease.lease_id,
            service_authorization_ref.lease_digest,
            tuple(row.required_bytes_digest for row in required_service_authorizations),
        )
        digest = _digest(values)
        attempt_id = f"attempt:{uuid.uuid4()}"
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._existing(
                    operation_id=operation_id, idempotency_key=idempotency_key,
                    digest=digest,
                )
                if existing is not None:
                    self.connection.commit()
                    return existing
                self.connection.execute(
                "INSERT INTO chat_authorization_operation_uses VALUES (?,?,?,?,?,'decided')",
                (
                    lease.lease_id,
                    lease.generation,
                    authorization_use_nonce,
                    operation_id,
                    digest,
                ),
            )
                self.connection.execute(
                "INSERT INTO chat_delivery_attempts "
                "(attempt_id,operation_id,plan_id,node_id,idempotency_key,instance_generation,"
                "binding_generation,lease_generation,authorization_use_nonce,request_digest,state) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'ready')",
                (
                    attempt_id,
                    operation_id,
                    plan_id,
                    node_id,
                    idempotency_key,
                    instance_generation,
                    binding_generation,
                    lease.generation,
                    authorization_use_nonce,
                    digest,
                ),
            )
                self.connection.commit()
            except sqlite3.IntegrityError as error:
                self.connection.rollback()
                raise DeliveryConflict("AUTHORIZATION_USE_CONFLICT") from error
            except BaseException:
                self.connection.rollback()
                raise
        return DeliveryAttempt(attempt_id, operation_id, plan_id, node_id, idempotency_key, "ready")

    def _verify_service_authorization(
        self,
        *,
        service_lease_bytes: bytes,
        service_authorization_ref: ServiceAuthorizationRefV1,
        required_service_authorizations: tuple[RequiredAuthorizationV1, ...],
        plan_id: str,
        instance_generation: int,
        now_ms: int,
    ) -> OrganizationAuthorizationLeaseV1:
        try:
            if not isinstance(service_authorization_ref, ServiceAuthorizationRefV1):
                raise ValueError("service authorization ref must be typed")
            required = tuple(required_service_authorizations)
            if (
                len(required) != 2
                or not all(isinstance(row, RequiredAuthorizationV1) for row in required)
                or any(row.authorization_kind != "service" for row in required)
            ):
                raise ValueError("service authorization requires exact typed rows")
            lease = OrganizationAuthorizationLeaseV1.from_bytes(service_lease_bytes)
            wire_digest = hashlib.sha256(service_lease_bytes).hexdigest()
            if (
                service_authorization_ref.use_owner != "daemon-chat-outbox"
                or service_authorization_ref.operation_id != plan_id
                or service_authorization_ref.lease_id != lease.lease_id
                or service_authorization_ref.lease_generation != lease.generation
                or service_authorization_ref.lease_digest != wire_digest
                or service_authorization_ref.use_nonce == ""
            ):
                raise ValueError("service authorization ref mismatch")
            subjects = {row.expected_subject_principal for row in required}
            actions = {row.action for row in required}
            resources = {row.resource.type: row.resource for row in required}
            if (
                subjects != {lease.subject_principal}
                or not lease.subject_principal.startswith("service:daemon:")
                or len(actions) != 1
                or set(resources) != {"instance", "chat"}
                or any(
                    row.source_record_ref != lease.lease_id
                    or row.source_generation != lease.generation
                    or row.use_nonce != service_authorization_ref.use_nonce
                    or row.resource.organization_id != lease.organization_id
                    for row in required
                )
            ):
                raise ValueError("service authorization fields mismatch")
            instance = resources["instance"]
            chat = resources["chat"]
            if (
                not isinstance(instance, InstanceAuthorizationResourceV1)
                or not isinstance(chat, ChatAuthorizationResourceV1)
                or instance.instance_generation != instance_generation
            ):
                raise ValueError("service authorization resource mismatch")
            scope = tuple(
                f"{row.action}:required/{row.required_bytes_digest}" for row in required
            )
            if self.service_authorization_verifier is None:
                raise ValueError("trusted service authorization verifier unavailable")
            self.service_authorization_verifier(
                lease, now_ms=now_ms, required_scope=scope
            )
        except Exception as error:
            raise AuthorizationDenied("SERVICE_AUTHORIZATION_STALE") from error
        return lease

    def enter_provider(
        self,
        attempt_id: str,
        *,
        current_instance_generation: int,
        current_binding_generation: int,
        current_lease_generation: int,
    ) -> bool:
        return self.enter_external_effect(
            attempt_id,
            current_instance_generation=current_instance_generation,
            current_binding_generation=current_binding_generation,
            current_lease_generation=current_lease_generation,
        )

    def enter_external_effect(
        self,
        attempt_id: str,
        *,
        current_instance_generation: int,
        current_binding_generation: int,
        current_lease_generation: int,
    ) -> bool:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    "SELECT instance_generation,binding_generation,lease_generation,state "
                    "FROM chat_delivery_attempts WHERE attempt_id=?", (attempt_id,),
                ).fetchone()
                if row is None:
                    raise DeliveryConflict("CHAT_DELIVERY_ATTEMPT_NOT_FOUND")
                if tuple(map(int, row[:3])) != (
                    current_instance_generation, current_binding_generation,
                    current_lease_generation,
                ):
                    self.connection.execute(
                        "UPDATE chat_delivery_attempts SET state='held-authorization' WHERE attempt_id=?",
                        (attempt_id,),
                    )
                    self.connection.commit()
                    raise AuthorizationDenied("SERVICE_AUTHORIZATION_STALE")
                if str(row[3]) != "ready":
                    self.connection.commit()
                    return False
                changed = self.connection.execute(
                    "UPDATE chat_delivery_attempts SET state='entering',"
                    "provider_entries=provider_entries+1 WHERE attempt_id=? AND state='ready'",
                    (attempt_id,),
                ).rowcount
                self.connection.commit()
                return changed == 1
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def settle_parts(
        self,
        attempt_id: str,
        *,
        required_parts: tuple[int, ...],
        parts: tuple[PartSubmission, ...],
    ) -> None:
        with self._lock, self.connection:
            self._write_parts(attempt_id, required_parts, parts)

    def _write_parts(
        self,
        attempt_id: str,
        required_parts: tuple[int, ...],
        parts: tuple[PartSubmission, ...],
    ) -> None:
        receipt = derive_target_receipt(attempt_id, required_parts, parts)
        terminal = {
            "accepted": "accepted",
            "failed": "failed-final",
            "pending": "failed-retryable",
            "partial-pending": "failed-retryable",
            "unknown": "unknown",
            "partial-unknown": "unknown",
            "partial": "accepted",
            "unsupported": "failed-final",
            "cancelled": "failed-final",
        }[receipt.state]
        self.connection.execute(
            "DELETE FROM chat_delivery_parts WHERE attempt_id=?", (attempt_id,)
        )
        self.connection.execute(
            "INSERT INTO chat_delivery_partitions VALUES (?,?) "
            "ON CONFLICT(attempt_id) DO UPDATE SET required_parts_json=excluded.required_parts_json",
            (attempt_id, _canonical_parts(required_parts)),
        )
        for part in receipt.parts:
            self.connection.execute(
                "INSERT INTO chat_delivery_parts VALUES (?,?,?,?,?,?)",
                (
                    attempt_id,
                    part.part_index,
                    part.state,
                    part.native_message_id,
                    int(part.definitely_not_sent),
                    part.reason_code,
                ),
            )
        self.connection.execute(
            "UPDATE chat_delivery_attempts SET state=? WHERE attempt_id=?",
            (terminal, attempt_id),
        )

    def settle_parts_fenced(
        self,
        attempt_id: str,
        *,
        idempotency_key: str,
        request_digest: str,
        plan_id: str,
        instance_generation: int,
        required_parts: tuple[int, ...],
        parts: tuple[PartSubmission, ...],
        reconcile: bool,
    ) -> TargetReceipt:
        """Settle one exact attempt without permitting a second native entry."""

        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                "SELECT idempotency_key,request_digest,plan_id,instance_generation,state "
                "FROM chat_delivery_attempts "
                "WHERE attempt_id=?",
                (attempt_id,),
                ).fetchone()
                if row is None:
                    raise DeliveryConflict("CHAT_DELIVERY_ATTEMPT_NOT_FOUND")
                if (
                    str(row[0]) != idempotency_key
                    or str(row[1]) != request_digest
                    or str(row[2]) != plan_id
                    or int(row[3]) != instance_generation
                ):
                    raise DeliveryConflict("CHAT_DELIVERY_ATTEMPT_CONFLICT")
                state = str(row[4])
                if reconcile and any(
                    part.state == "accepted" and not part.native_message_id
                    for part in parts
                ):
                    raise DeliveryConflict("CHAT_DELIVERY_RECONCILE_ID_REQUIRED")
                if state not in {"accepted", "failed-final", "failed-retryable"} and not (
                    state == "unknown" and not reconcile
                ):
                    self._write_parts(attempt_id, required_parts, parts)
                receipt = self._target_receipt(attempt_id)
                self.connection.commit()
                return receipt
            except BaseException:
                self.connection.rollback()
                raise

    def target_receipt(self, attempt_id: str) -> TargetReceipt:
        with self._lock:
            return self._target_receipt(attempt_id)

    def _target_receipt(self, attempt_id: str) -> TargetReceipt:
        with self._lock:
            partition = self.connection.execute(
                "SELECT required_parts_json FROM chat_delivery_partitions WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        if partition is None:
            raise DeliveryConflict("CHAT_DELIVERY_REQUIRED_PARTS_MISSING")
        try:
            decoded = json.loads(bytes(partition[0]))
            required_parts = tuple(decoded)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise DeliveryConflict("CHAT_DELIVERY_REQUIRED_PARTS_CORRUPT") from error
        rows = self.connection.execute(
            "SELECT part_index,state,provider_message_id,definitely_not_sent,reason_code "
            "FROM chat_delivery_parts WHERE attempt_id=? ORDER BY part_index",
            (attempt_id,),
        ).fetchall()
        if not rows:
            raise DeliveryConflict("CHAT_DELIVERY_PARTS_MISSING")
        parts = tuple(
            PartSubmission(
                part_index=int(row[0]),
                state=str(row[1]),  # type: ignore[arg-type]
                provider_message_id=None if row[2] is None else str(row[2]),
                definitely_not_sent=bool(row[3]),
                reason_code=None if row[4] is None else str(row[4]),
            )
            for row in rows
        )
        return derive_target_receipt(attempt_id, required_parts, parts)

    def wait_for_receipt(self, attempt_id: str, *, timeout: float = 65.0) -> TargetReceipt:
        """Join the database-owned settlement of an already-entered attempt."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                return self.target_receipt(attempt_id)
            except DeliveryConflict as error:
                if str(error) not in {
                    "CHAT_DELIVERY_REQUIRED_PARTS_MISSING", "CHAT_DELIVERY_PARTS_MISSING",
                }:
                    raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f"delivery attempt {attempt_id} remains unsettled")
            time.sleep(0.005)

    def retry_part_indexes(self, attempt_id: str) -> tuple[int, ...]:
        return retry_part_indexes(self.target_receipt(attempt_id))

    def reconcile_part_indexes(self, attempt_id: str) -> tuple[int, ...]:
        return reconcile_part_indexes(self.target_receipt(attempt_id))

    def provider_entry_count(self, idempotency_key: str) -> int:
        with self._lock:
            row = self.connection.execute(
                "SELECT provider_entries FROM chat_delivery_attempts WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return 0 if row is None else int(row[0])

    def authorization_use_count(self) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM chat_authorization_operation_uses"
        ).fetchone()[0])

    def attempt_count(self) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM chat_delivery_attempts"
        ).fetchone()[0])

    def unknown_count(self, *, plan_id: str | None = None) -> int:
        query = "SELECT count(*) FROM chat_delivery_attempts WHERE state='unknown'"
        values: tuple[str, ...] = ()
        if plan_id is not None:
            query += " AND plan_id=?"
            values = (plan_id,)
        with self._lock:
            return int(self.connection.execute(query, values).fetchone()[0])


def attempt_authorized_send(value: dict[str, object]) -> dict[str, object]:
    if value.get("authorization") == "expired":
        return {
            "ok": False,
            "code": "SERVICE_AUTHORIZATION_EXPIRED",
            "useRows": 0,
            "attemptRows": 0,
            "providerCalls": 0,
        }
    return {"ok": True, "code": None, "useRows": 1, "attemptRows": 1, "providerCalls": 0}


__all__ = [
    "AuthorizationDenied",
    "DeliveryAttempt",
    "DeliveryConflict",
    "DeliveryStore",
    "PartSubmission",
    "ServiceAuthorizationVerifier",
    "attempt_authorized_send",
]
