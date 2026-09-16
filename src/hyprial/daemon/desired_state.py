"""Versioned daemon desired state with fail-loud rollback protection."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import threading
import uuid
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Self

from hyprial.contracts.channel import safe_channel_build_version
from .desired_state_sqlite import DesiredStateSqliteShadow
from .state_db import StateDatabase
from .lifecycle_receipts import (
    DomainEffectClaim,
    LifecycleMutationRequest,
    MutationProvenance,
    StoredLifecycleReceipt,
    StoredLifecycleResource,
)

SCHEMA_VERSION = 1
ROLLBACK_GUARD_PROVIDER = "__hyprial_desired_state_schema_v1__"
_HARNESSES = frozenset({"codex", "claude", "pi", "dsh", "lark"})
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_LOGGER = logging.getLogger("hyprial.daemon.desired_state")


class DesiredStateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HarnessLaunchSpec:
    harness: str
    name: str
    headless: bool
    args: tuple[str, ...] = ()
    ownership: str = "managed"
    nickname: str | None = None
    cwd: str | None = None
    endpoint: str | None = None
    session_ref: str | None = None
    # Absolute harness binary resolved at registration time (postmortem
    # 2026-08-23: a daemon launched by launchd/cron has a minimal PATH, so a
    # bare binary name is a restart-context-dependent accident — codex under
    # ~/.local/bin was unstartable exactly then).  Empty means a legacy entry
    # registered before this field existed; ``resolved_command`` then tries
    # the current PATH and finally falls back to the bare default.
    command: tuple[str, ...] = ()
    # RETIRED (#277: no timeout ever kills a turn).  Parsed, validated and
    # round-tripped so existing desired-state files and older daemons keep
    # working across the version boundary, but nothing enforces it.
    turn_timeout_seconds: float | None = None
    # Quiet-period REPORT sensitivity (worker.turn.stalled /
    # worker.turn.resumed; codex only): how long a turn may go without new
    # correlated harness activity before it is reported -- never killed.
    # None defers to HYPRIAL_TURN_IDLE_TIMEOUT_SECONDS, then the harness
    # default; 0 disables reporting.
    idle_timeout_seconds: float | None = None

    def resolved_command(self, default: tuple[str, ...]) -> tuple[str, ...]:
        """The command to spawn: persisted absolute path, else a live PATH
        resolution (legacy entries), else the bare default unchanged."""

        if self.command:
            return self.command
        found = shutil.which(default[0])
        if found is None:
            return default
        # abspath, not resolve(): vendor entry points like ~/.local/bin/codex
        # are symlinks their own updaters repoint; freezing the resolved
        # target would rot on the next upgrade.
        return (os.path.abspath(found), *default[1:])
    containerized: bool = False
    pinned_owner: str | None = None
    container_image: str | None = None
    # Model-vendor selection is separate from ``harness``.  The on-disk key
    # ``provider`` remains the harness for schema-v1 downgrade compatibility;
    # these fields use unambiguous names and never contain credentials.
    model_provider: str | None = None
    model: str | None = None
    # U0b (Allen 2026-09-03): every desired-state row is "user intent +
    # LAST KNOWN RESULT".  ``running`` is the default and is OMITTED from
    # the serialized form (absent == running, exactly like every other
    # optional field), so documents written before this column existed
    # parse and re-serialize byte-identically.  ``failed`` means the harness
    # ran successfully at some point and its most recent (re)start did not
    # come up: displayed for a human, never auto-retried.  A start that
    # never succeeded writes NO row at all -- the value lives here only for
    # rows that earned their place.
    status: str = "running"

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        record = _record(value, label)
        # Dual-read: the on-disk key stays "provider" (schemaVersion=1, so a
        # downgrade can still read files this build writes), but a "harness"
        # key is accepted and wins when both are present.
        raw_harness = record.get("harness") or record.get("provider")
        harness = "claude" if raw_harness == "cc" else raw_harness
        if harness not in _HARNESSES:
            raise DesiredStateError(
                f"{label} has an invalid harness; expected codex, claude, pi, dsh, or lark"
            )
        name = _string(record.get("name"), f"{label}.name")
        raw_args = record.get("args", [])
        if not isinstance(raw_args, list) or any(
            not isinstance(item, str) for item in raw_args
        ):
            raise DesiredStateError(f"{label}.args must be an array of strings")
        raw_command = record.get("command", [])
        if not isinstance(raw_command, list) or any(
            not isinstance(item, str) or not item for item in raw_command
        ):
            raise DesiredStateError(
                f"{label}.command must be an array of non-empty strings"
            )
        if raw_command and not os.path.isabs(raw_command[0]):
            # The whole point of persisting the command is independence from
            # the launching daemon's PATH; a relative entry would be a lie.
            raise DesiredStateError(
                f"{label}.command[0] must be an absolute path"
            )
        ownership = record.get("ownership", "managed")
        if ownership != "managed":
            raise DesiredStateError(
                f"{label}.ownership must be managed in desired state"
            )
        headless = bool(record.get("headless")) or harness == "lark"
        if not headless:
            raise DesiredStateError(
                f"{label} must be headless; interactive sessions use interactiveSessions"
            )
        raw_turn_timeout = record.get("turnTimeoutSeconds")
        if raw_turn_timeout is not None and (
            not isinstance(raw_turn_timeout, (int, float))
            or isinstance(raw_turn_timeout, bool)
            or raw_turn_timeout < 0
        ):
            raise DesiredStateError(
                f"{label}.turnTimeoutSeconds must be a non-negative number "
                "(0 disables the cap)"
            )
        raw_idle_timeout = record.get("idleTimeoutSeconds")
        if raw_idle_timeout is not None and (
            not isinstance(raw_idle_timeout, (int, float))
            or isinstance(raw_idle_timeout, bool)
            or raw_idle_timeout < 0
        ):
            raise DesiredStateError(
                f"{label}.idleTimeoutSeconds must be a non-negative number "
                "(0 disables the idle lease)"
            )
        containerized_raw = record.get("containerized", False)
        if not isinstance(containerized_raw, bool):
            raise DesiredStateError(f"{label}.containerized must be a boolean")
        pinned_owner = _optional_string(
            record.get("pinnedOwner"), f"{label}.pinnedOwner"
        )
        if pinned_owner is not None and ":" in pinned_owner:
            raise DesiredStateError(f"{label}.pinnedOwner must not contain ':'")
        raw_status = record.get("status", "running")
        if raw_status not in ("running", "failed"):
            raise DesiredStateError(
                f"{label}.status must be 'running' or 'failed'",
            )
        return cls(
            harness=str(harness),
            name=name,
            headless=headless,
            args=tuple(raw_args),
            ownership="managed",
            nickname=_optional_string(record.get("nickname"), f"{label}.nickname"),
            cwd=_optional_string(record.get("cwd"), f"{label}.cwd"),
            endpoint=_optional_string(record.get("endpoint"), f"{label}.endpoint"),
            session_ref=_optional_string(
                record.get("sessionRef"), f"{label}.sessionRef"
            ),
            command=tuple(raw_command),
            turn_timeout_seconds=(
                None if raw_turn_timeout is None else float(raw_turn_timeout)
            ),
            idle_timeout_seconds=(
                None if raw_idle_timeout is None else float(raw_idle_timeout)
            ),
            containerized=containerized_raw,
            pinned_owner=pinned_owner,
            container_image=_optional_string(
                record.get("containerImage"), f"{label}.containerImage"
            ),
            model_provider=_optional_string(
                record.get("modelProvider"), f"{label}.modelProvider"
            ),
            model=_optional_string(record.get("model"), f"{label}.model"),
            status=str(raw_status),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "provider": self.harness,
            "name": self.name,
            "headless": self.headless,
            "args": list(self.args),
            "ownership": self.ownership,
            **({"nickname": self.nickname} if self.nickname is not None else {}),
            **({"cwd": self.cwd} if self.cwd is not None else {}),
            **({"endpoint": self.endpoint} if self.endpoint is not None else {}),
            **(
                {"sessionRef": self.session_ref} if self.session_ref is not None else {}
            ),
            **({"command": list(self.command)} if self.command else {}),
            **(
                {"turnTimeoutSeconds": self.turn_timeout_seconds}
                if self.turn_timeout_seconds is not None
                else {}
            ),
            **(
                {"idleTimeoutSeconds": self.idle_timeout_seconds}
                if self.idle_timeout_seconds is not None
                else {}
            ),
            **({"containerized": True} if self.containerized else {}),
            **(
                {"pinnedOwner": self.pinned_owner}
                if self.pinned_owner is not None
                else {}
            ),
            **(
                {"containerImage": self.container_image}
                if self.container_image is not None
                else {}
            ),
            **(
                {"modelProvider": self.model_provider}
                if self.model_provider is not None
                else {}
            ),
            **({"model": self.model} if self.model is not None else {}),
            # Omitted when running: absent == running keeps pre-U0b
            # documents byte-stable across the round trip.
            **({"status": self.status} if self.status != "running" else {}),
        }


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

    @classmethod
    def from_json(cls, value: object) -> Self:
        record = _record(value, "desired state")
        version = record.get("schemaVersion")
        if version != SCHEMA_VERSION:
            raise DesiredStateError(
                f"unsupported desired-state schema version {version!r}"
            )
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
        return cls(
            as_mailbox=as_mailbox,
            harnesses=parsed_harnesses,
            interactive_sessions=parsed_sessions,
            pending_session_agent_effects=pending_effects,
            lifecycle_resources=lifecycle_resources,
            lifecycle_receipts=lifecycle_receipts,
            channel_pins=channel_pins,
            deprecated_shared_channels=tuple(sorted(deprecated_channels)),
            zenoh=zenoh,
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
        }


# One desired-state document per state dir (U0a-2): two stores aiming
# different documents at the same directory would silently clobber each
# other's committed state.  Registration is keyed by the resolved SQLite
# path; stores on the SAME legacy path (a reopen) are fine.  Weak values so
# finished stores (and their fds) vanish with their test.
_STORES_BY_DB_PATH: "weakref.WeakValueDictionary[Path, DesiredStateStore]" = (
    weakref.WeakValueDictionary()
)
_STORE_REGISTRY_LOCK = threading.Lock()


def _register_store(store: "DesiredStateStore") -> None:
    key = store.state_db.path.resolve()
    with _STORE_REGISTRY_LOCK:
        existing = _STORES_BY_DB_PATH.get(key)
        if existing is not None and existing is not store and (
            existing.legacy_path != store.legacy_path
        ):
            raise DesiredStateError(
                "two desired-state stores with different documents share one "
                f"state directory ({existing.legacy_path} and {store.legacy_path}); "
                "the SQLite authority is one document per state dir -- give this "
                "store its own state directory"
            )
        _STORES_BY_DB_PATH[key] = store


class DesiredStateStore:
    """Serialized desired-state reads and atomic updates.

    Since U0a-2 (Allen's ruling) SQLite is the ONLY storage: ``save`` writes
    one transaction (the commit point, nothing else is written) and ``load``
    reads the document back through the reverse mapping.  There is no JSON
    projection, no read fallback and no compensation -- with one storage
    there is no cross-storage window.  Homes from earlier builds are
    absorbed by the migration at construction time (see
    ``_migrate_into_sqlite``); the rollback sentinel kept at the legacy path
    makes an OLD binary fail loudly instead of silently starting empty.
    """

    def __init__(self, legacy_path: Path, state_db: StateDatabase | None = None) -> None:
        self.legacy_path = Path(legacy_path)
        self.versioned_path = Path(f"{legacy_path}.v1")
        self._lock = threading.RLock()
        # U0a-2: the shared state database.  Same file the lifecycle journal
        # uses; when the caller (DaemonApplication) passes one instance, the
        # desired-state store and the journal write through the SAME
        # serialized connection owner, so in-process write contention
        # disappears by construction.  ``None`` (every standalone
        # construction, all tests) means: own the derived path.
        # File creation stays lazy: read-only stores leave no new files.
        self.state_db = (
            state_db
            if state_db is not None
            else StateDatabase(self.legacy_path.parent / "lifecycle-operations.sqlite3")
        )
        self._sqlite_shadow = DesiredStateSqliteShadow(self.state_db)
        # The SQLite authority is ONE document per state dir.  Two stores
        # pointing at different documents in the same directory would
        # silently clobber each other's committed state -- refuse loudly.
        _register_store(self)
        # Absorb homes from earlier builds (or finish an interrupted
        # migration) before the first read.
        with self._lock:
            self._migrate_into_sqlite()
            # U0b: add the harnesses.status column (and resolve rows whose
            # last lifecycle attempt never settled) on pre-U0b databases.
            # Guarded by file existence so read-only stores still leave no
            # new files behind.
            if self.state_db.exists():
                self._sqlite_shadow.migrate_harness_status()
                self._sqlite_shadow.migrate_interactive_process_identity()

    def load(self) -> DesiredState:
        """Read the desired state from SQLite (the only storage).

        The document written by the last committed transaction is the
        authority.  ``None`` here means a genuinely fresh home: the
        migration ran at construction and found nothing to import.
        """

        with self._lock:
            document = self._sqlite_shadow.read_document()
            if document is None:
                return DesiredState()
            return DesiredState.from_json(document)

    def save(self, state: DesiredState) -> None:
        if state.schema_version != SCHEMA_VERSION:
            raise DesiredStateError(
                f"unsupported desired-state schema version {state.schema_version!r}"
            )
        with self._lock:
            validated = DesiredState.from_json(state.to_json())
            document = validated.to_json()
            # The SQLite transaction is the only write and the only commit
            # point (U0a-2): a failure here lands nothing; a success is
            # immediately the authority.  No projection, no fallback, no
            # compensation -- one storage, no cross-storage window.
            self._sqlite_shadow.write_document(document)

    def apply_session_lifecycle(
        self, request: LifecycleMutationRequest
    ) -> tuple[MutationProvenance, tuple[str, ...]]:
        """Commit a Session mutation and its token receipt in one file replace."""

        from .session_ports import RegisterSessionCommand, UnregisterSessionCommand

        payload = request.payload
        if not isinstance(payload, (RegisterSessionCommand, UnregisterSessionCommand)):
            raise TypeError(f"unsupported Session lifecycle payload: {type(payload).__name__}")
        key = f"session:{payload.actor}"
        with self._lock:
            state = self.load()
            replay = _stored_receipt(state, "session", request, key)
            if replay is not None:
                return replay.provenance, ()
            sessions = {item.actor: item for item in state.interactive_sessions}
            current = sessions.get(payload.actor)
            resources = _resource_map(state)
            resource = _reconcile_resource(
                resources.get(("session", key)),
                "session",
                key,
                current is not None,
                {} if current is None else current.to_json(),
            )
            superseded: tuple[str, ...] = ()
            if isinstance(payload, RegisterSessionCommand):
                desired = InteractiveSession(
                    actor=payload.actor,
                    cwd=payload.cwd,
                    command=payload.command,
                    source=payload.source,
                    session_ref=payload.session_ref,
                    runtime=payload.runtime,
                    channel_confirmed=payload.channel_confirmed,
                    channel_build_version=payload.channel_build_version,
                    channel_protocol_version=payload.channel_protocol_version,
                    owner_fence=payload.owner_fence,
                    tmux_session=payload.tmux_session,
                    process_pid=payload.process_pid,
                    process_identity=payload.process_identity,
                )
                changed, created, resource = _apply_create_resource(
                    resource,
                    request.expected_resource_token,
                    desired.to_json(),
                )
                if changed:
                    superseded = tuple(
                        sorted(
                            item.actor
                            for item in sessions.values()
                            if item.actor != desired.actor
                            and desired.session_ref is not None
                            and item.session_ref == desired.session_ref
                            and item.source == desired.source
                        )
                    )
                    for actor in superseded:
                        sessions.pop(actor, None)
                    sessions[desired.actor] = desired
            else:
                if current is not None and current.session_ref != payload.session_ref:
                    changed = False
                    created = False
                else:
                    changed, created, resource = _apply_delete_resource(
                        resource,
                        request.expected_resource_token,
                        {} if current is None else current.to_json(),
                    )
                    if changed:
                        sessions.pop(payload.actor, None)
            resources[("session", key)] = resource
            provenance = MutationProvenance(created, changed, resource.resource_token)
            receipt = _new_stored_receipt("session", request, key, provenance)
            updated = replace(
                state,
                interactive_sessions=tuple(sessions[name] for name in sorted(sessions)),
                lifecycle_resources=tuple(resources[item] for item in sorted(resources)),
                lifecycle_receipts=(*state.lifecycle_receipts, receipt),
            )
            self.save(updated)
            return provenance, superseded

    def apply_harness_lifecycle(
        self,
        request: LifecycleMutationRequest,
        *,
        generation: int | None = None,
        version: int | None = None,
    ) -> tuple[MutationProvenance, bool]:
        """Commit the lifecycle fence and receipt; ensure rows wait for U0b.

        Allen 2026-09-03: ``hyprial start`` must leave desired-state trace only
        AFTER the process is actually up, so an EnsureHarnessCommand here
        writes the resource fence and the (incomplete) receipt -- the
        idempotency and rollback machinery -- but NOT the harnesses row.
        The row lands in the same save as the receipt completion via
        :meth:`confirm_harness_lifecycle`, called from the start-success
        event.  RemoveHarnessCommand still deletes the row here (a removal
        whose process effect is in flight owns "gone" immediately, exactly
        as before).
        """

        from .harness_ports import EnsureHarnessCommand, RemoveHarnessCommand

        payload = request.payload
        if not isinstance(payload, (EnsureHarnessCommand, RemoveHarnessCommand)):
            raise TypeError(f"unsupported Harness lifecycle payload: {type(payload).__name__}")
        harness = payload.spec.harness if isinstance(payload, EnsureHarnessCommand) else payload.harness
        name = payload.spec.name if isinstance(payload, EnsureHarnessCommand) else payload.name
        key = f"harness:{harness}:{name}"
        with self._lock:
            state = self.load()
            replay = _stored_receipt(state, "harness", request, key)
            if replay is not None:
                return replay.provenance, True
            harnesses = {(item.harness, item.name): item for item in state.harnesses}
            current = harnesses.get((harness, name))
            resources = _resource_map(state)
            resource = _reconcile_resource(
                resources.get(("harness", key)),
                "harness",
                key,
                current is not None,
                {} if current is None else current.to_json(),
            )
            if isinstance(payload, EnsureHarnessCommand):
                desired = HarnessLaunchSpec.from_json(
                    payload.spec.to_payload(), "lifecycle.harness"
                )
                changed, created, resource = _apply_create_resource(
                    resource,
                    request.expected_resource_token,
                    desired.to_json(),
                )
                # U0b: no harnesses row here.  The row (and its
                # status="running") is written by confirm_harness_lifecycle
                # when the start actually succeeds; a start that never
                # succeeds leaves no trace, by design.
            else:
                changed, created, resource = _apply_delete_resource(
                    resource,
                    request.expected_resource_token,
                    {} if current is None else current.to_json(),
                )
                if changed:
                    harnesses.pop((harness, name), None)
            resources[("harness", key)] = resource
            provenance = MutationProvenance(created, changed, resource.resource_token)
            receipt = _new_stored_receipt(
                "harness",
                request,
                key,
                provenance,
                completed=False,
                generation=generation,
                version=version,
            )
            updated = replace(
                state,
                harnesses=tuple(harnesses[item] for item in sorted(harnesses)),
                lifecycle_resources=tuple(resources[item] for item in sorted(resources)),
                lifecycle_receipts=(*state.lifecycle_receipts, receipt),
            )
            self.save(updated)
            return provenance, False

    def confirm_harness_lifecycle(
        self,
        attempt_token: str,
        resource_token: str,
        spec: HarnessLaunchSpec,
        *,
        generation: int | None = None,
        version: int | None = None,
    ) -> bool:
        """Complete an Ensure receipt AND write its desired-state row.

        This is the U0b commit point: called only from the start-success
        event, it lands the harnesses row (status "running" -- the process
        is up) and the completed receipt in ONE save.  If a row already
        exists (re-ensure of a running or previously failed harness) it is
        rewritten from ``spec`` with status "running": the spec that just
        started IS the user's latest intent, and an explicit start is the
        documented way out of ``failed``.
        """

        with self._lock:
            state = self.load()
            found = _receipt_by_attempt(state, "harness", attempt_token)
            if found is None or found.provenance.resource_token != resource_token:
                return False
            harnesses = {(item.harness, item.name): item for item in state.harnesses}
            harnesses[(spec.harness, spec.name)] = replace(
                spec, status="running"
            )
            if found.completed:
                updated_receipt = found
            else:
                updated_receipt = replace(
                    found,
                    completed=True,
                    generation=(
                        found.generation if generation is None else generation
                    ),
                    version=found.version if version is None else version,
                )
            self.save(
                replace(
                    state,
                    harnesses=tuple(harnesses[item] for item in sorted(harnesses)),
                    lifecycle_receipts=tuple(
                        updated_receipt if item is found else item
                        for item in state.lifecycle_receipts
                    ),
                )
            )
            return True

    def mark_harness_failed(self, harness: str, name: str) -> bool:
        """Persist "ran before, did not come back" for an existing row.

        U0b/Allen 2026-09-03: a harness whose last known result was running
        and whose (re)start did not come up is recorded as ``failed`` --
        displayed, never auto-retried.  No-op (False) when no row exists: a
        start that never succeeded leaves no trace, so there is nothing to
        mark.
        """

        with self._lock:
            state = self.load()
            harnesses = {(item.harness, item.name): item for item in state.harnesses}
            current = harnesses.get((harness, name))
            if current is None:
                return False
            if current.status == "failed":
                return True
            harnesses[(harness, name)] = replace(current, status="failed")
            self.save(
                replace(
                    state,
                    harnesses=tuple(harnesses[item] for item in sorted(harnesses)),
                )
            )
            return True

    def record_harness_lifecycle_failure(
        self, attempt_token: str, resource_token: str
    ) -> int:
        """Persist one failed internal process effect and return its count."""

        with self._lock:
            state = self.load()
            found = _receipt_by_attempt(state, "harness", attempt_token)
            if (
                found is None
                or found.provenance.resource_token != resource_token
                or found.completed
            ):
                raise ValueError("Harness lifecycle failure receipt mismatch")
            updated_receipt = replace(found, attempts=found.attempts + 1)
            self.save(
                replace(
                    state,
                    lifecycle_receipts=tuple(
                        updated_receipt if item is found else item
                        for item in state.lifecycle_receipts
                    ),
                )
            )
            return updated_receipt.attempts

    def complete_harness_lifecycle(
        self,
        attempt_token: str,
        resource_token: str,
        *,
        generation: int | None = None,
        version: int | None = None,
    ) -> bool:
        with self._lock:
            state = self.load()
            found = _receipt_by_attempt(state, "harness", attempt_token)
            if found is None or found.provenance.resource_token != resource_token:
                return False
            if found.completed:
                return True
            self.save(
                replace(
                    state,
                    lifecycle_receipts=tuple(
                        replace(
                            item,
                            completed=True,
                            generation=(
                                item.generation if generation is None else generation
                            ),
                            version=item.version if version is None else version,
                        )
                        if item is found
                        else item
                        for item in state.lifecycle_receipts
                    ),
                )
            )
            return True

    def incomplete_harness_lifecycle_resources(self) -> tuple[str, ...]:
        """Resource keys whose process effect still owns retry custody."""

        state = self.load()
        return tuple(
            sorted(
                receipt.resource_key.removeprefix("harness:")
                for receipt in state.lifecycle_receipts
                if receipt.domain == "harness" and not receipt.completed
            )
        )

    def incomplete_harness_lifecycle_receipts(
        self,
    ) -> tuple[StoredLifecycleReceipt, ...]:
        state = self.load()
        return tuple(
            receipt
            for receipt in state.lifecycle_receipts
            if receipt.domain == "harness" and not receipt.completed
        )

    def harness_lifecycle_receipt(
        self, attempt_token: str
    ) -> StoredLifecycleReceipt | None:
        return _receipt_by_attempt(self.load(), "harness", attempt_token)

    def rollback_harness_lifecycle(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        """Rollback only while the committed resource still owns its token."""

        with self._lock:
            state = self.load()
            receipt = _receipt_by_attempt(state, "harness", attempt_token)
            if receipt is None or receipt.provenance.resource_token != resource_token:
                return False
            resources = _resource_map(state)
            resource = resources.get(("harness", receipt.resource_key))
            if resource is None or resource.resource_token != resource_token:
                self.save(
                    replace(
                        state,
                        lifecycle_receipts=tuple(
                            item
                            for item in state.lifecycle_receipts
                            if item is not receipt
                        ),
                    )
                )
                return False
            harnesses = {(item.harness, item.name): item for item in state.harnesses}
            key = receipt.resource_key.removeprefix("harness:")
            harness, name = key.split(":", 1)
            if receipt.provenance.changed:
                if resource.active:
                    harnesses.pop((harness, name), None)
                    resource = replace(resource, active=False)
                else:
                    restored = HarnessLaunchSpec.from_json(
                        resource.payload, "lifecycle.rollback.harness"
                    )
                    harnesses[(harness, name)] = restored
                    resource = replace(resource, active=True)
            resources[("harness", receipt.resource_key)] = resource
            self.save(
                replace(
                    state,
                    harnesses=tuple(harnesses[item] for item in sorted(harnesses)),
                    lifecycle_resources=tuple(
                        resources[item] for item in sorted(resources)
                    ),
                    lifecycle_receipts=tuple(
                        item for item in state.lifecycle_receipts if item is not receipt
                    ),
                )
            )
            return True

    def fail_harness_removal(self, attempt_token: str, resource_token: str) -> bool:
        """Settle a failed stop without undoing the admitted removal intent.

        The receipt AND current resource token authorize this transaction. A
        stale failure may retire its own receipt, never a replacement's row.
        Explicitly delete the row here too: admission's earlier delete is not
        evidence that the row is still absent at terminal settlement.
        """
        with self._lock:
            state = self.load()
            receipt = _receipt_by_attempt(state, "harness", attempt_token)
            if (receipt is None or receipt.completed
                    or receipt.provenance.resource_token != resource_token):
                return False
            resources = _resource_map(state)
            resource = resources.get(("harness", receipt.resource_key))
            owns_resource = (
                receipt.provenance.changed
                and resource is not None
                and resource.resource_token == resource_token
            )
            harnesses = state.harnesses
            if owns_resource:
                harness, name = receipt.resource_key.removeprefix("harness:").split(":", 1)
                harnesses = tuple(item for item in harnesses
                                 if (item.harness, item.name) != (harness, name))
                resources[("harness", receipt.resource_key)] = replace(resource, active=False)
            self.save(replace(
                state,
                harnesses=harnesses,
                lifecycle_resources=tuple(resources[key] for key in sorted(resources)),
                lifecycle_receipts=tuple(item for item in state.lifecycle_receipts
                                         if item is not receipt),
            ))
            return owns_resource

    def lifecycle_effect_claims(self) -> tuple[DomainEffectClaim, ...]:
        """Every durable receipt as a backfill claim (U0c startup input)."""

        return tuple(
            DomainEffectClaim(
                receipt.operation_id,
                receipt.attempt_token,
                receipt.provenance.changed,
                receipt.provenance.created_by_operation,
                receipt.provenance.resource_token,
            )
            for receipt in self.load().lifecycle_receipts
        )

    def _journal_effect_completed(
        self, operation_id: str, attempt_token: str
    ) -> bool:
        """Whether the shared-db journal already settled this attempt.

        The desired-state tables and the lifecycle journal live in one
        SQLite file (U0a-2), so the receipt expiry can ask the journal
        directly which admissions the backfill left unsettled.
        """

        if not self.state_db.exists():
            return False
        try:
            with self.state_db.read() as db:
                row = db.execute(
                    "SELECT status FROM lifecycle_effects "
                    "WHERE operation_id = ? AND attempt_token = ?",
                    (operation_id, attempt_token),
                ).fetchone()
        except sqlite3.OperationalError:
            # A database that predates the journal (or a read-only home)
            # cannot account for any attempt -- treat as unsettled.
            return False
        return row is not None and str(row[0]) == "completed"

    def expire_interrupted_lifecycle_receipts(self) -> tuple[int, int]:
        """U0c: no lifecycle receipt crosses a daemon generation.

        Allen 2026-09-03: which step of a saga is done only matters in
        memory within one generation, so at startup every durable receipt
        belongs to a dead generation.  The restart compensates from the
        journal -- ``backfill_domain_attested_effects`` has already journaled
        what these receipts attest -- and re-runs from desired-state; it
        never replays an old attempt token.

        Receipts whose journal effect is STILL unsettled after the backfill
        are orphans of a saga the journal no longer knows (lost/legacy
        databases, receipts written outside a saga): the harness domain's
        own rollback undoes those admissions (fence flip + row restore --
        this is also what collects the U0b "ghost receipts" on homes whose
        journal predates them); every other receipt is deleted outright
        (retirement crash windows -- completed-unretired and
        retired-unconfirmed -- and session-domain orphans, whose undo
        without a plan would be guesswork).  Resource fences are otherwise
        NOT touched: they carry the tokens the journal's completed effects
        reference, which is exactly the compensation input the restart is
        about to use.  Returns ``(rolled_back, deleted)``.
        """

        rolled_back = 0
        with self._lock:
            for receipt in self.load().lifecycle_receipts:
                if receipt.completed:
                    # A completed effect's undo is compensation's job; a
                    # journal-unaccounted completed receipt is dead weight
                    # (orphan of a lost journal), deleted below.
                    continue
                if self._journal_effect_completed(
                    receipt.operation_id, receipt.attempt_token
                ):
                    continue
                if receipt.domain == "harness" and self.rollback_harness_lifecycle(
                    receipt.attempt_token, receipt.provenance.resource_token
                ):
                    rolled_back += 1
            state = self.load()
            deleted = len(state.lifecycle_receipts)
            if deleted:
                self.save(replace(state, lifecycle_receipts=()))
        return rolled_back, deleted

    def retire_lifecycle_receipt(
        self, domain: str, attempt_token: str, resource_token: str
    ) -> bool:
        with self._lock:
            state = self.load()
            found = next(
                (
                    item
                    for item in state.lifecycle_receipts
                    if item.domain == domain and item.attempt_token == attempt_token
                ),
                None,
            )
            if (
                found is None
                or found.provenance.resource_token != resource_token
                or not found.completed
            ):
                return False
            if found.retired:
                return True
            receipts = tuple(
                replace(item, retired=True) if item is found else item
                for item in state.lifecycle_receipts
            )
            self.save(replace(state, lifecycle_receipts=receipts))
            return True

    def confirm_lifecycle_receipt_retired(
        self, domain: str, attempt_token: str, resource_token: str
    ) -> None:
        with self._lock:
            state = self.load()
            found = next(
                (
                    item
                    for item in state.lifecycle_receipts
                    if item.domain == domain and item.attempt_token == attempt_token
                ),
                None,
            )
            if found is None:
                return
            if not found.retired or found.provenance.resource_token != resource_token:
                raise ValueError("lifecycle receipt retirement mismatch")
            self.save(
                replace(
                    state,
                    lifecycle_receipts=tuple(
                        item for item in state.lifecycle_receipts if item is not found
                    ),
                )
            )

    def upsert_harness(self, spec: HarnessLaunchSpec) -> DesiredState:
        with self._lock:
            state = self.load()
            harnesses = {(item.harness, item.name): item for item in state.harnesses}
            # U0b: staging is an intent change, not a result change -- an
            # existing row keeps its last-known-result status (a failed
            # harness stays failed until an explicit start confirms it up);
            # a brand-new row starts at "running" so declared-then-restored
            # staging keeps its pre-U0b behavior.
            previous = harnesses.get((spec.harness, spec.name))
            harnesses[(spec.harness, spec.name)] = (
                spec if previous is None else replace(spec, status=previous.status)
            )
            updated = replace(
                state,
                harnesses=tuple(harnesses[key] for key in sorted(harnesses)),
            )
            updated = _with_external_resource(
                updated,
                "harness",
                f"harness:{spec.harness}:{spec.name}",
                True,
                spec.to_json(),
            )
            self.save(updated)
            return updated

    def remove_harness(self, harness: str, name: str) -> DesiredState:
        with self._lock:
            state = self.load()
            updated = replace(
                state,
                harnesses=tuple(
                    item
                    for item in state.harnesses
                    if (item.harness, item.name) != (harness, name)
                ),
            )
            previous = next(
                (
                    item
                    for item in state.harnesses
                    if (item.harness, item.name) == (harness, name)
                ),
                None,
            )
            if previous is not None:
                updated = _with_external_resource(
                    updated,
                    "harness",
                    f"harness:{harness}:{name}",
                    False,
                    previous.to_json(),
                )
            self.save(updated)
            return updated

    def adapter_registration(
        self, name: str
    ) -> tuple[HarnessLaunchSpec | None, str | None]:
        """Read the desired Lark harness and staged legacy pin atomically."""

        with self._lock:
            state = self.load()
            spec = next(
                (
                    item
                    for item in state.harnesses
                    if (item.harness, item.name) == ("lark", name)
                ),
                None,
            )
            return spec, dict(state.channel_pins).get(name)

    def remove_adapter_registration(
        self,
        name: str,
        *,
        expected_spec: HarnessLaunchSpec | None,
        expected_legacy_pin: str | None,
    ) -> DesiredState:
        """Remove one adapter's desired rows behind an exact compare fence."""

        with self._lock:
            state = self.load()
            current_spec = next(
                (
                    item
                    for item in state.harnesses
                    if (item.harness, item.name) == ("lark", name)
                ),
                None,
            )
            pins = dict(state.channel_pins)
            current_pin = pins.get(name)
            if current_spec != expected_spec or current_pin != expected_legacy_pin:
                raise DesiredStateError(
                    f"adapter {name!r} registry changed during removal"
                )
            pins.pop(name, None)
            harnesses = tuple(
                item
                for item in state.harnesses
                if (item.harness, item.name) != ("lark", name)
            )
            if harnesses == state.harnesses and current_pin is None:
                return state
            updated = replace(
                state,
                harnesses=harnesses,
                channel_pins=tuple(sorted(pins.items())),
            )
            self.save(updated)
            return updated

    def restore_adapter_registration(
        self,
        name: str,
        *,
        spec: HarnessLaunchSpec | None,
        legacy_pin: str | None,
    ) -> DesiredState:
        """Rollback one removal without overwriting a concurrent replacement."""

        with self._lock:
            state = self.load()
            current_spec = next(
                (
                    item
                    for item in state.harnesses
                    if (item.harness, item.name) == ("lark", name)
                ),
                None,
            )
            pins = dict(state.channel_pins)
            current_pin = pins.get(name)
            if current_spec == spec and current_pin == legacy_pin:
                return state
            if current_spec is not None or current_pin is not None:
                raise DesiredStateError(
                    f"adapter {name!r} registry replacement blocks rollback"
                )
            harnesses = list(state.harnesses)
            if spec is not None:
                harnesses.append(spec)
            if legacy_pin is not None:
                pins[name] = legacy_pin
            updated = replace(
                state,
                harnesses=tuple(
                    sorted(harnesses, key=lambda item: (item.harness, item.name))
                ),
                channel_pins=tuple(sorted(pins.items())),
            )
            self.save(updated)
            return updated

    def sync_harness_session_refs(
        self, refs: Mapping[tuple[str, str], str]
    ) -> DesiredState:
        """Persist runtime-learned session refs, touching nothing else.

        Managed harnesses learn their native session/thread id only once the
        connector is up (a Codex thread id after ``thread/start``, a pi
        session id minted at launch, a Claude session id after connect).
        Writing the ref back is what lets a daemon restart resume the
        conversation instead of cold-starting every worker.

        This is deliberately NOT ``upsert_harness``: it rewrites only the
        ``sessionRef`` key of harnesses still present in desired state, so a
        concurrent operator edit (args/cwd via ``lifecycle.start``, which
        replaces the whole spec and thereby clears the ref) can never be
        clobbered by a stale runtime observation.  Harnesses absent from
        desired state are ignored -- a removed worker keeps no residue.
        """

        with self._lock:
            state = self.load()
            changed = False
            harnesses: list[HarnessLaunchSpec] = []
            for spec in state.harnesses:
                ref = refs.get((spec.harness, spec.name))
                if ref is not None and ref != spec.session_ref:
                    spec = replace(spec, session_ref=ref)
                    changed = True
                harnesses.append(spec)
            if not changed:
                return state
            updated = replace(state, harnesses=tuple(harnesses))
            self.save(updated)
            return updated

    def set_mailbox_role(self, enabled: bool) -> DesiredState:
        with self._lock:
            state = self.load()
            updated = replace(state, as_mailbox=enabled)
            self.save(updated)
            return updated

    def update_zenoh_endpoints(
        self,
        *,
        listen: tuple[str, ...] | None = None,
        connect: tuple[str, ...] | None = None,
    ) -> DesiredState:
        """Persist explicit Zenoh endpoints; None leaves that side unchanged.

        Pass an empty tuple to clear one side, for example after tearing down
        a two-machine mesh or to fall back to per-launch environment
        overrides only.
        """

        with self._lock:
            state = self.load()
            updated = replace(
                state,
                zenoh=ZenohEndpoints(
                    listen=state.zenoh.listen if listen is None else listen,
                    connect=state.zenoh.connect if connect is None else connect,
                ),
            )
            self.save(updated)
            return updated

    def register_interactive(self, session: InteractiveSession) -> DesiredState:
        state, _superseded = self.claim_interactive(session)
        return state

    def claim_interactive(
        self, session: InteractiveSession
    ) -> tuple[DesiredState, tuple[str, ...]]:
        return self.claim_interactive_with_effects(session, ())

    def claim_interactive_with_effects(
        self,
        session: InteractiveSession,
        effects: tuple[PendingSessionAgentEffect, ...],
    ) -> tuple[DesiredState, tuple[str, ...]]:
        """Atomically make ``session`` the owner of its actor and carrier id.

        The original ``register_interactive`` API remains the compatibility
        seam used by the pre-cutover daemon.  Actor-owned session mutation needs
        the displaced aliases as part of the *same* serialized state change so
        it can fence them without a load/register race.  A carrier session may
        move to another actor, and an actor may be taken over by a newer session;
        both remain the established last-writer-wins contract.
        """

        with self._lock:
            state = self.load()
            superseded = tuple(
                sorted(
                    existing.actor
                    for existing in state.interactive_sessions
                    if existing.actor != session.actor
                    and session.session_ref is not None
                    and existing.session_ref == session.session_ref
                    and existing.source == session.source
                )
            )
            sessions = {
                existing.actor: existing
                for existing in state.interactive_sessions
                if not (
                    session.session_ref is not None
                    and existing.session_ref == session.session_ref
                    and existing.source == session.source
                    and existing.actor != session.actor
                )
            }
            sessions[session.actor] = session
            updated = replace(
                state,
                interactive_sessions=tuple(
                    sessions[actor] for actor in sorted(sessions)
                ),
                pending_session_agent_effects=_merge_session_effects(
                    state.pending_session_agent_effects, effects
                ),
            )
            updated = _with_external_resource(
                updated,
                "session",
                f"session:{session.actor}",
                True,
                session.to_json(),
            )
            for actor in superseded:
                previous = next(
                    item for item in state.interactive_sessions if item.actor == actor
                )
                updated = _with_external_resource(
                    updated,
                    "session",
                    f"session:{actor}",
                    False,
                    previous.to_json(),
                )
            self.save(updated)
            return updated, superseded

    def claim_interactive_with_agent_effects(
        self,
        session: InteractiveSession,
        bind_effect: PendingSessionAgentEffect,
    ) -> tuple[
        DesiredState,
        tuple[str, ...],
        tuple[PendingSessionAgentEffect, ...],
    ]:
        """Atomically claim a session and durably stage every Agent effect."""

        if bind_effect.operation != "bind" or bind_effect.actor != session.actor:
            raise DesiredStateError(
                "interactive claim requires a bind effect for the claimed actor"
            )
        with self._lock:
            state = self.load()
            superseded = tuple(
                sorted(
                    existing.actor
                    for existing in state.interactive_sessions
                    if existing.actor != session.actor
                    and session.session_ref is not None
                    and existing.session_ref == session.session_ref
                    and existing.source == session.source
                )
            )
            effects = tuple(
                PendingSessionAgentEffect(
                    effect_id=f"{bind_effect.effect_id}:release:{index}",
                    correlation_id=bind_effect.correlation_id,
                    operation="release",
                    actor=actor,
                )
                for index, actor in enumerate(superseded)
            ) + (bind_effect,)
            sessions = {
                existing.actor: existing
                for existing in state.interactive_sessions
                if not (
                    session.session_ref is not None
                    and existing.session_ref == session.session_ref
                    and existing.source == session.source
                    and existing.actor != session.actor
                )
            }
            sessions[session.actor] = session
            updated = replace(
                state,
                interactive_sessions=tuple(
                    sessions[actor] for actor in sorted(sessions)
                ),
                pending_session_agent_effects=_merge_session_effects(
                    state.pending_session_agent_effects, effects
                ),
            )
            updated = _with_external_resource(
                updated,
                "session",
                f"session:{session.actor}",
                True,
                session.to_json(),
            )
            for actor in superseded:
                previous = next(
                    item for item in state.interactive_sessions if item.actor == actor
                )
                updated = _with_external_resource(
                    updated,
                    "session",
                    f"session:{actor}",
                    False,
                    previous.to_json(),
                )
            self.save(updated)
            return updated, superseded, effects

    def unregister_interactive(self, actor: str) -> DesiredState:
        with self._lock:
            state = self.load()
            current = next(
                (
                    session
                    for session in state.interactive_sessions
                    if session.actor == actor
                ),
                None,
            )
            updated = replace(
                state,
                interactive_sessions=tuple(
                    session
                    for session in state.interactive_sessions
                    if session.actor != actor
                ),
            )
            if current is not None:
                updated = _with_external_resource(
                    updated,
                    "session",
                    f"session:{actor}",
                    False,
                    current.to_json(),
                )
            self.save(updated)
            return updated

    def unregister_interactive_if_current(
        self,
        actor: str,
        session_ref: str,
        effects: tuple[PendingSessionAgentEffect, ...] = (),
    ) -> tuple[DesiredState, bool]:
        """Remove only the session generation that still owns ``actor``.

        A delayed ``finally`` from a superseded carrier must never unregister
        its successor.  Keeping the comparison and write under this store's
        lock preserves the old no-op response while closing that race for the
        actor-owned mutation path.
        """

        with self._lock:
            state = self.load()
            current = next(
                (
                    session
                    for session in state.interactive_sessions
                    if session.actor == actor
                ),
                None,
            )
            if current is None or current.session_ref != session_ref:
                return state, False
            updated = replace(
                state,
                interactive_sessions=tuple(
                    session
                    for session in state.interactive_sessions
                    if session.actor != actor
                ),
                pending_session_agent_effects=_merge_session_effects(
                    state.pending_session_agent_effects, effects
                ),
            )
            updated = _with_external_resource(
                updated,
                "session",
                f"session:{actor}",
                False,
                current.to_json(),
            )
            self.save(updated)
            return updated, True

    def record_session_agent_effects(
        self, effects: tuple[PendingSessionAgentEffect, ...]
    ) -> DesiredState:
        """Durably take custody of effects without changing session ownership."""

        if not effects:
            return self.load()
        with self._lock:
            state = self.load()
            updated = replace(
                state,
                pending_session_agent_effects=_merge_session_effects(
                    state.pending_session_agent_effects, effects
                ),
            )
            self.save(updated)
            return updated

    def complete_session_agent_effect(self, effect_id: str) -> bool:
        """Retire an effect only after Agent reports its correlated result."""

        with self._lock:
            state = self.load()
            remaining = tuple(
                effect
                for effect in state.pending_session_agent_effects
                if effect.effect_id != effect_id
            )
            if len(remaining) == len(state.pending_session_agent_effects):
                return False
            self.save(replace(state, pending_session_agent_effects=remaining))
            return True

    def set_channel_pin(
        self, channel: str, agent: str
    ) -> tuple[DesiredState, str | None]:
        with self._lock:
            state = self.load()
            pins = dict(state.channel_pins)
            previous = pins.get(channel)
            pins[channel] = agent
            updated = replace(state, channel_pins=tuple(sorted(pins.items())))
            self.save(updated)
            return updated, previous

    def remove_channel_pin(self, channel: str) -> tuple[DesiredState, str | None]:
        with self._lock:
            state = self.load()
            pins = dict(state.channel_pins)
            previous = pins.pop(channel, None)
            updated = replace(state, channel_pins=tuple(sorted(pins.items())))
            self.save(updated)
            return updated, previous

    def _migrate_into_sqlite(self) -> None:
        """Absorb a pre-SQLite home -- Allen's ruling, one rule:

        At construction: if the canonical ``.v1`` document exists, import
        it in ONE transaction (replace-all; re-importing the same data is
        lossless, so this is idempotent) and then archive the file as
        ``.v1.migrated-<ts>`` (kept, not deleted).  If it does not exist,
        do nothing.

        Two properties here are pinned, not decoration (h2b-developer
        review):

        - The import reads ONLY the ``.v1`` file; ``desired-state.json``
          is never looked at.  On every real upgraded machine that file
          holds the migration.py poison pill (marker
          ``__hyprial_desired_state_schema_v1__``) -- reading it would import
          a harness named "rollback-blocked".
        - The archive rename is FATAL on failure (nothing catches it).
          Re-import being lossless relies on the .v1 being gone once
          imported: a rename failure left in place would make the next
          start replace newer SQLite state with the stale document,
          silently.  A crash between the commit and the rename is the
          benign variant -- the next start re-imports the same data and
          the rename succeeds.
        """

        if not self.versioned_path.exists():
            return
        document = self._read_import_document()
        self._sqlite_shadow.write_document(document)
        self.versioned_path.rename(self._archive_path(self.versioned_path))

    def _read_import_document(self) -> dict[str, object]:
        """Parse the canonical ``.v1`` document; loud on a broken file.

        Reads ONLY the ``.v1`` file -- never ``desired-state.json`` (see
        the poison-pill note in ``_migrate_into_sqlite``).
        """

        raw = self._read_json(self.versioned_path)
        try:
            return DesiredState.from_json(raw).to_json()
        except DesiredStateError as error:
            raise DesiredStateError(
                f"cannot import desired state from {self.versioned_path}: {error}"
            ) from error

    @staticmethod
    def _archive_path(path: Path) -> Path:
        stamp = int(time.time())
        candidate = Path(f"{path}.migrated-{stamp}")
        counter = 0
        while candidate.exists():
            counter += 1
            candidate = Path(f"{path}.migrated-{stamp}-{counter}")
        return candidate

    @staticmethod
    def _read_json(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DesiredStateError(
                f"cannot read desired state {path}: {error}"
            ) from error

    @staticmethod
    def _atomic_json_write(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


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
