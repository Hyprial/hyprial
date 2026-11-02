"""Daemon-owned preview→execute confirmation records (generic protocol only).

A preview record binds the caller, the operation (method), a canonical digest
of the previewed params, the daemon epoch and an expiry.  The returned
``operationId`` is an unpredictable instance ID; it is deliberately not a
business identifier such as a ``moveId`` and not an execution operation ID.
The record lives in memory for one daemon epoch: no CLI confirmation file and
no second durable business authority is created here.  This is the generic
record/claim protocol, NOT a complete §7 business implementation: a domain
owner still supplies authorization and resource-version checks before claim
and owns durable result replay across restarts.  No business write is wired to
this registry in A-03.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from hyprial.kernel import DaemonRequestError, MethodSpec, ipc_errors

DEFAULT_CAPACITY = 256


def _canonical_json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("preview data must be a mapping")
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise TypeError(f"preview data is not canonical JSON: {error}") from error


def params_digest(params: Mapping[str, Any]) -> str:
    """Stable digest of the previewed params; no credentials are added here."""

    return hashlib.sha256(_canonical_json(params).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PreviewRecord:
    """The daemon-held facts one operationId is bound to."""

    operation_id: str
    method: str
    caller: str
    session: str | None
    params_digest: str
    epoch: str
    resource_version: str | None
    issued_at_ms: int
    expires_at_ms: int
    summary: str
    params_json: str = "{}"
    claimed: bool = False
    completed: bool = False
    result_json: str | None = None

    @property
    def params(self) -> Mapping[str, Any]:
        return json.loads(self.params_json)

    @property
    def result(self) -> Mapping[str, Any] | None:
        return None if self.result_json is None else json.loads(self.result_json)


class PreviewRegistry:
    """In-memory preview records for exactly one daemon epoch."""

    def __init__(
        self,
        *,
        epoch: str,
        now_ms: Callable[[], int] | None = None,
        capacity: int = DEFAULT_CAPACITY,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(epoch, str) or not epoch:
            raise ValueError("preview registry needs a daemon epoch")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("preview registry capacity must be a positive integer")
        self.epoch = epoch
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._capacity = capacity
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(24))
        self._records: dict[str, PreviewRecord] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._records)

    def issue(
        self,
        spec: MethodSpec,
        params: Mapping[str, Any],
        *,
        caller: str,
        summary: str,
        session: str | None = None,
        resource_version: str | None = None,
        ttl_seconds: int | None = None,
    ) -> PreviewRecord:
        """Create one preview record; the caller never supplies the operation ID."""

        if spec.confirmation != "preview" or spec.preview is None:
            raise ValueError(f"{spec.name} is not a preview method")
        if not isinstance(caller, str) or not caller:
            raise ValueError("preview caller must be a non-empty identity")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("preview summary must be non-empty")
        ttl = spec.preview.ttl_seconds if ttl_seconds is None else ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= spec.preview.ttl_seconds:
            raise ValueError(f"preview ttl must be 1..{spec.preview.ttl_seconds} seconds")
        digest = params_digest(params)
        canonical_params = _canonical_json(params)
        now = self._now_ms()
        with self._lock:
            operation_id = self._new_id()
            record = PreviewRecord(
                operation_id=operation_id,
                method=spec.name,
                caller=caller,
                session=session,
                params_digest=digest,
                epoch=self.epoch,
                resource_version=resource_version,
                issued_at_ms=now,
                expires_at_ms=now + ttl * 1000,
                summary=summary,
                params_json=canonical_params,
            )
            self._records[operation_id] = record
            self._evict()
        return record

    def claim(
        self,
        spec: MethodSpec,
        params: Mapping[str, Any],
        *,
        caller: str,
        session: str | None = None,
        expected_digest: str | None = None,
        resource_version: str | None = None,
        authorize: Callable[[PreviewRecord], None] | None = None,
    ) -> PreviewRecord:
        """Claim one preview for execution; a second claim never runs twice."""

        if spec.confirmation != "execute" or spec.preview_of is None:
            raise ValueError("preview claim requires an execute method with preview_of")
        field = self._operation_field(spec)
        if set(params) != {field}:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"preview confirmation accepts only {field!r}; no replacement params",
            )
        operation_id = params.get(field)
        if not isinstance(operation_id, str) or not operation_id:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"preview confirmation must carry {field!r}",
            )
        now = self._now_ms()
        with self._lock:
            record = self._records.get(operation_id)
            if record is None:
                raise DaemonRequestError(
                    ipc_errors.PREVIEW_UNKNOWN,
                    "preview record is unknown; preview the operation again",
                )
            expected_method = (
                spec.preview_of
                if spec.confirmation == "execute" and spec.preview_of is not None
                else spec.name
            )
            if (
                record.method != expected_method
                or record.caller != caller
                or record.session != session
            ):
                # Do not leak another caller/session's target details.
                raise DaemonRequestError(
                    ipc_errors.PREVIEW_STALE,
                    "preview record does not belong to this caller and method",
                )
            if record.epoch != self.epoch:
                raise DaemonRequestError(
                    ipc_errors.PREVIEW_STALE,
                    "preview record belongs to another daemon epoch",
                )
            if now >= record.expires_at_ms:
                self._records.pop(operation_id, None)
                raise DaemonRequestError(
                    ipc_errors.PREVIEW_EXPIRED,
                    "preview record expired; preview the operation again",
                )
            if expected_digest is not None and expected_digest != record.params_digest:
                raise DaemonRequestError(
                    ipc_errors.PREVIEW_STALE,
                    "preview params changed; preview the operation again",
                )
            if (
                resource_version is not None
                and resource_version != record.resource_version
            ):
                raise DaemonRequestError(
                    ipc_errors.PREVIEW_STALE,
                    "preview resource version changed; preview the operation again",
                )
            if authorize is not None:
                authorize(record)
            if record.completed:
                # A repeat confirmation replays the same terminal result; it never
                # executes the operation a second time.
                return record
            if record.claimed:
                raise DaemonRequestError(
                    ipc_errors.OPERATION_IN_PROGRESS,
                    "preview record is already claimed",
                )
            claimed = replace(record, claimed=True)
            self._records[operation_id] = claimed
            return claimed

    def complete(
        self, operation_id: str, result: Mapping[str, Any]
    ) -> PreviewRecord:
        """Record the claimed operation's terminal result for replays."""

        canonical = _canonical_json(result)
        with self._lock:
            record = self._records.get(operation_id)
            if record is None:
                raise DaemonRequestError(
                    ipc_errors.PREVIEW_UNKNOWN,
                    "preview record is unknown; preview the operation again",
                )
            if not record.claimed:
                raise DaemonRequestError(
                    ipc_errors.PREVIEW_STALE,
                    "preview record has not been claimed",
                )
            if record.completed:
                if record.result_json == canonical:
                    return record
                raise DaemonRequestError(
                    ipc_errors.PREVIEW_STALE,
                    "preview terminal result is immutable",
                )
            completed = replace(record, completed=True, result_json=canonical)
            self._records[operation_id] = completed
            return completed

    def discard(self, operation_id: str) -> None:
        self._records.pop(operation_id, None)

    def _operation_field(self, spec: MethodSpec) -> str:
        if spec.preview is not None:
            return spec.preview.operation_field
        if spec.confirmation == "execute" and spec.preview_of is not None:
            return "operationId"
        raise ValueError(f"{spec.name} is not a preview-confirmed method")

    def _new_id(self) -> str:
        for _attempt in range(8):
            candidate = self._id_factory()
            if isinstance(candidate, str) and candidate and candidate not in self._records:
                return candidate
        raise RuntimeError("preview id factory did not produce a unique operation id")

    def _evict(self) -> None:
        while len(self._records) > self._capacity:
            oldest = min(
                self._records.values(), key=lambda record: record.issued_at_ms
            )
            self._records.pop(oldest.operation_id, None)
