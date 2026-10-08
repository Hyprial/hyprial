"""Authoritative provenance returned by lifecycle domain mutations."""

from __future__ import annotations

from hyprial.kernel import (
    LifecycleMutationCompleted,
    LifecycleMutationRequest,
    MutationProvenance,
)  # canonical vocab (MP-2)

from dataclasses import dataclass
from typing import Literal, Protocol, Self

from hyprial.kernel import PortAdmission








@dataclass(frozen=True, slots=True)
class RetireLifecycleReceiptCommand:
    correlation_id: str
    attempt_token: str
    resource_token: str


@dataclass(frozen=True, slots=True)
class ConfirmLifecycleReceiptCommand:
    correlation_id: str
    attempt_token: str
    resource_token: str


@dataclass(frozen=True, slots=True)
class LifecycleReceiptCompleted:
    correlation_id: str
    generation: int
    version: int
    domain: str
    attempt_token: str
    resource_token: str
    operation: Literal["retire", "confirm"]
    matched: bool


@dataclass(frozen=True, slots=True)
class StoredLifecycleResource:
    """Durable token fence stored beside one domain's authoritative state."""

    domain: str
    resource_key: str
    resource_token: str
    active: bool
    payload: dict[str, object]

    @classmethod
    def from_json(cls, value: object) -> Self:
        if not isinstance(value, dict):
            raise ValueError("lifecycle resource must be an object")
        payload = value.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("lifecycle resource payload must be an object")
        result = cls(
            domain=str(value.get("domain", "")),
            resource_key=str(value.get("resourceKey", "")),
            resource_token=str(value.get("resourceToken", "")),
            active=bool(value.get("active", False)),
            payload={str(key): item for key, item in payload.items()},
        )
        if not result.domain or not result.resource_key or not result.resource_token:
            raise ValueError("lifecycle resource identity must not be blank")
        return result

    def to_json(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "resourceKey": self.resource_key,
            "resourceToken": self.resource_token,
            "active": self.active,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class StoredLifecycleReceipt:
    """Idempotency receipt kept in the same store as the resource token."""

    domain: str
    attempt_token: str
    operation_id: str
    resource_key: str
    expected_resource_token: str | None
    provenance: MutationProvenance
    retired: bool = False
    completed: bool = True
    attempts: int = 0
    correlation_id: str | None = None
    generation: int | None = None
    version: int | None = None

    @classmethod
    def from_json(cls, value: object) -> Self:
        if not isinstance(value, dict):
            raise ValueError("lifecycle receipt must be an object")
        result = cls(
            domain=str(value.get("domain", "")),
            attempt_token=str(value.get("attemptToken", "")),
            operation_id=str(value.get("operationId", "")),
            resource_key=str(value.get("resourceKey", "")),
            expected_resource_token=(
                None
                if value.get("expectedResourceToken") is None
                else str(value["expectedResourceToken"])
            ),
            provenance=MutationProvenance(
                bool(value.get("createdByOperation", False)),
                bool(value.get("changed", False)),
                str(value.get("resourceToken", "")),
            ),
            retired=bool(value.get("retired", False)),
            completed=bool(value.get("completed", True)),
            attempts=int(value.get("attempts", 0)),
            correlation_id=(
                str(value["correlationId"])
                if isinstance(value.get("correlationId"), str)
                and str(value["correlationId"])
                else None
            ),
            generation=(
                int(value["generation"])
                if isinstance(value.get("generation"), int)
                else None
            ),
            version=(
                int(value["version"])
                if isinstance(value.get("version"), int)
                else None
            ),
        )
        if not result.domain or not result.attempt_token or not result.operation_id:
            raise ValueError("lifecycle receipt identity must not be blank")
        if not result.resource_key:
            raise ValueError("lifecycle receipt resource key must not be blank")
        return result

    def to_json(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "attemptToken": self.attempt_token,
            "operationId": self.operation_id,
            "resourceKey": self.resource_key,
            "expectedResourceToken": self.expected_resource_token,
            "createdByOperation": self.provenance.created_by_operation,
            "changed": self.provenance.changed,
            "resourceToken": self.provenance.resource_token,
            "retired": self.retired,
            "completed": self.completed,
            "attempts": self.attempts,
            **(
                {"correlationId": self.correlation_id}
                if self.correlation_id is not None
                else {}
            ),
            **({"generation": self.generation} if self.generation is not None else {}),
            **({"version": self.version} if self.version is not None else {}),
        }




@dataclass(frozen=True, slots=True)
class LifecycleMutationFailed:
    correlation_id: str
    attempt_token: str
    generation: int
    version: int
    domain: str
    code: str
    detail: str
    rolled_back: bool


@dataclass(frozen=True, slots=True)
class TerminalizeHarnessLifecycleCommand:
    correlation_id: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class HarnessLifecycleTerminalized:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class FailHarnessLifecycleCommand:
    """Control admission, independent of the process-effect waiter queue."""

    correlation_id: str
    request: LifecycleMutationRequest
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class HarnessLifecycleFailureSettled:
    correlation_id: str
    result: LifecycleMutationCompleted | LifecycleMutationFailed


class AtomicLifecycleMutationPort(Protocol):
    """Domain seam whose transaction owns mutation and provenance together."""

    domain: str

    @property
    def generation(self) -> int: ...

    @property
    def version(self) -> int: ...

    def submit(self, command: LifecycleMutationRequest) -> PortAdmission: ...

    def retire_receipt(self, attempt_token: str, resource_token: str) -> bool: ...

    def confirm_receipt_retired(
        self, attempt_token: str, resource_token: str
    ) -> None: ...


class AgentLifecycleMutationPort(AtomicLifecycleMutationPort, Protocol):
    domain: Literal["agent"]


class SessionLifecycleMutationPort(AtomicLifecycleMutationPort, Protocol):
    domain: Literal["session"]


class HarnessLifecycleMutationPort(AtomicLifecycleMutationPort, Protocol):
    domain: Literal["harness"]

    def fail_lifecycle(
        self, request: LifecycleMutationRequest, *, code: str, detail: str,
        timeout: float,
    ) -> LifecycleMutationCompleted | LifecycleMutationFailed: ...


class RouteLifecycleMutationPort(AtomicLifecycleMutationPort, Protocol):
    domain: Literal["route"]

    def reassociate(
        self,
        attempt_token: str,
        *,
        correlation_id: str,
        generation: int,
        version: int,
    ) -> bool: ...
