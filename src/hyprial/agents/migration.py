"""Fail-closed, per-agent migration transactions for agent-home P2.

This module owns the filesystem transaction and its durable evidence.  It does
not discover a user's home, credentials, or provider configuration.  Every
source is explicit, every run is fenced by the registry incarnation and home
receipt, and provider/transfer/launcher rewiring is delegated to one required
binding facade.  That separation lets P22/P23 supply their frozen runtime
context without teaching the migration layer to reconstruct native roots.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol, Self

from hyprial.contracts import ipc_errors

from .config import AgentConfig, AgentConfigError, ConfigManifest, build_native_projection
from .environment import apply_runtime_environment_profile
from .registry import Agent, AgentRegistry
from .runtime import (
    AgentRuntimeContext,
    AgentToolProfile,
    resolve_agent_runtime_context,
)

__all__ = [
    "AgentHomeMigrationError",
    "AgentMigrationAuthorization",
    "AgentMigrationBindings",
    "AgentMigrationCoordinator",
    "AgentMigrationLivenessProbe",
    "AgentMigrationPlan",
    "AgentRuntimeMigrationBindings",
    "MigrationEntry",
    "MigrationPhase",
    "MigrationRecord",
    "SupportKey",
    "SupportMatrix",
    "SupportRow",
    "SupportStatus",
]

_SCHEMA_VERSION = 1
_ALLOWED_DESTINATIONS = frozenset(
    {
        "config",
        "secrets/native/claude",
        "secrets/native/codex",
        "secrets/native/pi",
        "secrets/tools",
        "state/pi",
    }
)


class AgentHomeMigrationError(RuntimeError):
    """A sanitized migration refusal or failure with a stable phase."""

    code = ipc_errors.INVALID_ARGUMENT

    def __init__(self, category: str, phase: str, *, actor: str) -> None:
        self.category = category
        self.phase = phase
        self.actor = actor
        self.data = {"category": category, "phase": phase, "actor": actor}
        super().__init__(
            f"agent-home migration {category}: actor={actor!r} phase={phase!r}"
        )


class SupportStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    FAILED = "FAILED"
    UNSUPPORTED = ipc_errors.UNSUPPORTED
    PASS = "PASS"


@dataclass(frozen=True, slots=True, order=True)
class SupportKey:
    harness: str
    version: str
    operating_system: str
    entrypoint: str
    auth_mode: str

    def __post_init__(self) -> None:
        for label, value in (
            ("harness", self.harness),
            ("version", self.version),
            ("operating_system", self.operating_system),
            ("entrypoint", self.entrypoint),
            ("auth_mode", self.auth_mode),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"support key {label} must not be blank")

    def to_json(self) -> dict[str, str]:
        return {
            "harness": self.harness,
            "version": self.version,
            "operatingSystem": self.operating_system,
            "entrypoint": self.entrypoint,
            "authMode": self.auth_mode,
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = _exact_object(
            value,
            {"harness", "version", "operatingSystem", "entrypoint", "authMode"},
            "support key",
        )
        return cls(
            _string(raw["harness"], "support key harness"),
            _string(raw["version"], "support key version"),
            _string(raw["operatingSystem"], "support key operatingSystem"),
            _string(raw["entrypoint"], "support key entrypoint"),
            _string(raw["authMode"], "support key authMode"),
        )


@dataclass(frozen=True, slots=True)
class SupportRow:
    key: SupportKey
    status: SupportStatus
    evidence: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, SupportStatus):
            raise ValueError("support row status must be explicit")
        if not isinstance(self.evidence, str) or not self.evidence.strip():
            raise ValueError("support row evidence must not be blank")

    def to_json(self) -> dict[str, object]:
        return {**self.key.to_json(), "status": self.status.value, "evidence": self.evidence}

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = _exact_object(
            value,
            {
                "harness",
                "version",
                "operatingSystem",
                "entrypoint",
                "authMode",
                "status",
                "evidence",
            },
            "support row",
        )
        return cls(
            SupportKey.from_json(
                {
                    key: raw[key]
                    for key in (
                        "harness",
                        "version",
                        "operatingSystem",
                        "entrypoint",
                        "authMode",
                    )
                }
            ),
            SupportStatus(_string(raw["status"], "support row status")),
            _string(raw["evidence"], "support row evidence"),
        )


@dataclass(frozen=True, slots=True)
class SupportMatrix:
    rows: tuple[SupportRow, ...]

    def __post_init__(self) -> None:
        keys = tuple(row.key for row in self.rows)
        if len(keys) != len(set(keys)):
            raise ValueError("support matrix keys must be unique")
        if keys != tuple(sorted(keys)):
            raise ValueError("support matrix rows must be sorted by exact key")

    def status_for(self, key: SupportKey) -> SupportStatus:
        for row in self.rows:
            if row.key == key:
                return row.status
        return SupportStatus.NOT_RUN

    def require_pass(self, keys: Iterable[SupportKey], *, actor: str) -> None:
        for key in keys:
            if self.status_for(key) is not SupportStatus.PASS:
                raise AgentHomeMigrationError(
                    f"support-{self.status_for(key).value.lower()}",
                    "preflight-support",
                    actor=actor,
                )

    def to_json(self) -> dict[str, object]:
        return {
            "schemaVersion": _SCHEMA_VERSION,
            "rows": [row.to_json() for row in self.rows],
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = _exact_object(value, {"schemaVersion", "rows"}, "support matrix")
        if raw["schemaVersion"] != _SCHEMA_VERSION or not isinstance(raw["rows"], list):
            raise ValueError("unsupported support matrix")
        return cls(tuple(sorted((SupportRow.from_json(row) for row in raw["rows"]), key=lambda row: row.key)))


@dataclass(frozen=True, slots=True)
class MigrationEntry:
    """One explicitly authorized source tree and one fixed agent-home target."""

    source: str
    destination: str
    sensitive: bool

    def __post_init__(self) -> None:
        source = Path(self.source)
        if not source.is_absolute():
            raise ValueError("migration source must be absolute")
        _validate_relative(self.destination, "migration destination")
        if self.destination not in _ALLOWED_DESTINATIONS:
            raise ValueError(f"unsupported migration destination: {self.destination}")
        if not isinstance(self.sensitive, bool):
            raise ValueError("migration sensitive flag must be boolean")
        if self.destination != "config" and not self.sensitive:
            raise ValueError("native, tool, and session roots require sensitive handling")

    def to_json(self) -> dict[str, object]:
        return {
            "source": self.source,
            "destination": self.destination,
            "sensitive": self.sensitive,
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = _exact_object(value, {"source", "destination", "sensitive"}, "entry")
        sensitive = raw["sensitive"]
        if not isinstance(sensitive, bool):
            raise ValueError("entry sensitive must be boolean")
        return cls(
            _string(raw["source"], "entry source"),
            _string(raw["destination"], "entry destination"),
            sensitive,
        )


@dataclass(frozen=True, slots=True)
class AgentMigrationAuthorization:
    actor: str
    entity_token: str
    home_resource_token: str
    window_id: str
    responsible_owner: str
    expires_at_ms: int

    def __post_init__(self) -> None:
        for label, value in (
            ("actor", self.actor),
            ("entity_token", self.entity_token),
            ("home_resource_token", self.home_resource_token),
            ("window_id", self.window_id),
            ("responsible_owner", self.responsible_owner),
        ):
            if not value:
                raise ValueError(f"authorization {label} must not be blank")
        if not isinstance(self.expires_at_ms, int) or isinstance(self.expires_at_ms, bool):
            raise ValueError("authorization expiry must be integer milliseconds")

    def to_json(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "entityToken": self.entity_token,
            "homeResourceToken": self.home_resource_token,
            "windowId": self.window_id,
            "responsibleOwner": self.responsible_owner,
            "expiresAtMs": self.expires_at_ms,
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = _exact_object(
            value,
            {
                "actor",
                "entityToken",
                "homeResourceToken",
                "windowId",
                "responsibleOwner",
                "expiresAtMs",
            },
            "authorization",
        )
        expiry = raw["expiresAtMs"]
        if not isinstance(expiry, int) or isinstance(expiry, bool):
            raise ValueError("authorization expiresAtMs must be an integer")
        return cls(
            _string(raw["actor"], "authorization actor"),
            _string(raw["entityToken"], "authorization entityToken"),
            _string(raw["homeResourceToken"], "authorization homeResourceToken"),
            _string(raw["windowId"], "authorization windowId"),
            _string(raw["responsibleOwner"], "authorization responsibleOwner"),
            expiry,
        )


@dataclass(frozen=True, slots=True)
class _TreeMetadata:
    path: str
    kind: str
    size: int
    mode: int
    modified_ns: int

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            "mode": self.mode,
            "modifiedNs": self.modified_ns,
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = _exact_object(value, {"path", "kind", "size", "mode", "modifiedNs"}, "tree item")
        return cls(
            _string(raw["path"], "tree item path"),
            _string(raw["kind"], "tree item kind"),
            _integer(raw["size"], "tree item size"),
            _integer(raw["mode"], "tree item mode"),
            _integer(raw["modifiedNs"], "tree item modifiedNs"),
        )


@dataclass(frozen=True, slots=True)
class _EntrySnapshot:
    entry: MigrationEntry
    items: tuple[_TreeMetadata, ...]

    def to_json(self) -> dict[str, object]:
        return {"entry": self.entry.to_json(), "items": [item.to_json() for item in self.items]}

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = _exact_object(value, {"entry", "items"}, "entry snapshot")
        if not isinstance(raw["items"], list):
            raise ValueError("entry snapshot items must be a list")
        return cls(
            MigrationEntry.from_json(raw["entry"]),
            tuple(_TreeMetadata.from_json(item) for item in raw["items"]),
        )


@dataclass(frozen=True, slots=True)
class AgentMigrationPlan:
    migration_id: str
    authorization: AgentMigrationAuthorization
    agent_uri: str
    prior_config: AgentConfig | None
    prior_config_revision: str | None
    target_config_content_digest: str | None
    entries: tuple[_EntrySnapshot, ...]
    support: tuple[SupportKey, ...]
    created_at_ms: int

    def __post_init__(self) -> None:
        if not self.migration_id or not self.agent_uri:
            raise ValueError("migration plan identity must not be blank")
        destinations = tuple(item.entry.destination for item in self.entries)
        if len(destinations) != len(set(destinations)):
            raise ValueError("migration destinations must be unique")
        if not self.entries:
            raise ValueError("migration plan needs at least one entry")

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_json(self) -> dict[str, object]:
        return {
            "schemaVersion": _SCHEMA_VERSION,
            "migrationId": self.migration_id,
            "authorization": self.authorization.to_json(),
            "agentUri": self.agent_uri,
            "priorConfig": (
                None if self.prior_config is None else self.prior_config.to_json()
            ),
            "priorConfigRevision": self.prior_config_revision,
            "targetConfigContentDigest": self.target_config_content_digest,
            "entries": [entry.to_json() for entry in self.entries],
            "support": [key.to_json() for key in self.support],
            "createdAtMs": self.created_at_ms,
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = _exact_object(
            value,
            {
                "schemaVersion",
                "migrationId",
                "authorization",
                "agentUri",
                "priorConfig",
                "priorConfigRevision",
                "targetConfigContentDigest",
                "entries",
                "support",
                "createdAtMs",
            },
            "migration plan",
        )
        if raw["schemaVersion"] != _SCHEMA_VERSION:
            raise ValueError("unsupported migration plan")
        if not isinstance(raw["entries"], list) or not isinstance(raw["support"], list):
            raise ValueError("migration plan entries/support must be lists")
        prior_revision = raw["priorConfigRevision"]
        target_content_digest = raw["targetConfigContentDigest"]
        prior_config = (
            None
            if raw["priorConfig"] is None
            else AgentConfig.from_json(raw["priorConfig"], "migration plan priorConfig")
        )
        if prior_revision is not None and not isinstance(prior_revision, str):
            raise ValueError("migration plan priorConfigRevision must be string or null")
        if target_content_digest is not None and not isinstance(
            target_content_digest, str
        ):
            raise ValueError(
                "migration plan targetConfigContentDigest must be string or null"
            )
        return cls(
            _string(raw["migrationId"], "migration plan migrationId"),
            AgentMigrationAuthorization.from_json(raw["authorization"]),
            _string(raw["agentUri"], "migration plan agentUri"),
            prior_config,
            prior_revision,
            target_content_digest,
            tuple(_EntrySnapshot.from_json(item) for item in raw["entries"]),
            tuple(SupportKey.from_json(item) for item in raw["support"]),
            _integer(raw["createdAtMs"], "migration plan createdAtMs"),
        )


class MigrationPhase(StrEnum):
    PLANNED = "planned"
    STAGED = "staged"
    PUBLISHED = "published"
    BOUND = "bound"
    COMPLETE = "complete"
    ROLLING_BACK = "rolling-back"
    ROLLED_BACK = "rolled-back"
    FAILED = "failed"
    ROLLBACK_FAILED = "rollback-failed"


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    migration_id: str
    plan_digest: str
    actor: str
    phase: MigrationPhase
    started_at_ms: int
    updated_at_ms: int
    deadline_ms: int
    next_responsible: str
    failure_phase: str | None = None
    failure_category: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "schemaVersion": _SCHEMA_VERSION,
            "migrationId": self.migration_id,
            "planDigest": self.plan_digest,
            "actor": self.actor,
            "phase": self.phase.value,
            "startedAtMs": self.started_at_ms,
            "updatedAtMs": self.updated_at_ms,
            "deadlineMs": self.deadline_ms,
            "nextResponsible": self.next_responsible,
            "failurePhase": self.failure_phase,
            "failureCategory": self.failure_category,
        }


class AgentMigrationBindings(Protocol):
    """P22/P23-owned context switch consumed by the P24 transaction."""

    def preflight(self, plan: AgentMigrationPlan) -> None: ...

    def activate(self, plan: AgentMigrationPlan) -> None: ...

    def rollback(self, plan: AgentMigrationPlan) -> None: ...

    def legacy_sources_in_use(self, plan: AgentMigrationPlan) -> tuple[str, ...]: ...


class AgentMigrationLivenessProbe(Protocol):
    """Return True only after observing this exact incarnation is stopped."""

    def __call__(self, agent_uri: str, entity_token: str) -> bool | None: ...


class AgentRuntimeMigrationBindings:
    """Bind a published migration through P22's one resolved context seam.

    This class deliberately owns no root construction.  P22 resolves every
    root, projection, and environment selector; P24 only checks that the
    returned incarnation is the one authorized by the plan and that no legacy
    source remains in any resolved root or applied launch environment.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        harnesses: Iterable[str],
        tool_profile: AgentToolProfile,
        base_environment: Mapping[str, str],
        cwd: str | None = None,
        containerized: bool = False,
    ) -> None:
        selected = tuple(harnesses)
        if not selected or len(selected) != len(set(selected)):
            raise ValueError("migration runtime harnesses must be non-empty and unique")
        if any(harness not in {"claude", "codex", "pi"} for harness in selected):
            raise ValueError("migration runtime harness must be claude, codex, or pi")
        self.registry = registry
        self.harnesses = selected
        self.tool_profile = tool_profile
        self.base_environment = dict(base_environment)
        self.cwd = cwd
        self.containerized = containerized
        self._contexts: tuple[AgentRuntimeContext, ...] = ()
        self._environments: dict[str, dict[str, str]] = {}

    @property
    def contexts(self) -> tuple[AgentRuntimeContext, ...]:
        return self._contexts

    @property
    def environments(self) -> dict[str, dict[str, str]]:
        return {
            harness: dict(environment)
            for harness, environment in self._environments.items()
        }

    def preflight(self, plan: AgentMigrationPlan) -> None:
        agent = self.registry.require(plan.authorization.actor)
        if agent.uri != plan.agent_uri or agent.entity_token != (
            plan.authorization.entity_token
        ):
            raise AgentHomeMigrationError(
                "binding-identity-mismatch", "preflight-bindings", actor=agent.actor
            )
        declared = {key.harness for key in plan.support}
        if set(self.harnesses) - declared:
            raise AgentHomeMigrationError(
                "binding-support-missing", "preflight-bindings", actor=agent.actor
            )

    def activate(self, plan: AgentMigrationPlan) -> None:
        agent = self.registry.require(plan.authorization.actor)
        receipt = self.registry.home_receipt(agent.actor)
        if any(entry.entry.destination == "config" for entry in plan.entries):
            agent = self.registry.update(
                agent.actor,
                config=AgentConfig(str(Path(receipt.path) / "config")),
            )
        cwd = self.cwd if self.cwd is not None else agent.cwd
        contexts: list[AgentRuntimeContext] = []
        environments: dict[str, dict[str, str]] = {}
        for harness in self.harnesses:
            context = resolve_agent_runtime_context(
                registry=self.registry,
                agent_name=agent.actor,
                harness=harness,
                cwd=cwd,
                tool_profile=self.tool_profile,
                containerized=self.containerized,
            )
            if context is None:
                raise AgentHomeMigrationError(
                    "runtime-context-unavailable",
                    "activate-bindings",
                    actor=agent.actor,
                )
            self._validate_context(context, agent, Path(receipt.path))
            environment = apply_runtime_environment_profile(
                self.base_environment,
                context.environment(),
            )
            if any(environment.get(name) != value for name, value in context.environment().items()):
                raise AgentHomeMigrationError(
                    "runtime-environment-mismatch",
                    "activate-bindings",
                    actor=agent.actor,
                )
            contexts.append(context)
            environments[harness] = environment
        self._contexts = tuple(contexts)
        self._environments = environments

    def rollback(self, plan: AgentMigrationPlan) -> None:
        agent = self.registry.require(plan.authorization.actor)
        self.registry.update(agent.actor, config=plan.prior_config)
        self._contexts = ()
        self._environments = {}

    def legacy_sources_in_use(self, plan: AgentMigrationPlan) -> tuple[str, ...]:
        if len(self._contexts) != len(self.harnesses):
            return tuple(snapshot.entry.source for snapshot in plan.entries)
        candidates: list[Path] = []
        for context in self._contexts:
            candidates.extend(
                (
                    context.roots.agent_home,
                    context.roots.config_source,
                    context.roots.projection_root,
                    context.roots.native_root,
                    context.roots.session_root,
                    context.roots.tool_home,
                    context.roots.xdg_config_home,
                    context.roots.xdg_cache_home,
                    context.roots.xdg_data_home,
                    context.roots.xdg_state_home,
                    context.roots.tool_profile_root,
                )
            )
            candidates.extend(
                Path(value)
                for value in self._environments[context.harness].values()
                if Path(value).is_absolute()
            )
        return tuple(
            snapshot.entry.source
            for snapshot in plan.entries
            if any(
                candidate == Path(snapshot.entry.source)
                or _is_within(candidate, Path(snapshot.entry.source))
                for candidate in candidates
            )
        )

    @staticmethod
    def _validate_context(
        context: AgentRuntimeContext, agent: Agent, agent_home: Path
    ) -> None:
        if (
            context.actor != agent.uri
            or context.entity_token != agent.entity_token
            or context.roots.agent_home != agent_home
            or agent.config is None
            or context.roots.config_source != Path(agent.config.source)
        ):
            raise AgentHomeMigrationError(
                "runtime-context-mismatch", "activate-bindings", actor=agent.actor
            )
        for root in (
            context.roots.projection_root,
            context.roots.native_root,
            context.roots.session_root,
            context.roots.tool_home,
            context.roots.xdg_config_home,
            context.roots.xdg_cache_home,
            context.roots.xdg_data_home,
            context.roots.xdg_state_home,
            context.roots.tool_profile_root,
        ):
            if not _is_within(root, agent_home):
                raise AgentHomeMigrationError(
                    "runtime-root-escape", "activate-bindings", actor=agent.actor
                )


class AgentMigrationCoordinator:
    """Preflight, publish, bind, retire, and roll back one agent at a time."""

    def __init__(
        self,
        registry: AgentRegistry,
        support: SupportMatrix,
        *,
        liveness_probe: AgentMigrationLivenessProbe,
        clock_ms: Callable[[], int] | None = None,
        copy_file: Callable[[Path, Path], None] | None = None,
    ) -> None:
        self.registry = registry
        self.support = support
        self._liveness_probe = liveness_probe
        self._clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._copy_file = copy_file or _copy_regular_file

    def preflight(
        self,
        authorization: AgentMigrationAuthorization,
        entries: Iterable[MigrationEntry],
        *,
        required_support: Iterable[SupportKey] = (),
        bindings: AgentMigrationBindings,
    ) -> AgentMigrationPlan:
        agent, receipt = self._authorize(authorization, "preflight")
        requested = tuple(entries)
        if not requested:
            raise AgentHomeMigrationError("empty-plan", "preflight", actor=agent.actor)
        destinations = [entry.destination for entry in requested]
        if len(destinations) != len(set(destinations)):
            raise AgentHomeMigrationError("duplicate-target", "preflight", actor=agent.actor)
        sources = [Path(entry.source) for entry in requested]
        if any(
            left == right or _is_within(left, right) or _is_within(right, left)
            for index, left in enumerate(sources)
            for right in sources[index + 1 :]
        ):
            raise AgentHomeMigrationError(
                "overlapping-source", "preflight-source", actor=agent.actor
            )
        config_entries = [entry for entry in requested if entry.destination == "config"]
        if (
            config_entries
            and agent.config is not None
            and Path(config_entries[0].source) != Path(agent.config.source)
        ):
            raise AgentHomeMigrationError(
                "config-source-mismatch", "preflight-config", actor=agent.actor
            )
        keys = tuple(required_support)
        declared_harnesses = {key.harness for key in keys}
        native_harnesses = {
            entry.destination.removeprefix("secrets/native/")
            for entry in requested
            if entry.destination.startswith("secrets/native/")
        }
        missing_support = native_harnesses - declared_harnesses
        if missing_support:
            raise AgentHomeMigrationError(
                "support-not-declared", "preflight-support", actor=agent.actor
            )
        self.support.require_pass(keys, actor=agent.actor)
        root = Path(receipt.path)
        snapshots: list[_EntrySnapshot] = []
        for entry in requested:
            source = Path(entry.source)
            if source.absolute() != source.resolve(strict=False):
                raise AgentHomeMigrationError(
                    "source-ancestor-link", "preflight-source", actor=agent.actor
                )
            if _is_within(source, root) or _is_within(root, source):
                raise AgentHomeMigrationError("overlapping-root", "preflight", actor=agent.actor)
            items = _snapshot_tree(source, agent.actor, private=entry.sensitive)
            target = root / entry.destination
            _validate_target_slot(target, root, agent.actor)
            snapshots.append(_EntrySnapshot(entry, items))
        prior_config_revision = _config_revision(agent)
        target_config_content_digest = _agent_config_content_digest(agent)
        if config_entries:
            try:
                candidate_config = AgentConfig(config_entries[0].source)
                candidate_manifest = candidate_config.freeze_manifest()
                for harness in ("claude", "codex", "pi"):
                    build_native_projection(candidate_manifest, harness)
                target_config_content_digest = _config_content_digest(
                    candidate_manifest
                )
            except AgentConfigError as error:
                raise AgentHomeMigrationError(
                    "invalid-config-source", "preflight-config", actor=agent.actor
                ) from error
        plan = AgentMigrationPlan(
            uuid.uuid4().hex,
            authorization,
            agent.uri,
            agent.config,
            prior_config_revision,
            target_config_content_digest,
            tuple(snapshots),
            keys,
            self._clock(),
        )
        _require_bindings(bindings, plan)
        bindings.preflight(plan)
        return plan

    def execute(
        self, plan: AgentMigrationPlan, *, bindings: AgentMigrationBindings
    ) -> MigrationRecord:
        agent, receipt = self._authorize(plan.authorization, "execute")
        if agent.uri != plan.agent_uri:
            raise AgentHomeMigrationError("identity-drift", "execute", actor=agent.actor)
        self.support.require_pass(plan.support, actor=agent.actor)
        _require_bindings(bindings, plan)
        root = Path(receipt.path)
        migrations_root = root / "state" / "migrations"
        _ensure_private_directory(migrations_root, agent.actor, "migration-root")
        transaction = migrations_root / plan.migration_id
        record_path = transaction / "record.json"
        plan_path = transaction / "plan.json"
        existing = _read_record(record_path, agent.actor)
        if (
            existing is None
            or existing.phase in {MigrationPhase.PLANNED, MigrationPhase.STAGED}
        ) and _config_revision(agent) != plan.prior_config_revision:
            raise AgentHomeMigrationError(
                "config-revision-drift", "execute", actor=agent.actor
            )
        if existing is not None:
            if existing.plan_digest != plan.digest:
                raise AgentHomeMigrationError("plan-mismatch", "resume", actor=agent.actor)
            if existing.phase is MigrationPhase.COMPLETE:
                return existing
            if existing.phase in {
                MigrationPhase.ROLLING_BACK,
                MigrationPhase.FAILED,
                MigrationPhase.ROLLBACK_FAILED,
            }:
                raise AgentHomeMigrationError("failed-transaction", "resume", actor=agent.actor)
        _ensure_private_directory(transaction, agent.actor, "transaction")
        persisted_plan = _read_plan(plan_path, agent.actor)
        if persisted_plan is None:
            _write_json(plan_path, plan.to_json())
        elif persisted_plan != plan:
            raise AgentHomeMigrationError("plan-mismatch", "resume", actor=agent.actor)
        started = self._clock()
        record = existing or MigrationRecord(
            plan.migration_id,
            plan.digest,
            agent.actor,
            MigrationPhase.PLANNED,
            started,
            started,
            plan.authorization.expires_at_ms,
            plan.authorization.responsible_owner,
        )
        _write_record(record_path, record)
        activated = record.phase in {MigrationPhase.BOUND}
        operation_phase = record.phase.value
        try:
            stage_root = transaction / "staging"
            if record.phase is MigrationPhase.PLANNED:
                operation_phase = "stage"
                if stage_root.exists():
                    shutil.rmtree(stage_root)
                _ensure_private_directory(stage_root, agent.actor, "stage-root")
                for index, snapshot in enumerate(plan.entries):
                    source = Path(snapshot.entry.source)
                    if _snapshot_tree(
                        source, agent.actor, private=snapshot.entry.sensitive
                    ) != snapshot.items:
                        raise AgentHomeMigrationError(
                            "source-drift", "stage", actor=agent.actor
                        )
                    _copy_tree(
                        source,
                        stage_root / str(index),
                        agent.actor,
                        self._copy_file,
                    )
                record = self._advance(record_path, record, MigrationPhase.STAGED)

            if record.phase is MigrationPhase.STAGED:
                operation_phase = "publish"
                backup_root = transaction / "target-backups"
                _ensure_private_directory(backup_root, agent.actor, "backup-root")
                for index, snapshot in enumerate(plan.entries):
                    target = root / snapshot.entry.destination
                    staged = stage_root / str(index)
                    backup = backup_root / str(index)
                    if staged.exists():
                        _validate_target_slot(target, root, agent.actor)
                        if target.exists():
                            if backup.exists():
                                raise AgentHomeMigrationError(
                                    "backup-occupied", "publish", actor=agent.actor
                                )
                            os.rename(target, backup)
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        _safe_target_parents(target.parent, root, agent.actor)
                        os.rename(staged, target)
                        _fsync_directory(target.parent)
                        _fsync_directory(backup_root)
                    elif not target.exists() or not _matches_shape(
                        target, snapshot, agent.actor
                    ):
                        raise AgentHomeMigrationError(
                            "publish-resume-drift", "publish", actor=agent.actor
                        )
                _fsync_directory(root)
                record = self._advance(record_path, record, MigrationPhase.PUBLISHED)

            if record.phase is MigrationPhase.PUBLISHED:
                operation_phase = "activate-bindings"
                bindings.activate(plan)
                activated = True
                activated_agent = self.registry.require(agent.actor)
                has_config_migration = any(
                    entry.entry.destination == "config" for entry in plan.entries
                )
                config_matches = (
                    _agent_config_content_digest(activated_agent)
                    == plan.target_config_content_digest
                    if has_config_migration
                    else _config_revision(activated_agent)
                    == plan.prior_config_revision
                )
                if not config_matches:
                    raise AgentHomeMigrationError(
                        "binding-config-mismatch",
                        "activate-bindings",
                        actor=agent.actor,
                    )
                record = self._advance(record_path, record, MigrationPhase.BOUND)

            if record.phase is MigrationPhase.BOUND:
                operation_phase = "source-exit"
                in_use = bindings.legacy_sources_in_use(plan)
                if in_use:
                    raise AgentHomeMigrationError(
                        "legacy-source-in-use", "source-exit", actor=agent.actor
                    )
                for index, snapshot in enumerate(plan.entries):
                    source = Path(snapshot.entry.source)
                    retired_path = source.parent / (
                        f".{source.name}.hyprial-retired-{plan.migration_id}-{index}"
                    )
                    if source.exists() and not os.path.lexists(retired_path):
                        os.rename(source, retired_path)
                        _fsync_directory(source.parent)
                    elif source.exists() or not os.path.lexists(retired_path):
                        raise AgentHomeMigrationError(
                            "source-exit-drift", "source-exit", actor=agent.actor
                        )
                record = self._advance(record_path, record, MigrationPhase.COMPLETE)
            return record
        except BaseException as error:
            category = error.category if isinstance(error, AgentHomeMigrationError) else type(error).__name__
            phase = error.phase if isinstance(error, AgentHomeMigrationError) else operation_phase
            compensation_errors: list[str] = []
            for index, snapshot in reversed(tuple(enumerate(plan.entries))):
                source = Path(snapshot.entry.source)
                retired_path = source.parent / (
                    f".{source.name}.hyprial-retired-{plan.migration_id}-{index}"
                )
                try:
                    if retired_path.exists() and not source.exists():
                        os.rename(retired_path, source)
                        _fsync_directory(source.parent)
                    elif retired_path.exists() and source.exists():
                        raise OSError("source and retired source both exist")
                except BaseException as compensation_error:
                    compensation_errors.append(type(compensation_error).__name__)
            if activated or record.phase in {
                MigrationPhase.PUBLISHED,
                MigrationPhase.BOUND,
            }:
                try:
                    bindings.rollback(plan)
                except BaseException as compensation_error:
                    compensation_errors.append(type(compensation_error).__name__)
            backup_root = transaction / "target-backups"
            for index, snapshot in reversed(tuple(enumerate(plan.entries))):
                target = root / snapshot.entry.destination
                staged = stage_root / str(index)
                backup = backup_root / str(index)
                try:
                    if not staged.exists() and target.exists() and _matches_shape(
                        target, snapshot, agent.actor
                    ):
                        shutil.rmtree(target)
                    if backup.exists() and not target.exists():
                        os.rename(backup, target)
                    _fsync_directory(target.parent)
                except BaseException as compensation_error:
                    compensation_errors.append(type(compensation_error).__name__)
            try:
                stage_root = transaction / "staging"
                if stage_root.exists():
                    shutil.rmtree(stage_root)
            except BaseException as compensation_error:
                compensation_errors.append(type(compensation_error).__name__)
            failed = MigrationRecord(
                record.migration_id,
                record.plan_digest,
                record.actor,
                MigrationPhase.ROLLBACK_FAILED if compensation_errors else MigrationPhase.FAILED,
                record.started_at_ms,
                self._clock(),
                record.deadline_ms,
                record.next_responsible,
                phase,
                category if not compensation_errors else f"{category}+compensation-error",
            )
            _write_record(record_path, failed)
            if isinstance(error, AgentHomeMigrationError):
                raise
            raise AgentHomeMigrationError(category, phase, actor=agent.actor) from error

    def rollback(
        self,
        plan: AgentMigrationPlan,
        *,
        authorization: AgentMigrationAuthorization,
        bindings: AgentMigrationBindings,
    ) -> MigrationRecord:
        agent, receipt = self._authorize(authorization, "rollback")
        if (
            authorization.actor != plan.authorization.actor
            or authorization.entity_token != plan.authorization.entity_token
            or authorization.home_resource_token
            != plan.authorization.home_resource_token
        ):
            raise AgentHomeMigrationError(
                "rollback-authorization-mismatch", "rollback", actor=agent.actor
            )
        _require_bindings(bindings, plan)
        root = Path(receipt.path)
        transaction = root / "state" / "migrations" / plan.migration_id
        record_path = transaction / "record.json"
        record = _read_record(record_path, agent.actor)
        if record is None or record.plan_digest != plan.digest:
            raise AgentHomeMigrationError("unknown-transaction", "rollback", actor=agent.actor)
        if record.phase is MigrationPhase.ROLLED_BACK:
            return record
        if record.phase not in {
            MigrationPhase.COMPLETE,
            MigrationPhase.ROLLING_BACK,
        }:
            raise AgentHomeMigrationError("not-complete", "rollback", actor=agent.actor)
        quarantine = transaction / "rolled-back-targets"
        _ensure_private_directory(quarantine, agent.actor, "rollback-quarantine")
        try:
            if record.phase is MigrationPhase.COMPLETE:
                for index, snapshot in enumerate(plan.entries):
                    source = Path(snapshot.entry.source)
                    retired_path = source.parent / (
                        f".{source.name}.hyprial-retired-{plan.migration_id}-{index}"
                    )
                    target = root / snapshot.entry.destination
                    if (
                        source.exists()
                        or not retired_path.exists()
                        or not target.exists()
                        or (quarantine / str(index)).exists()
                    ):
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                    if _tree_shape(
                        _snapshot_tree(target, agent.actor, private=True)
                    ) != _tree_shape(snapshot.items):
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                record = self._advance(
                    record_path, record, MigrationPhase.ROLLING_BACK
                )
            bindings.rollback(plan)
            for index, snapshot in reversed(tuple(enumerate(plan.entries))):
                source = Path(snapshot.entry.source)
                retired_path = source.parent / (
                    f".{source.name}.hyprial-retired-{plan.migration_id}-{index}"
                )
                target = root / snapshot.entry.destination
                rolled_back_target = quarantine / str(index)
                if not rolled_back_target.exists():
                    if source.exists() or not retired_path.exists() or not target.exists():
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                    if _tree_shape(
                        _snapshot_tree(target, agent.actor, private=True)
                    ) != _tree_shape(snapshot.items):
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                    os.rename(target, rolled_back_target)
                    _fsync_directory(quarantine)
                elif _tree_shape(
                    _snapshot_tree(rolled_back_target, agent.actor, private=True)
                ) != _tree_shape(snapshot.items):
                    raise AgentHomeMigrationError(
                        "rollback-drift", "rollback", actor=agent.actor
                    )
                if not source.exists():
                    if not retired_path.exists():
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                    os.rename(retired_path, source)
                elif retired_path.exists():
                    raise AgentHomeMigrationError(
                        "rollback-drift", "rollback", actor=agent.actor
                    )
                _fsync_directory(source.parent)
                backup = transaction / "target-backups" / str(index)
                if backup.exists():
                    if target.exists():
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                    os.rename(backup, target)
                _fsync_directory(target.parent)
            if _config_revision(self.registry.require(agent.actor)) != (
                plan.prior_config_revision
            ):
                raise AgentHomeMigrationError(
                    "rollback-config-mismatch", "rollback", actor=agent.actor
                )
            return self._advance(record_path, record, MigrationPhase.ROLLED_BACK)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            category = (
                error.category
                if isinstance(error, AgentHomeMigrationError)
                else type(error).__name__
            )
            phase = (
                error.phase
                if isinstance(error, AgentHomeMigrationError)
                else "rollback"
            )
            failed = MigrationRecord(
                record.migration_id,
                record.plan_digest,
                record.actor,
                MigrationPhase.ROLLBACK_FAILED,
                record.started_at_ms,
                self._clock(),
                record.deadline_ms,
                record.next_responsible,
                phase,
                category,
            )
            _write_record(record_path, failed)
            if isinstance(error, AgentHomeMigrationError):
                raise
            raise AgentHomeMigrationError(category, phase, actor=agent.actor) from error

    def _authorize(self, authorization: AgentMigrationAuthorization, phase: str):
        agent = self.registry.require(authorization.actor)
        if self._clock() > authorization.expires_at_ms:
            raise AgentHomeMigrationError("authorization-expired", phase, actor=agent.actor)
        if agent.entity_token != authorization.entity_token:
            raise AgentHomeMigrationError("incarnation-mismatch", phase, actor=agent.actor)
        receipt = self.registry.home_receipt(agent.actor)
        if receipt.resource_token != authorization.home_resource_token:
            raise AgentHomeMigrationError("home-receipt-mismatch", phase, actor=agent.actor)
        try:
            stopped = self._liveness_probe(agent.uri, agent.entity_token)
        except Exception as error:
            raise AgentHomeMigrationError(
                "liveness-unknown", phase, actor=agent.actor
            ) from error
        if stopped is not True:
            category = "liveness-unknown" if stopped is None else "agent-running"
            raise AgentHomeMigrationError(category, phase, actor=agent.actor)
        return agent, receipt

    def _advance(
        self, path: Path, record: MigrationRecord, phase: MigrationPhase
    ) -> MigrationRecord:
        updated = MigrationRecord(
            record.migration_id,
            record.plan_digest,
            record.actor,
            phase,
            record.started_at_ms,
            self._clock(),
            record.deadline_ms,
            record.next_responsible,
        )
        _write_record(path, updated)
        return updated


def _require_bindings(bindings: object, plan: AgentMigrationPlan) -> None:
    for name in ("preflight", "activate", "rollback", "legacy_sources_in_use"):
        if not callable(getattr(bindings, name, None)):
            raise AgentHomeMigrationError(
                "binding-facade-incomplete",
                "preflight-bindings",
                actor=plan.authorization.actor,
            )


def _config_revision(agent: Agent) -> str | None:
    config = agent.config
    return None if config is None else config.freeze_manifest().digest


def _agent_config_content_digest(agent: Agent) -> str | None:
    config = agent.config
    return None if config is None else _config_content_digest(config.freeze_manifest())


def _config_content_digest(manifest: ConfigManifest) -> str:
    encoded = json.dumps(
        [item.to_json() for item in manifest.items],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_tree(
    source: Path, actor: str, *, private: bool
) -> tuple[_TreeMetadata, ...]:
    try:
        root_stat = source.lstat()
    except OSError as error:
        raise AgentHomeMigrationError("source-unreadable", "preflight-source", actor=actor) from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise AgentHomeMigrationError("unsafe-source", "preflight-source", actor=actor)
    if private and (
        root_stat.st_uid != os.getuid()
        or stat.S_IMODE(root_stat.st_mode) & 0o077
    ):
        raise AgentHomeMigrationError("unsafe-source-mode", "preflight-source", actor=actor)
    items: list[_TreeMetadata] = []
    try:
        for root, directories, files in os.walk(source, topdown=True, followlinks=False):
            root_path = Path(root)
            for name, kind in ((*[(name, "directory") for name in directories], *[(name, "file") for name in files])):
                path = root_path / name
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise AgentHomeMigrationError("unsafe-source", "preflight-source", actor=actor)
                if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
                    raise AgentHomeMigrationError("unsafe-source", "preflight-source", actor=actor)
                if kind == "file" and not stat.S_ISREG(metadata.st_mode):
                    raise AgentHomeMigrationError("unsafe-source", "preflight-source", actor=actor)
                if private and (
                    metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                ):
                    raise AgentHomeMigrationError(
                        "unsafe-source-mode", "preflight-source", actor=actor
                    )
                items.append(
                    _TreeMetadata(
                        path.relative_to(source).as_posix(),
                        kind,
                        metadata.st_size if kind == "file" else 0,
                        stat.S_IMODE(metadata.st_mode),
                        metadata.st_mtime_ns,
                    )
                )
    except AgentHomeMigrationError:
        raise
    except OSError as error:
        raise AgentHomeMigrationError("source-unreadable", "preflight-source", actor=actor) from error
    return tuple(sorted(items, key=lambda item: item.path))


def _copy_tree(
    source: Path,
    destination: Path,
    actor: str,
    copy_file: Callable[[Path, Path], None],
) -> None:
    if destination.exists():
        raise AgentHomeMigrationError("stage-exists", "stage", actor=actor)
    destination.mkdir(mode=0o700)
    for root, directories, files in os.walk(source, topdown=True, followlinks=False):
        relative = Path(root).relative_to(source)
        target_root = destination / relative
        for name in directories:
            path = Path(root) / name
            if path.is_symlink():
                raise AgentHomeMigrationError("unsafe-source", "stage", actor=actor)
            (target_root / name).mkdir(mode=0o700)
        for name in files:
            path = Path(root) / name
            if path.is_symlink():
                raise AgentHomeMigrationError("unsafe-source", "stage", actor=actor)
            copy_file(path, target_root / name)
    _fsync_tree(destination)


def _copy_regular_file(source: Path, destination: Path) -> None:
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError("source changed from a regular file")
    input_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(input_fd, "rb", closefd=False) as input_stream, os.fdopen(
            output_fd, "wb", closefd=False
        ) as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    finally:
        os.close(input_fd)
        os.close(output_fd)
    after = source.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise OSError("source changed while it was copied")


def _validate_target_slot(target: Path, agent_root: Path, actor: str) -> None:
    if not _is_within(target, agent_root):
        raise AgentHomeMigrationError("target-escape", "preflight-target", actor=actor)
    current = agent_root
    for part in target.relative_to(agent_root).parts[:-1]:
        current /= part
        if current.exists():
            metadata = current.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise AgentHomeMigrationError("unsafe-target", "preflight-target", actor=actor)
    if not target.exists():
        return
    metadata = target.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentHomeMigrationError("target-occupied", "preflight-target", actor=actor)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise AgentHomeMigrationError("unsafe-target", "preflight-target", actor=actor)
    try:
        if any(target.iterdir()):
            raise AgentHomeMigrationError("target-occupied", "preflight-target", actor=actor)
    except OSError as error:
        raise AgentHomeMigrationError("target-unreadable", "preflight-target", actor=actor) from error


def _safe_private_directory(path: Path, actor: str, phase: str) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AgentHomeMigrationError("unsafe-state", phase, actor=actor)


def _ensure_private_directory(path: Path, actor: str, phase: str) -> None:
    if not os.path.lexists(path):
        path.mkdir(mode=0o700)
    _safe_private_directory(path, actor, phase)


def _safe_target_parents(path: Path, root: Path, actor: str) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        metadata = current.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise AgentHomeMigrationError(
                "unsafe-target", "publish", actor=actor
            )


def _write_record(path: Path, record: MigrationRecord) -> None:
    _write_json(path, record.to_json())


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _read_plan(path: Path, actor: str) -> AgentMigrationPlan | None:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise AgentHomeMigrationError("unsafe-plan", "resume", actor=actor)
        return AgentMigrationPlan.from_json(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except AgentHomeMigrationError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise AgentHomeMigrationError("invalid-plan", "resume", actor=actor) from error


def _read_record(path: Path, actor: str) -> MigrationRecord | None:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("unsafe migration record")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise AgentHomeMigrationError("invalid-record", "resume", actor=actor) from error
    try:
        value = _exact_object(
            raw,
            {
                "schemaVersion",
                "migrationId",
                "planDigest",
                "actor",
                "phase",
                "startedAtMs",
                "updatedAtMs",
                "deadlineMs",
                "nextResponsible",
                "failurePhase",
                "failureCategory",
            },
            "migration record",
        )
        if value["schemaVersion"] != _SCHEMA_VERSION:
            raise ValueError("unsupported migration record")
        failure_phase = value["failurePhase"]
        failure_category = value["failureCategory"]
        if failure_phase is not None and not isinstance(failure_phase, str):
            raise ValueError("record failurePhase must be string or null")
        if failure_category is not None and not isinstance(failure_category, str):
            raise ValueError("record failureCategory must be string or null")
        return MigrationRecord(
            _string(value["migrationId"], "record migrationId"),
            _string(value["planDigest"], "record planDigest"),
            _string(value["actor"], "record actor"),
            MigrationPhase(_string(value["phase"], "record phase")),
            _integer(value["startedAtMs"], "record startedAtMs"),
            _integer(value["updatedAtMs"], "record updatedAtMs"),
            _integer(value["deadlineMs"], "record deadlineMs"),
            _string(value["nextResponsible"], "record nextResponsible"),
            failure_phase,
            failure_category,
        )
    except (ValueError, KeyError) as error:
        raise AgentHomeMigrationError("invalid-record", "resume", actor=actor) from error


def _fsync_tree(root: Path) -> None:
    for current, directories, _files in os.walk(root, topdown=False):
        for directory in directories:
            _fsync_directory(Path(current) / directory)
        _fsync_directory(Path(current))


def _tree_shape(items: tuple[_TreeMetadata, ...]) -> tuple[tuple[str, str, int], ...]:
    return tuple((item.path, item.kind, item.size) for item in items)


def _matches_shape(target: Path, snapshot: _EntrySnapshot, actor: str) -> bool:
    try:
        return _tree_shape(_snapshot_tree(target, actor, private=True)) == _tree_shape(
            snapshot.items
        )
    except AgentHomeMigrationError:
        return False


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _validate_relative(value: str, label: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or value != path.as_posix() or ".." in path.parts:
        raise ValueError(f"{label} must be a normalized relative path")


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return value
