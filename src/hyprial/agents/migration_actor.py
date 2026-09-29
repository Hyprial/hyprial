"""Typed Agent-owner bridge for the operator's filesystem migration saga."""

from __future__ import annotations

import os
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .home import AgentHomeProvisioner, HomeReceipt
from .config import AgentConfig
from .migration import (
    AgentHomeMigrationError,
    AgentMigrationCoordinator,
    AgentRuntimeMigrationBindings,
)
from .migration_entry import (
    AgentMigrationPlanStore,
    MigrationAuthorizationWindow,
    MigrationPreflightManifest,
    load_packaged_support_matrix,
)
from .registry import Agent, AgentError, AgentHomeError
from .runtime import DEFAULT_AGENT_TOOL_PROFILE


@dataclass(frozen=True, slots=True)
class MigrationRequest:
    operation_id: str
    method: str
    actor: str
    manifest: MigrationPreflightManifest | None = None
    migration_id: str | None = None
    authorization_window: MigrationAuthorizationWindow | None = None


@dataclass(frozen=True, slots=True)
class MigrationEffect:
    request: MigrationRequest
    agent: Agent
    receipt: HomeReceipt
    state_dir: Path
    hyprial_home: Path


@dataclass(frozen=True, slots=True)
class MigrationOwnerCall:
    operation_id: str
    call_id: str
    kind: Literal["snapshot", "home-receipt", "stopped", "update-config"]
    actor: str
    expected_entity_token: str
    config: AgentConfig | None = None


@dataclass(frozen=True, slots=True)
class MigrationOwnerResult:
    value: Agent | HomeReceipt | bool | None = None
    failure: MigrationFailure | None = None


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    result_json: str | None = None
    failure: MigrationFailure | None = None

    @classmethod
    def success(cls, result: dict[str, object]) -> MigrationOutcome:
        return cls(json.dumps(result, sort_keys=True, allow_nan=False))

    @classmethod
    def failed(cls, error: BaseException) -> MigrationOutcome:
        return cls(failure=MigrationFailure.from_exception(error))

    def result(self) -> dict[str, object]:
        if self.failure is not None:
            raise self.failure.to_exception()
        if self.result_json is None:
            raise RuntimeError("migration completed without a result")
        value = json.loads(self.result_json)
        if not isinstance(value, dict):
            raise RuntimeError("migration result must be an object")
        return value


@dataclass(frozen=True, slots=True)
class MigrationFailure:
    kind: str
    message: str
    code: str | None = None
    category: str | None = None
    phase: str | None = None
    actor: str | None = None

    @classmethod
    def from_exception(cls, error: BaseException) -> MigrationFailure:
        return cls(
            type(error).__name__, str(error)[:1000],
            getattr(error, "code", None),
            getattr(error, "category", None),
            getattr(error, "phase", None),
            getattr(error, "actor", None),
        )

    def to_exception(self) -> Exception:
        if self.kind == "AgentHomeMigrationError" and all(
            value is not None for value in (self.category, self.phase, self.actor)
        ):
            return AgentHomeMigrationError(
                self.category or "unknown", self.phase or "unknown",
                actor=self.actor or "unknown",
            )
        if self.kind == "AgentHomeError" and all(
            value is not None for value in (self.category, self.phase, self.actor)
        ):
            return AgentHomeError(
                self.category or "unknown", self.actor or "unknown",
                self.phase or "unknown",
            )
        if self.kind == "ValueError":
            return ValueError(self.message)
        if self.kind == "TypeError":
            return TypeError(self.message)
        if self.kind == "OSError":
            return OSError(self.message)
        if self.code is not None:
            error = AgentError(self.message)
            error.code = self.code
            return error
        return RuntimeError(self.message)


class MigrationOwnerPort(Protocol):
    def migration_owner_call(self, command: MigrationOwnerCall) -> MigrationOwnerResult: ...


class _RegistryProxy:
    """Frozen identity/home view; all mutation and liveness proofs return to owner."""

    def __init__(self, effect: MigrationEffect, owner: MigrationOwnerPort) -> None:
        self._effect = effect
        self._owner = owner
        self._agent = effect.agent
        self._receipt = effect.receipt
        self._home = AgentHomeProvisioner(effect.hyprial_home)
        self._counter = 0
        self._lock = threading.Lock()

    def _call(
        self,
        kind: Literal["snapshot", "home-receipt", "stopped", "update-config"],
        config: AgentConfig | None = None,
    ) -> Agent | HomeReceipt | bool | None:
        with self._lock:
            self._counter += 1
            call_id = f"{self._effect.request.operation_id}:{self._counter}"
        response = self._owner.migration_owner_call(
            MigrationOwnerCall(
                self._effect.request.operation_id,
                call_id,
                kind,
                self._agent.actor,
                self._agent.entity_token,
                config,
            )
        )
        if response.failure is not None:
            raise response.failure.to_exception()
        return response.value

    def require(self, actor: str) -> Agent:
        if actor not in {self._agent.actor, self._agent.uri}:
            raise ValueError("migration identity changed")
        current = self._call("snapshot")
        if not isinstance(current, Agent):
            raise RuntimeError("migration owner returned invalid Agent snapshot")
        self._agent = current
        return current

    def home_receipt(
        self, actor: str, *, require_ready: bool = True,
        validate_mirror: bool = True,
    ) -> HomeReceipt:
        self.require(actor)
        current = self._call("home-receipt")
        if not isinstance(current, HomeReceipt):
            raise RuntimeError("migration owner returned invalid home receipt")
        if require_ready and current.status != "ready":
            raise RuntimeError("migration home is no longer ready")
        if validate_mirror:
            self._home.validate(current)
        self._receipt = current
        return current

    def update(self, actor: str, **changes: Any) -> Agent:
        if set(changes) != {"config"}:
            raise ValueError("migration may update only Agent config")
        self.require(actor)
        current = self._call("update-config", changes["config"])
        if not isinstance(current, Agent):
            raise RuntimeError("migration owner returned invalid updated Agent")
        self._agent = current
        return current

    def stopped(self, actor: str, entity_token: str) -> bool | None:
        if actor != self._agent.uri or entity_token != self._agent.entity_token:
            return None
        return self._call("stopped")


def run_migration_effect(effect: MigrationEffect, owner: MigrationOwnerPort) -> MigrationOutcome:
    """Run native/FS work off mailbox with only typed reads/CAS from its owner."""

    proxy = _RegistryProxy(effect, owner)
    request = effect.request
    try:
        agent = proxy.require(request.actor)
        store = AgentMigrationPlanStore(effect.state_dir)
        coordinator = AgentMigrationCoordinator(
            proxy, load_packaged_support_matrix(), liveness_probe=proxy.stopped
        )
        if request.method == "agent.migrate.preflight":
            manifest = request.manifest
            if manifest is None:
                raise ValueError("migration manifest is required")
            authorization = manifest.authorization_window.bind(proxy, agent.actor)
            keys = manifest.required_support
            bindings = AgentRuntimeMigrationBindings(
                proxy,
                harnesses=tuple(dict.fromkeys(key.harness for key in keys)),
                tool_profile=DEFAULT_AGENT_TOOL_PROFILE,
                base_environment=os.environ,
                containerized=False,
            )
            plan = coordinator.preflight(
                authorization, manifest.entries, required_support=keys,
                bindings=bindings,
            )
            store.save(plan)
            return MigrationOutcome.success({
                "ok": True,
                "agent": plan.agent_uri,
                "migrationId": plan.migration_id,
                "planDigest": plan.digest,
                "createdAtMs": plan.created_at_ms,
                "persisted": True,
            })
        if request.migration_id is None:
            raise ValueError("migrationId is required")
        plan = store.load(agent.actor, request.migration_id)
        bindings = AgentRuntimeMigrationBindings(
            proxy,
            harnesses=tuple(dict.fromkeys(key.harness for key in plan.support)),
            tool_profile=DEFAULT_AGENT_TOOL_PROFILE,
            base_environment=os.environ,
            containerized=False,
        )
        if request.method == "agent.migrate.execute":
            record = coordinator.execute(plan, bindings=bindings)
        elif request.method == "agent.migrate.rollback":
            window = request.authorization_window
            if window is None:
                raise ValueError("migration rollback authorization is required")
            record = coordinator.rollback(
                plan, authorization=window.bind(proxy, agent.actor), bindings=bindings,
            )
        else:
            raise ValueError("unsupported migration operation")
        return MigrationOutcome.success({"ok": True, "migration": record.to_json()})
    except BaseException as error:
        return MigrationOutcome.failed(error)
