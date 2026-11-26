"""Schema-four model records contain references and intent, never credentials."""

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Iterable, NoReturn, Self

from hyprial.error import codes
from hyprial.kernel import DECLARED_HARNESSES, DesiredStateError

MODEL_BINDING_OPERATION_LIMIT = 10_000


class ModelBindingError(DesiredStateError):
    def __init__(self, code: str, reason: str, phase: str = "bind") -> None:
        self.code, self.phase, self.reason = code, phase, reason
        super().__init__(f"{code}: {phase}/{reason}")


def fail(
    reason: str, code: str = codes.MODEL_CONFIGURATION_CONFLICT, phase: str = "bind"
) -> NoReturn:
    raise ModelBindingError(code, reason, phase)


def invalid() -> NoReturn:
    fail("invalid-connection", codes.MODEL_CONFIGURATION_INVALID, "validate")


def text(value: object, *, limit: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or value != value.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        invalid()
    return value


def integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        invalid()
    return value


def digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{64}", value) is None:
        invalid()
    return value


def record(
    value: object, required: Iterable[str], optional: Iterable[str] = ()
) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or not set(required) <= value.keys()
        or value.keys() - set(required) - set(optional)
    ):
        invalid()
    return value


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class Configuration:
    auth_kind: str
    model: str
    harness: str
    mode: str
    intent_digest: str
    connection_ref: str | None = None
    config_revision: int | None = None
    native_auth_ref: str | None = None

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = record(
            value,
            ("authKind", "model", "harness", "mode", "intentDigest"),
            ("connectionRef", "configRevision", "nativeAuthRef"),
        )
        kind = text(raw["authKind"])
        if (
            kind not in {"connection", "native"}
            or text(raw["harness"]) not in DECLARED_HARNESSES
            or text(raw["mode"]) not in {"headless", "interactive"}
        ):
            invalid()
        if kind == "connection":
            if (
                "nativeAuthRef" in raw
                or "connectionRef" not in raw
                or "configRevision" not in raw
            ):
                invalid()
            ref, revision, native = (
                text(raw["connectionRef"]),
                integer(raw["configRevision"], 1),
                None,
            )
        else:
            if (
                "connectionRef" in raw
                or "configRevision" in raw
                or "nativeAuthRef" not in raw
            ):
                invalid()
            ref, revision, native = None, None, text(raw["nativeAuthRef"])
        return cls(
            kind,
            text(raw["model"]),
            raw["harness"],
            raw["mode"],
            digest(raw["intentDigest"]),
            ref,
            revision,
            native,
        )

    def to_json(self) -> dict[str, object]:
        result = {
            "authKind": self.auth_kind,
            "model": self.model,
            "harness": self.harness,
            "mode": self.mode,
            "intentDigest": self.intent_digest,
        }
        if self.auth_kind == "connection":
            result.update(
                connectionRef=self.connection_ref, configRevision=self.config_revision
            )
        else:
            result["nativeAuthRef"] = self.native_auth_ref
        return result


def request_digest(
    agent_id: str, member_id: str, expected_revision: int, config: Configuration,
    coordination=None,
) -> str:
    return sha256(
        canonical(
            {
                "version": 1 if coordination is None else 2,
                "agentId": agent_id,
                "memberId": member_id,
                "expectedRevision": expected_revision,
                "configuration": config.to_json(),
                **({} if coordination is None else {"cell": coordination.plan_json()}),
            }
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class Binding:
    agent_id: str
    member_id: str
    binding_ref: str
    binding_revision: int
    configuration: Configuration
    operation_id: str

    def to_json(self) -> dict[str, object]:
        return {
            "agentId": self.agent_id,
            "memberId": self.member_id,
            "bindingRef": self.binding_ref,
            "bindingRevision": self.binding_revision,
            "configuration": self.configuration.to_json(),
            "operationId": self.operation_id,
            "phase": "active",
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = record(
            value,
            (
                "agentId",
                "memberId",
                "bindingRef",
                "bindingRevision",
                "configuration",
                "operationId",
                "phase",
            ),
        )
        if raw["phase"] != "active":
            invalid()
        return cls(
            text(raw["agentId"]),
            text(raw["memberId"]),
            text(raw["bindingRef"]),
            integer(raw["bindingRevision"], 1),
            Configuration.from_json(raw["configuration"]),
            text(raw["operationId"]),
        )


@dataclass(frozen=True, slots=True)
class Coordination:
    cell_id: str
    expected_revision: int
    previous_intent: tuple[tuple[str, str | None], ...]
    phase: str = "planned"
    observed_revision: int | None = None

    def plan_json(self):
        return {
            "schemaVersion": 1, "cellId": self.cell_id,
            "expectedRevision": self.expected_revision,
            "previousIntent": dict(self.previous_intent),
        }

    def to_json(self):
        return {**self.plan_json(), "phase": self.phase,
                "observedRevision": self.observed_revision}

    @classmethod
    def from_json(cls, value):
        raw = record(value, ("schemaVersion", "cellId", "expectedRevision",
                             "previousIntent", "phase", "observedRevision"))
        if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != 1:
            invalid()
        previous = record(raw["previousIntent"], (), ("kind", "model"))
        for value in previous.values():
            if value is not None:
                text(value)
        result = cls(text(raw["cellId"]), integer(raw["expectedRevision"], 1),
                     tuple(sorted(previous.items())), text(raw["phase"]),
                     raw["observedRevision"])
        if result.phase not in {"planned", "applied", "cancelling", "compensated"}:
            invalid()
        if result.observed_revision is not None:
            integer(result.observed_revision, result.expected_revision)
        if (result.phase == "planned" and result.observed_revision is not None
                or result.phase in {"applied", "compensated"}
                and result.observed_revision is None):
            invalid()
        return result


@dataclass(frozen=True, slots=True)
class Operation:
    agent_id: str
    member_id: str
    operation_id: str
    request_digest: str
    expected_revision: int
    binding_ref: str
    candidate: Configuration
    phase: str = "prepared"
    result_revision: int | None = None
    coordination: Coordination | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "agentId": self.agent_id,
            "memberId": self.member_id,
            "operationId": self.operation_id,
            "requestDigest": self.request_digest,
            "expectedRevision": self.expected_revision,
            "bindingRef": self.binding_ref,
            "candidate": self.candidate.to_json(),
            "phase": self.phase,
            "resultRevision": self.result_revision,
            **({} if self.coordination is None else {
                "coordination": self.coordination.to_json(),
            }),
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = record(
            value,
            (
                "agentId",
                "memberId",
                "operationId",
                "requestDigest",
                "expectedRevision",
                "bindingRef",
                "candidate",
                "phase",
                "resultRevision",
            ),
            ("coordination",),
        )
        result = cls(
            text(raw["agentId"]),
            text(raw["memberId"]),
            text(raw["operationId"]),
            digest(raw["requestDigest"]),
            integer(raw["expectedRevision"]),
            text(raw["bindingRef"]),
            Configuration.from_json(raw["candidate"]),
            text(raw["phase"]),
            raw["resultRevision"],
            Coordination.from_json(raw["coordination"]) if "coordination" in raw else None,
        )
        if result.phase not in {
            "prepared",
            "finalized",
            "cancelled",
        } or result.request_digest != request_digest(
            result.agent_id,
            result.member_id,
            result.expected_revision,
            result.candidate,
            result.coordination,
        ):
            invalid()
        if result.phase == "prepared":
            if result.result_revision is not None:
                invalid()
        elif integer(result.result_revision) != result.expected_revision + (
            result.phase == "finalized"
        ):
            invalid()
        if result.coordination is not None and result.phase != "prepared":
            if result.coordination.phase != (
                "applied" if result.phase == "finalized" else "compensated"
            ):
                invalid()
        return result


@dataclass(frozen=True, slots=True)
class Cleanup:
    agent_id: str
    entity_token: str
    destroy_attempt: str

    def to_json(self) -> dict[str, object]:
        return {
            "agentId": self.agent_id,
            "cleanupPending": {
                "entityToken": self.entity_token,
                "destroyAttempt": self.destroy_attempt,
            },
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = record(value, ("agentId", "cleanupPending"))
        pending = record(raw["cleanupPending"], ("entityToken", "destroyAttempt"))
        return cls(
            text(raw["agentId"]),
            text(pending["entityToken"]),
            text(pending["destroyAttempt"]),
        )


@dataclass(frozen=True, slots=True)
class Status:
    binding: Binding | None
    pending: Operation | None
    operations: tuple[Operation, ...]
    cleanup: Cleanup | None
