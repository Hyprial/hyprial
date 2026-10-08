"""Desired-state documents: interactive sessions, pending effects, zenoh endpoints and the DesiredState document with its JSON codecs."""

from __future__ import annotations
from hyprial.kernel import LifecycleMutationRequest
from hyprial.kernel import MutationProvenance
from dataclasses import replace
import uuid

import logging
import re
from dataclasses import dataclass
from typing import Any, Self
from hyprial.kernel import safe_channel_build_version
from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
    StoredLifecycleReceipt,
    StoredLifecycleResource,
)
from hyprial.kernel import DesiredStateError, HarnessLaunchSpec  # canonical defs (MP-1)
from hyprial.kernel import has_execution_runtime
from .services import ServiceConnection, ServiceRegistry


SCHEMA_VERSION = 1


SUPPORTED_SCHEMA_VERSIONS = (1, 2)


ROLLBACK_GUARD_PROVIDER = "__hyprial_desired_state_schema_v1__"


_HARNESSES = frozenset(
    {"codex", "claude", "pi", "dsh", "lark", "jev", "user-proxy"}
)


_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


_LOGGER = logging.getLogger("hyprial.daemon.impl.desired_state")


@dataclass(frozen=True, slots=True)
class InteractiveSession:
    actor: str
    cwd: str
    command: tuple[str, ...]
    source: str
    session_ref: str | None = None
    runtime: str | None = None
    channel_confirmed: bool = False
    channel_build_version: str | None = None
    channel_protocol_version: int | None = None
    owner_fence: bool | None = None
    channel_lease_digest: str | None = None
    tmux_session: str | None = None
    process_pid: int | None = None
    process_identity: str | None = None

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        record = _record(value, label)
        raw_command = record.get("command")
        if (
            not isinstance(raw_command, list)
            or not raw_command
            or any(not isinstance(item, str) or not item for item in raw_command)
        ):
            raise DesiredStateError(
                f"{label}.command must be a non-empty array of strings"
            )
        channel_confirmed = record.get("channelConfirmed", False)
        if not isinstance(channel_confirmed, bool):
            raise DesiredStateError(f"{label}.channelConfirmed must be a boolean")
        return cls(
            actor=_string(record.get("actor"), f"{label}.actor"),
            cwd=_string(record.get("cwd"), f"{label}.cwd"),
            command=tuple(raw_command),
            source=_string(record.get("source"), f"{label}.source"),
            session_ref=_optional_string(
                record.get("sessionRef"), f"{label}.sessionRef"
            ),
            runtime=_optional_string(record.get("runtime"), f"{label}.runtime"),
            channel_confirmed=channel_confirmed,
            channel_build_version=_optional_channel_build_version(
                record.get("channelBuildVersion"), f"{label}.channelBuildVersion"
            ),
            channel_protocol_version=_optional_positive_integer(
                record.get("channelProtocolVersion"),
                f"{label}.channelProtocolVersion",
            ),
            owner_fence=_optional_boolean(
                record.get("ownerFence"), f"{label}.ownerFence"
            ),
            channel_lease_digest=_optional_sha256_digest(
                record.get("channelLeaseDigest"),
                f"{label}.channelLeaseDigest",
            ),
            tmux_session=_optional_string(
                record.get("tmuxSession"), f"{label}.tmuxSession"
            ),
            process_pid=_optional_positive_integer(
                record.get("processPid"), f"{label}.processPid"
            ),
            process_identity=_optional_string(
                record.get("processIdentity"), f"{label}.processIdentity"
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "cwd": self.cwd,
            "command": list(self.command),
            "source": self.source,
            **({"sessionRef": self.session_ref} if self.session_ref else {}),
            **({"runtime": self.runtime} if self.runtime else {}),
            **({"channelConfirmed": True} if self.channel_confirmed else {}),
            **(
                {"channelBuildVersion": self.channel_build_version}
                if self.channel_build_version is not None
                else {}
            ),
            **(
                {"channelProtocolVersion": self.channel_protocol_version}
                if self.channel_protocol_version is not None
                else {}
            ),
            **(
                {"ownerFence": self.owner_fence}
                if self.owner_fence is not None
                else {}
            ),
            **(
                {"channelLeaseDigest": self.channel_lease_digest}
                if self.channel_lease_digest is not None
                else {}
            ),
            **(
                {"tmuxSession": self.tmux_session}
                if self.tmux_session is not None
                else {}
            ),
            **(
                {"processPid": self.process_pid}
                if self.process_pid is not None
                else {}
            ),
            **(
                {"processIdentity": self.process_identity}
                if self.process_identity is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class PendingSessionAgentEffect:
    """Durable custody for a Session -> Agent bind/release consequence.

    A session mutation and its Agent-domain consequence cross two independent
    bounded mailboxes.  Keeping the effect beside desired state means a full
    mailbox, a quarantined Agent actor, or a daemon restart cannot turn an
    already-persisted session mutation into an untracked best-effort call.
    """

    effect_id: str
    correlation_id: str
    operation: str
    actor: str
    harness: str | None = None
    runtime: str | None = None
    session_id: str | None = None

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        record = _record(value, label)
        operation = _string(record.get("operation"), f"{label}.operation")
        if operation not in {"bind", "release"}:
            raise DesiredStateError(f"{label}.operation must be bind or release")
        harness = _optional_string(record.get("harness"), f"{label}.harness")
        runtime = _optional_string(record.get("runtime"), f"{label}.runtime")
        if operation == "bind" and (harness is None or runtime is None):
            raise DesiredStateError(f"{label} bind effect requires harness and runtime")
        if operation == "release" and (harness is not None or runtime is not None):
            raise DesiredStateError(
                f"{label} release effect must not carry harness or runtime"
            )
        return cls(
            effect_id=_string(record.get("effectId"), f"{label}.effectId"),
            correlation_id=_string(
                record.get("correlationId"), f"{label}.correlationId"
            ),
            operation=operation,
            actor=_string(record.get("actor"), f"{label}.actor"),
            harness=harness,
            runtime=runtime,
            session_id=_optional_string(record.get("sessionId"), f"{label}.sessionId"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "effectId": self.effect_id,
            "correlationId": self.correlation_id,
            "operation": self.operation,
            "actor": self.actor,
            **({"harness": self.harness} if self.harness is not None else {}),
            **({"runtime": self.runtime} if self.runtime is not None else {}),
            **({"sessionId": self.session_id} if self.session_id is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class ZenohEndpoints:
    """Explicit Zenoh listen/connect endpoints that survive daemon restarts.

    Scouting is deliberately disabled in the transport layer, so two hosts
    only meet when at least one side listens and the other connects to an
    explicit TCP/QUIC/UDP endpoint.  Storing them in desired state is the
    persistent path; environment variables remain the per-launch override.
    """

    listen: tuple[str, ...] = ()
    connect: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        record = _record(value, f"desired state {label}")
        return cls(
            listen=_endpoint_list(record.get("listen"), f"{label}.listen"),
            connect=_endpoint_list(record.get("connect"), f"{label}.connect"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "listen": list(self.listen),
            "connect": list(self.connect),
        }


@dataclass(frozen=True, slots=True)
class DesiredState:
    """Per-machine reconciliation target for one daemon.

    ``channel_pins`` is legacy staging only: adapter pins live in the agents
    database (``~/.hyprial/state/agents.sqlite3``, ``pins`` table).  Entries remaining here are pins
    the startup migration could not resolve yet; nothing reads them for
    routing.  The field (and its on-disk ``channelPins`` key) is kept so an
    unresolved pin is never silently dropped and a pre-rework daemon can still
    read the file after a rollback.
    """

    schema_version: int = SCHEMA_VERSION
    as_mailbox: bool = False
    harnesses: tuple[HarnessLaunchSpec, ...] = ()
    interactive_sessions: tuple[InteractiveSession, ...] = ()
    pending_session_agent_effects: tuple[PendingSessionAgentEffect, ...] = ()
    lifecycle_resources: tuple[StoredLifecycleResource, ...] = ()
    lifecycle_receipts: tuple[StoredLifecycleReceipt, ...] = ()
    channel_pins: tuple[tuple[str, str], ...] = ()
    deprecated_shared_channels: tuple[str, ...] = ()
    zenoh: ZenohEndpoints = ZenohEndpoints()
    service_connections: tuple[ServiceConnection, ...] = ()
    service_registry: ServiceRegistry | None = None

    @classmethod
    def from_json(cls, value: object) -> Self:
        record = _record(value, "desired state")
        version = record.get("schemaVersion")
        if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
            raise DesiredStateError(
                f"unsupported desired-state schema version {version!r}"
            )
        if version == 1 and has_execution_runtime(record):
            raise DesiredStateError("executionRuntime requires desired-state schema 2")
        harnesses = record.get("providers")
        sessions = record.get("interactiveSessions")
        if not isinstance(harnesses, list) or not isinstance(sessions, list):
            raise DesiredStateError(
                "desired-state schema v1 requires providers and interactiveSessions arrays"
            )
        as_mailbox = record.get("asMailbox", False)
        if not isinstance(as_mailbox, bool):
            raise DesiredStateError("desired state asMailbox must be a boolean")
        parsed_harnesses = tuple(
            HarnessLaunchSpec.from_json(item, f"providers[{index}]")
            for index, item in enumerate(harnesses)
        )
        harness_keys = [(item.harness, item.name) for item in parsed_harnesses]
        if len(set(harness_keys)) != len(harness_keys):
            raise DesiredStateError(
                "desired state contains duplicate managed harnesses"
            )
        parsed_sessions = tuple(
            InteractiveSession.from_json(item, f"interactiveSessions[{index}]")
            for index, item in enumerate(sessions)
        )
        actors = [item.actor for item in parsed_sessions]
        if len(set(actors)) != len(actors):
            raise DesiredStateError(
                "desired state contains duplicate interactive actors"
            )
        raw_effects = record.get("pendingSessionAgentEffects", [])
        if not isinstance(raw_effects, list):
            raise DesiredStateError(
                "desired state pendingSessionAgentEffects must be an array"
            )
        pending_effects = tuple(
            PendingSessionAgentEffect.from_json(
                item, f"pendingSessionAgentEffects[{index}]"
            )
            for index, item in enumerate(raw_effects)
        )
        effect_ids = [item.effect_id for item in pending_effects]
        if len(set(effect_ids)) != len(effect_ids):
            raise DesiredStateError(
                "desired state contains duplicate pending session effect ids"
            )
        raw_resources = record.get("lifecycleResources", [])
        raw_receipts = record.get("lifecycleReceipts", [])
        if not isinstance(raw_resources, list) or not isinstance(raw_receipts, list):
            raise DesiredStateError(
                "desired state lifecycleResources/lifecycleReceipts must be arrays"
            )
        lifecycle_resources = tuple(
            StoredLifecycleResource.from_json(item) for item in raw_resources
        )
        lifecycle_receipts = tuple(
            StoredLifecycleReceipt.from_json(item) for item in raw_receipts
        )
        resource_ids = [
            (item.domain, item.resource_key) for item in lifecycle_resources
        ]
        receipt_ids = [
            (item.domain, item.attempt_token) for item in lifecycle_receipts
        ]
        if len(set(resource_ids)) != len(resource_ids):
            raise DesiredStateError("desired state contains duplicate lifecycle resources")
        if len(set(receipt_ids)) != len(receipt_ids):
            raise DesiredStateError("desired state contains duplicate lifecycle receipts")
        # "legacyConversationPins" (TS-era conversation pins) is retired: the
        # Python side never had a writer, production carried an empty map, and
        # nothing reads it.  A file that still contains the key parses fine --
        # unknown keys are ignored -- and the key is simply not written back.
        channel_pins = _string_map(record.get("channelPins", {}), "channelPins")
        deprecated_channels = record.get("deprecatedSharedChannels", [])
        if not isinstance(deprecated_channels, list) or any(
            not isinstance(item, str) or not item for item in deprecated_channels
        ):
            raise DesiredStateError(
                "desired state deprecatedSharedChannels must be an array of non-empty strings"
            )
        if len(set(deprecated_channels)) != len(deprecated_channels):
            raise DesiredStateError(
                "desired state contains duplicate deprecated shared channels"
            )
        raw_zenoh = record.get("zenoh")
        zenoh = (
            ZenohEndpoints()
            if raw_zenoh is None
            else ZenohEndpoints.from_json(raw_zenoh, "zenoh")
        )
        raw_connections = record.get("serviceConnections", [])
        if not isinstance(raw_connections, list):
            raise DesiredStateError("desired state serviceConnections must be an array")
        service_connections = tuple(
            ServiceConnection.from_json(item, f"serviceConnections[{index}]")
            for index, item in enumerate(raw_connections)
        )
        if len({item.name for item in service_connections}) != len(service_connections):
            raise DesiredStateError("desired state contains duplicate service connections")
        raw_registry = record.get("serviceRegistry")
        service_registry = None if raw_registry is None else ServiceRegistry.from_json(raw_registry)
        return cls(
            schema_version=version,
            as_mailbox=as_mailbox,
            harnesses=parsed_harnesses,
            interactive_sessions=parsed_sessions,
            pending_session_agent_effects=pending_effects,
            lifecycle_resources=lifecycle_resources,
            lifecycle_receipts=lifecycle_receipts,
            channel_pins=channel_pins,
            deprecated_shared_channels=tuple(sorted(deprecated_channels)),
            zenoh=zenoh,
            service_connections=service_connections,
            service_registry=service_registry,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "asMailbox": self.as_mailbox,
            "providers": [item.to_json() for item in self.harnesses],
            "interactiveSessions": [
                item.to_json() for item in self.interactive_sessions
            ],
            **(
                {
                    "pendingSessionAgentEffects": [
                        item.to_json() for item in self.pending_session_agent_effects
                    ]
                }
                if self.pending_session_agent_effects
                else {}
            ),
            **(
                {
                    "lifecycleResources": [
                        item.to_json() for item in self.lifecycle_resources
                    ]
                }
                if self.lifecycle_resources
                else {}
            ),
            **(
                {
                    "lifecycleReceipts": [
                        item.to_json() for item in self.lifecycle_receipts
                    ]
                }
                if self.lifecycle_receipts
                else {}
            ),
            "channelPins": dict(self.channel_pins),
            "deprecatedSharedChannels": list(self.deprecated_shared_channels),
            "zenoh": self.zenoh.to_json(),
            **(
                {"serviceConnections": [item.to_json() for item in self.service_connections]}
                if self.service_connections else {}
            ),
            **(
                {"serviceRegistry": self.service_registry.to_json()}
                if self.service_registry is not None else {}
            ),
        }


def _record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DesiredStateError(f"{label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DesiredStateError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _optional_channel_build_version(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not safe_channel_build_version(value):
        raise DesiredStateError(f"{label} must be a safe package-version token")
    return value


def _optional_positive_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DesiredStateError(f"{label} must be a positive integer")
    return value


def _optional_boolean(value: object, label: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise DesiredStateError(f"{label} must be a boolean")
    return value


def _optional_sha256_digest(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise DesiredStateError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _endpoint_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise DesiredStateError(f"{label} must be an array of non-empty strings")
    if len(set(value)) != len(value):
        raise DesiredStateError(f"{label} contains duplicate endpoints")
    return tuple(value)


def _string_map(value: object, label: str) -> tuple[tuple[str, str], ...]:
    record = _record(value, f"desired state {label}")
    parsed: list[tuple[str, str]] = []
    for key, item in record.items():
        parsed.append((_string(key, f"{label} key"), _string(item, f"{label}.{key}")))
    return tuple(sorted(parsed))


def _merge_session_effects(
    current: tuple[PendingSessionAgentEffect, ...],
    added: tuple[PendingSessionAgentEffect, ...],
) -> tuple[PendingSessionAgentEffect, ...]:
    effects = {effect.effect_id: effect for effect in current}
    for effect in added:
        existing = effects.get(effect.effect_id)
        if existing is not None and existing != effect:
            raise DesiredStateError(
                f"pending session effect {effect.effect_id!r} changed payload"
            )
        effects[effect.effect_id] = effect
    return tuple(effects[key] for key in sorted(effects))


def _resource_map(
    state: DesiredState,
) -> dict[tuple[str, str], StoredLifecycleResource]:
    return {
        (item.domain, item.resource_key): item for item in state.lifecycle_resources
    }

def _with_external_resource(
    state: DesiredState,
    domain: str,
    key: str,
    active: bool,
    payload: dict[str, object],
) -> DesiredState:
    resources = _resource_map(state)
    resources[(domain, key)] = _reconcile_resource(
        resources.get((domain, key)), domain, key, active, payload
    )
    return replace(
        state,
        lifecycle_resources=tuple(resources[item] for item in sorted(resources)),
    )

def _reconcile_resource(
    resource: StoredLifecycleResource | None,
    domain: str,
    key: str,
    active: bool,
    payload: dict[str, object],
) -> StoredLifecycleResource:
    """Fence mutations performed through a non-lifecycle domain command."""

    if resource is not None and resource.active == active and (
        not active or resource.payload == payload
    ):
        return resource
    return StoredLifecycleResource(domain, key, uuid.uuid4().hex, active, payload)

def _apply_create_resource(
    resource: StoredLifecycleResource,
    expected: str | None,
    payload: dict[str, object],
) -> tuple[bool, bool, StoredLifecycleResource]:
    if expected is None:
        if resource.active:
            return False, False, resource
        created = replace(
            resource,
            resource_token=uuid.uuid4().hex,
            active=True,
            payload=payload,
        )
        return True, True, created
    if resource.active or resource.resource_token != expected:
        return False, False, resource
    restored = replace(resource, active=True, payload=resource.payload or payload)
    return True, True, restored

def _apply_delete_resource(
    resource: StoredLifecycleResource,
    expected: str | None,
    payload: dict[str, object],
) -> tuple[bool, bool, StoredLifecycleResource]:
    if not resource.active:
        return False, False, resource
    if expected is not None and resource.resource_token != expected:
        return False, False, resource
    deleted = replace(resource, active=False, payload=payload or resource.payload)
    return True, True, deleted

def _stored_receipt(
    state: DesiredState,
    domain: str,
    request: LifecycleMutationRequest,
    resource_key: str,
) -> StoredLifecycleReceipt | None:
    receipt = next(
        (
            item
            for item in state.lifecycle_receipts
            if item.domain == domain and item.attempt_token == request.attempt_token
        ),
        None,
    )
    if receipt is None:
        return None
    if (
        receipt.operation_id != request.operation_id
        or receipt.resource_key != resource_key
        or receipt.expected_resource_token != request.expected_resource_token
    ):
        raise ValueError("lifecycle attempt token was reused")
    return receipt

def _receipt_by_attempt(
    state: DesiredState, domain: str, attempt_token: str
) -> StoredLifecycleReceipt | None:
    return next(
        (
            item
            for item in state.lifecycle_receipts
            if item.domain == domain and item.attempt_token == attempt_token
        ),
        None,
    )

def _new_stored_receipt(
    domain: str,
    request: LifecycleMutationRequest,
    resource_key: str,
    provenance: MutationProvenance,
    *,
    completed: bool = True,
    generation: int | None = None,
    version: int | None = None,
) -> StoredLifecycleReceipt:
    return StoredLifecycleReceipt(
        domain=domain,
        attempt_token=request.attempt_token,
        operation_id=request.operation_id,
        resource_key=resource_key,
        expected_resource_token=request.expected_resource_token,
        provenance=provenance,
        completed=completed,
        correlation_id=request.correlation_id,
        generation=generation,
        version=version,
    )
