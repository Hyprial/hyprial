"""Frozen MFU ``agent.task`` v1 validation and actor-port contracts.

The module deliberately owns only the generic control-plane envelope.  MFU
business values (work packages and work results) are opaque JSON after the
closed-shape, safety, size, and digest checks defined by the frozen v1 wire
contract. PAC remains the execution engine and WorkflowRegistry remains the
only state owner; this module contains no store connection or mutable service.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
from typing import TypeAlias

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from hyprial.contracts import ipc_errors

NAMESPACE = "mfu.agent-task.v1"
PROTOCOL_VERSION = 1
COMPLETION_KIND = "result.submitted"
OPERATIONS = (
    "agent.task.capabilities",
    "agent.task.start",
    "agent.task.status",
    "agent.task.result",
    "agent.task.cancel",
    "agent.task.observe",
)

# Authority mismatch tracked against MFU 3c9f8d2 / DSH 311844b: README makes
# reply a typed nonterminal activity, while the frozen schema still excludes
# it. HYPRIAL accepts only the full closed envelope with payload={text}; it does
# not normalize the contradictory shorthand fixture or its demo conversation.
README_REPLY_EXTENSION = True

MAX_START_BYTES = 65_536
MAX_METADATA_BYTES = 8_192
MAX_ACTIVITY_BYTES = 65_536
MAX_RESULT_BYTES = 65_536
MAX_ARTIFACTS_BYTES = 32_768
MAX_ARTIFACT_BYTES = 8_192
MAX_ARTIFACTS = 100
MAX_DELEGATES = 8
MAX_TARGETS = 1_000

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EXTERNAL_REF = re.compile(r"mfu:[^:\s]+:[^:\s]+:[^:\s]+\Z")
_TARGET_REF = re.compile(r"[A-Za-z0-9._-]+\Z")
_SENSITIVE = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "apikey",
        "privatekey",
        "credential",
    }
)
_BINARY_FIELDS = frozenset({"base64", "contentbase64", "binary", "binarydata"})
_IDENTITY_OVERRIDE_FIELDS = frozenset(
    {
        "sender",
        "from",
        "caller",
        "owner",
        "actorUri",
        "serviceActor",
        "serviceActorUri",
        "serviceIdentity",
        "coordinatorActor",
    }
)
_SCHEMA_DIRECTORY = Path(__file__).with_name("agent_task_v1_schemas")


class AgentTaskError(RuntimeError):
    """One stable v1 refusal with retry guidance and structured details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = dict(details or {})

    @property
    def data(self) -> dict[str, object]:
        return {"retryable": self.retryable, "details": self.details}


def _fail(code: str, message: str, **details: object) -> None:
    raise AgentTaskError(code, message, details=details)


@lru_cache(maxsize=1)
def _schema_registry() -> tuple[Registry, dict[str, dict[str, object]]]:
    schemas: dict[str, dict[str, object]] = {}
    resources: list[tuple[str, Resource[dict[str, object]]]] = []
    for path in sorted(_SCHEMA_DIRECTORY.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(schema, dict) or not isinstance(schema.get("$id"), str):
            raise RuntimeError(f"invalid bundled agent.task schema: {path}")
        schemas[path.name] = schema
        resources.append((str(schema["$id"]), Resource.from_contents(schema)))
    return Registry().with_resources(resources), schemas


def _validate_schema(name: str, value: object, label: str) -> None:
    registry, schemas = _schema_registry()
    try:
        Draft202012Validator(
            schemas[name], registry=registry, format_checker=FormatChecker()
        ).validate(value)
    except ValidationError as error:
        path = ".".join(str(item) for item in error.absolute_path)
        location = f"{label}.{path}" if path else label
        _fail(ipc_errors.INVALID_REQUEST, f"{location}: {error.message}")


def _plain_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(ipc_errors.INVALID_REQUEST, f"{label} must be an object")
    return dict(value)


def _only_keys(value: Mapping[str, object], allowed: set[str], label: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        _fail(
            ipc_errors.INVALID_REQUEST,
            f"{label} contains unsupported fields: {', '.join(unexpected)}",
        )


def _required_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(
            ipc_errors.INVALID_REQUEST,
            f"{label} must be a non-empty string no longer than {maximum} characters",
        )
    if any(ord(character) < 32 for character in value):
        _fail(ipc_errors.INVALID_REQUEST, f"{label} contains a control character")
    return value


def _canonical_agent_uri(value: object, label: str) -> str:
    text = _required_text(value, label, 512)
    from hyprial.uri import parse_agent_uri

    if parse_agent_uri(text) is None:
        _fail(
            ipc_errors.INVALID_REQUEST,
            f"{label} must be a canonical four-part HYPRIAL Agent URI",
        )
    return text


def _target_ref(value: object, label: str = "targetRef") -> str:
    text = _required_text(value, label, 160)
    if _TARGET_REF.fullmatch(text) is None:
        _fail(ipc_errors.INVALID_REQUEST, f"{label} contains unsupported characters")
    return text


def _digest(value: object, label: str) -> str:
    text = _required_text(value, label, 71)
    if _DIGEST.fullmatch(text) is None:
        _fail(ipc_errors.INVALID_REQUEST, f"{label} must be a lowercase SHA-256 digest")
    return text


def _validate_json(value: object, label: str, *, seen: set[int] | None = None) -> None:
    """Reject values whose canonical JSON would be ambiguous or unsafe."""

    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value[:200].casefold():
            _fail(ipc_errors.INVALID_REQUEST, f"{label} contains a base64 blob")
        return
    if isinstance(value, float):
        if not math.isfinite(value) or (value == 0.0 and math.copysign(1.0, value) < 0):
            _fail(ipc_errors.INVALID_REQUEST, f"{label} contains a non-canonical number")
        return
    if isinstance(value, bytes | bytearray | memoryview):
        _fail(ipc_errors.INVALID_REQUEST, f"{label} contains binary content")
    if seen is None:
        seen = set()
    marker = id(value)
    if marker in seen:
        _fail(ipc_errors.INVALID_REQUEST, f"{label} contains a cycle")
    seen.add(marker)
    try:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    _fail(ipc_errors.INVALID_REQUEST, f"{label} contains a non-string field name")
                if key.casefold() in _SENSITIVE:
                    _fail(
                        ipc_errors.INVALID_REQUEST,
                        f"{label} contains forbidden sensitive field {key}",
                    )
                if key.casefold() in _BINARY_FIELDS:
                    _fail(ipc_errors.INVALID_REQUEST, f"{label} contains forbidden binary field {key}")
                _validate_json(item, f"{label}.{key}", seen=seen)
            return
        if isinstance(value, Sequence) and not isinstance(value, str):
            for index, item in enumerate(value):
                _validate_json(item, f"{label}[{index}]", seen=seen)
            return
        _fail(ipc_errors.INVALID_REQUEST, f"{label} is not JSON serializable")
    finally:
        seen.discard(marker)


def canonical_json(value: object) -> str:
    _validate_json(value, "value")
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise AgentTaskError(ipc_errors.INVALID_REQUEST, "value is not canonical JSON") from error


def json_bytes(value: object, label: str, maximum: int) -> bytes:
    encoded = canonical_json(value).encode()
    if len(encoded) > maximum:
        _fail(
            ipc_errors.PAYLOAD_TOO_LARGE,
            f"{label} exceeds the {maximum} byte safety limit",
            actualBytes=len(encoded),
            limitBytes=maximum,
        )
    return encoded


def sha256_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentTaskTargetInput:
    target_ref: str
    target: str
    role: str
    delegates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentTaskStartInput:
    external_ref: str
    request_digest: str
    metadata: dict[str, object]
    payload: dict[str, object]
    targets: tuple[AgentTaskTargetInput, ...]
    completion: dict[str, object]


def validate_namespace_request(
    value: Mapping[str, object],
    *,
    allowed: set[str],
    label: str,
) -> dict[str, object]:
    request = dict(value)
    overrides = sorted(set(request) & _IDENTITY_OVERRIDE_FIELDS)
    if overrides:
        _fail(
            ipc_errors.IDENTITY_OVERRIDE_FORBIDDEN,
            f"{overrides[0]} is daemon-bound and cannot be supplied",
        )
    _only_keys(request, allowed | {"protocolVersion", "namespace"}, label)
    if request.get("protocolVersion") != PROTOCOL_VERSION:
        _fail(ipc_errors.INVALID_REQUEST, f"protocolVersion must be {PROTOCOL_VERSION}")
    if request.get("namespace") != NAMESPACE:
        _fail(ipc_errors.INVALID_REQUEST, f"namespace must be {NAMESPACE}")
    return request


def validate_start(value: Mapping[str, object]) -> AgentTaskStartInput:
    request = validate_namespace_request(
        value,
        allowed={
            "externalRef",
            "requestDigest",
            "metadata",
            "payload",
            "targets",
            "completion",
        },
        label="agent.task.start",
    )
    json_bytes(request, "agent.task.start", MAX_START_BYTES)
    _validate_schema("agent-task-start.schema.json", request, "agent.task.start")
    external_ref = _required_text(request.get("externalRef"), "externalRef", 240)
    if _EXTERNAL_REF.fullmatch(external_ref) is None:
        _fail(
            ipc_errors.INVALID_REQUEST,
            "externalRef must use mfu:<case>:<workItem>:<attempt>",
        )
    metadata = _plain_object(request.get("metadata"), "metadata")
    payload = _plain_object(request.get("payload"), "payload")
    json_bytes(metadata, "metadata", MAX_METADATA_BYTES)
    _validate_json(payload, "payload")
    raw_targets = request.get("targets")
    if not isinstance(raw_targets, list) or not 1 <= len(raw_targets) <= MAX_TARGETS:
        _fail(ipc_errors.INVALID_REQUEST, f"targets must contain 1..{MAX_TARGETS} entries")
    targets: list[AgentTaskTargetInput] = []
    seen_refs: set[str] = set()
    seen_targets: set[str] = set()
    owners = 0
    for index, raw in enumerate(raw_targets):
        item = _plain_object(raw, f"targets[{index}]")
        _only_keys(item, {"targetRef", "target", "role", "delegates"}, f"targets[{index}]")
        target_ref = _target_ref(item.get("targetRef"), f"targets[{index}].targetRef")
        target = _canonical_agent_uri(item.get("target"), f"targets[{index}].target")
        role = item.get("role")
        if role not in {"owner", "participant"}:
            _fail(ipc_errors.INVALID_REQUEST, f"targets[{index}].role is invalid")
        owners += int(role == "owner")
        raw_delegates = item.get("delegates")
        if not isinstance(raw_delegates, list) or len(raw_delegates) > MAX_DELEGATES:
            _fail(ipc_errors.INVALID_REQUEST, f"targets[{index}].delegates is invalid")
        delegates = tuple(
            _canonical_agent_uri(delegate, f"targets[{index}].delegates[{delegate_index}]")
            for delegate_index, delegate in enumerate(raw_delegates)
        )
        if len(set(delegates)) != len(delegates) or target in delegates:
            _fail(
                ipc_errors.INVALID_REQUEST,
                f"targets[{index}].delegates must be unique and exclude target",
            )
        if target_ref in seen_refs:
            _fail(ipc_errors.INVALID_REQUEST, f"duplicate targetRef: {target_ref}")
        if target in seen_targets:
            _fail(ipc_errors.INVALID_REQUEST, f"duplicate target actor: {target}")
        seen_refs.add(target_ref)
        seen_targets.add(target)
        targets.append(AgentTaskTargetInput(target_ref, target, str(role), delegates))
    if owners != 1:
        _fail(ipc_errors.INVALID_REQUEST, "targets must contain exactly one owner")
    completion = _plain_object(request.get("completion"), "completion")
    _only_keys(completion, {"kind"}, "completion")
    if completion.get("kind") != COMPLETION_KIND:
        _fail(ipc_errors.INVALID_REQUEST, f"completion.kind must be {COMPLETION_KIND}")
    supplied_digest = _digest(request.get("requestDigest"), "requestDigest")
    return AgentTaskStartInput(
        external_ref,
        supplied_digest,
        metadata,
        payload,
        tuple(targets),
        completion,
    )


def validate_capabilities_request(value: Mapping[str, object]) -> None:
    validate_namespace_request(value, allowed=set(), label="agent.task.capabilities")


def validate_status_request(value: Mapping[str, object]) -> str:
    request = validate_namespace_request(
        value, allowed={"runId"}, label="agent.task.status"
    )
    return _required_text(request.get("runId"), "runId", 240)


def validate_result_request(value: Mapping[str, object]) -> tuple[str, str | None]:
    request = validate_namespace_request(
        value, allowed={"runId", "targetRef"}, label="agent.task.result"
    )
    run_id = _required_text(request.get("runId"), "runId", 240)
    raw_target_ref = request.get("targetRef")
    return run_id, None if raw_target_ref is None else _target_ref(raw_target_ref)


def validate_cancel_request(value: Mapping[str, object]) -> tuple[str, str | None]:
    request = validate_namespace_request(
        value, allowed={"runId", "reason"}, label="agent.task.cancel"
    )
    run_id = _required_text(request.get("runId"), "runId", 240)
    reason = request.get("reason")
    if reason is None or reason == "":
        return run_id, None
    return run_id, _required_text(reason, "reason", 1_000)


def _instant(value: object, label: str) -> str:
    text = _required_text(value, label, 100)
    if not text.endswith("Z"):
        _fail(ipc_errors.INVALID_REQUEST, f"{label} must be a UTC date-time ending in Z")
    try:
        datetime.fromisoformat(text.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise AgentTaskError(ipc_errors.INVALID_REQUEST, f"{label} is not a date-time") from error
    return text


def _validate_artifacts(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_ARTIFACTS:
        _fail(ipc_errors.INVALID_REQUEST, f"artifactRefs must contain at most {MAX_ARTIFACTS} entries")
    json_bytes(value, "artifactRefs", MAX_ARTIFACTS_BYTES)
    artifacts: list[dict[str, str]] = []
    for index, raw in enumerate(value):
        item = _plain_object(raw, f"artifactRefs[{index}]")
        _only_keys(item, {"kind", "value"}, f"artifactRefs[{index}]")
        json_bytes(item, f"artifactRefs[{index}]", MAX_ARTIFACT_BYTES)
        kind = item.get("kind")
        text = _required_text(item.get("value"), f"artifactRefs[{index}].value", 2_000)
        if kind == "path":
            if text.startswith("/") or "\\" in text or ".." in text.split("/"):
                _fail(ipc_errors.INVALID_REQUEST, f"artifactRefs[{index}] contains an unsafe path")
        elif kind == "url":
            if not text.startswith("https://"):
                _fail(ipc_errors.INVALID_REQUEST, f"artifactRefs[{index}] URL must use HTTPS")
        elif kind != "text":
            _fail(ipc_errors.INVALID_REQUEST, f"artifactRefs[{index}].kind is invalid")
        artifacts.append({"kind": str(kind), "value": text})
    return artifacts


@dataclass(frozen=True, slots=True)
class AgentTaskActivity:
    event_id: str
    run_id: str
    target_ref: str
    conversation_id: str
    kind: str
    at: str
    payload: dict[str, object]
    event_digest: str


def validate_activity(value: Mapping[str, object]) -> AgentTaskActivity:
    event = dict(value)
    _only_keys(
        event,
        {"schemaVersion", "eventId", "runId", "targetRef", "conversationId", "kind", "at", "payload"},
        "agent.task activity",
    )
    json_bytes(event, "agent.task activity", MAX_ACTIVITY_BYTES)
    if event.get("kind") == "reply":
        _validate_reply_extension(event)
    else:
        _validate_schema("activity-envelope.schema.json", event, "agent.task activity")
    if event.get("schemaVersion") != "hyprial.agent-task.event/v1":
        _fail(ipc_errors.INVALID_REQUEST, "activity.schemaVersion is invalid")
    event_id = _required_text(event.get("eventId"), "eventId", 240)
    run_id = _required_text(event.get("runId"), "runId", 240)
    target_ref = _target_ref(event.get("targetRef"))
    conversation_id = _required_text(event.get("conversationId"), "conversationId", 240)
    kind = event.get("kind")
    if kind not in {"progress", "question", "blocked", "reply", COMPLETION_KIND}:
        _fail(ipc_errors.INVALID_REQUEST, "activity.kind is invalid")
    at = _instant(event.get("at"), "at")
    payload = _plain_object(event.get("payload"), "payload")
    _validate_activity_payload(str(kind), payload)
    return AgentTaskActivity(
        event_id,
        run_id,
        target_ref,
        conversation_id,
        str(kind),
        at,
        payload,
        sha256_digest(event),
    )


def _validate_activity_payload(kind: str, payload: dict[str, object]) -> None:
    if kind == "reply":
        _only_keys(payload, {"text"}, "reply.payload")
        _required_text(payload.get("text"), "reply.text", 4_000)
        return
    if kind == "progress":
        _only_keys(payload, {"summary", "percent", "phase"}, "progress.payload")
        _required_text(payload.get("summary"), "progress.summary", 2_000)
        percent = payload.get("percent")
        if percent is not None and (
            not isinstance(percent, int | float)
            or isinstance(percent, bool)
            or not 0 <= percent <= 100
        ):
            _fail(ipc_errors.INVALID_REQUEST, "progress.percent must be null or 0..100")
        phase = payload.get("phase")
        if phase is not None:
            _required_text(phase, "progress.phase", 240)
        return
    if kind == "question":
        _only_keys(payload, {"question", "blocking", "responseFormat"}, "question.payload")
        _required_text(payload.get("question"), "question.question", 4_000)
        if not isinstance(payload.get("blocking"), bool):
            _fail(ipc_errors.INVALID_REQUEST, "question.blocking must be a boolean")
        response_format = payload.get("responseFormat")
        if response_format is not None:
            _required_text(response_format, "question.responseFormat", 1_000)
        return
    if kind == "blocked":
        _only_keys(payload, {"summary", "blockers", "retryable"}, "blocked.payload")
        _required_text(payload.get("summary"), "blocked.summary", 2_000)
        blockers = payload.get("blockers")
        if not isinstance(blockers, list) or not 1 <= len(blockers) <= 100:
            _fail(ipc_errors.INVALID_REQUEST, "blocked.blockers must contain 1..100 entries")
        for index, blocker in enumerate(blockers):
            _required_text(blocker, f"blocked.blockers[{index}]", 2_000)
        if not isinstance(payload.get("retryable"), bool):
            _fail(ipc_errors.INVALID_REQUEST, "blocked.retryable must be a boolean")
        return
    _only_keys(payload, {"resultRef", "resultDigest", "result", "artifactRefs"}, "result.payload")
    _required_text(payload.get("resultRef"), "resultRef", 240)
    supplied = _digest(payload.get("resultDigest"), "resultDigest")
    result = _plain_object(payload.get("result"), "result")
    json_bytes(result, "result", MAX_RESULT_BYTES)
    _validate_artifacts(payload.get("artifactRefs"))
    # Digest equality is checked transactionally in AgentTaskStore.  The
    # idempotency lookup intentionally precedes body-digest validation so a
    # changed digest for an existing ref yields the frozen *_REF_CONFLICT
    # verdict even when the changed digest no longer describes the body.
    _ = supplied


def _validate_reply_extension(event: Mapping[str, object]) -> None:
    if not README_REPLY_EXTENSION:
        _fail(ipc_errors.INVALID_REQUEST, "reply activity extension is disabled")
    if event.get("schemaVersion") != "hyprial.agent-task.event/v1":
        _fail(ipc_errors.INVALID_REQUEST, "activity.schemaVersion is invalid")
    _required_text(event.get("eventId"), "eventId", 240)
    _required_text(event.get("runId"), "runId", 240)
    _target_ref(event.get("targetRef"))
    _required_text(event.get("conversationId"), "conversationId", 240)
    _instant(event.get("at"), "at")
    payload = _plain_object(event.get("payload"), "payload")
    _only_keys(payload, {"text"}, "reply.payload")
    _required_text(payload.get("text"), "reply.text", 4_000)


@dataclass(frozen=True, slots=True)
class StartAgentTaskCommand:
    correlation_id: str
    service_actor: str
    caller: str
    request: AgentTaskStartInput
    yaml_text: str


@dataclass(frozen=True, slots=True)
class CancelAgentTaskCommand:
    correlation_id: str
    service_actor: str
    caller: str
    run_id: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class ObserveAgentTaskCommand:
    """Submit one already-validated activity through the frozen actor port."""

    correlation_id: str
    service_actor: str
    submitter: str
    activity: AgentTaskActivity
    message_id: str | None = None


AgentTaskCommand: TypeAlias = (
    StartAgentTaskCommand | CancelAgentTaskCommand | ObserveAgentTaskCommand
)


@dataclass(frozen=True, slots=True)
class AgentTaskTargetProjection:
    target_ref: str
    target: str
    conversation_id: str
    attempts: int
    state: str
    result_ref: str | None
    result: dict[str, object] | None = None

    def to_payload(self, *, include_result: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "targetRef": self.target_ref,
            "target": self.target,
            "conversationId": self.conversation_id,
            "attempts": self.attempts,
            "state": self.state,
            "resultRef": self.result_ref,
        }
        if include_result:
            payload["result"] = self.result
        return payload


@dataclass(frozen=True, slots=True)
class AgentTaskRunProjection:
    run_id: str
    external_ref: str
    state: str
    targets: tuple[AgentTaskTargetProjection, ...]
    last_event_id: str | None
    created: bool | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "externalRef": self.external_ref,
            "state": self.state,
            "targets": [target.to_payload() for target in self.targets],
            "lastEventId": self.last_event_id,
            **({"created": self.created} if self.created is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class AgentTaskResultProjection:
    run_id: str
    external_ref: str
    targets: tuple[AgentTaskTargetProjection, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "externalRef": self.external_ref,
            "targets": [
                target.to_payload(include_result=True) for target in self.targets
            ],
        }


@dataclass(frozen=True, slots=True)
class AgentTaskStarted:
    correlation_id: str
    generation: int
    version: int
    result: AgentTaskRunProjection


@dataclass(frozen=True, slots=True)
class AgentTaskCancelled:
    correlation_id: str
    generation: int
    version: int
    result: AgentTaskRunProjection


@dataclass(frozen=True, slots=True)
class AgentTaskObserved:
    correlation_id: str
    generation: int
    version: int
    run_id: str
    target_ref: str
    event_id: str
    accepted: bool
    created: bool


AgentTaskEvent: TypeAlias = AgentTaskStarted | AgentTaskCancelled | AgentTaskObserved
