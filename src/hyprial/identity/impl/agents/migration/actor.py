"""Typed Agent-owner bridge for the operator's filesystem migration saga."""

from __future__ import annotations

import os
import json
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from hyprial.identity.impl.agents.home.provisioner  import AgentHomeProvisioner, HomeReceipt
from hyprial.identity.impl.agents.home.config  import AgentConfig
from hyprial.identity.impl.agents.migration._base  import (
    AgentHomeMigrationError,
    )
from hyprial.identity.impl.agents.migration.coordinator  import (
    AgentMigrationCoordinator,
    )
from hyprial.identity.impl.agents.migration._bindings  import (
    AgentRuntimeMigrationBindings,
)
from hyprial.identity.impl.agents.migration.entry  import (
    AgentMigrationPlanStore,
    MigrationAuthorizationWindow,
    MigrationPreflightManifest,
    load_packaged_support_matrix,
)
from hyprial.identity.impl.agents.registry._base  import Agent, AgentError
from hyprial.identity.impl.agents.home.provisioner  import AgentHomeError
from hyprial.identity.impl.agents.runtime.context  import DEFAULT_AGENT_TOOL_PROFILE
from hyprial.identity.impl.agents.config_rebind import (
    ConfigRebindError,
    run_config_rebind,
)
from hyprial.identity.impl.agents.home.effects import HomeRuntimePreparer
from hyprial.kernel import DECLARED_HARNESSES, ipc_errors


@dataclass(frozen=True, slots=True)
class MigrationRequest:
    operation_id: str
    method: str
    actor: str
    manifest: MigrationPreflightManifest | None = None
    migration_id: str | None = None
    authorization_window: MigrationAuthorizationWindow | None = None
    source: str | None = None
    rollback_receipt_id: str | None = None
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class MigrationEffect:
    request: MigrationRequest
    agent: Agent
    receipt: HomeReceipt
    state_dir: Path
    hyprial_home: Path
    runtime_preparers: tuple[tuple[str, HomeRuntimePreparer], ...] = ()


@dataclass(frozen=True, slots=True)
class MigrationOwnerCall:
    operation_id: str
    call_id: str
    kind: Literal[
        "snapshot", "home-receipt", "stopped", "update-config",
        "bind-config-quiescent", "quiescence",
    ]
    actor: str
    expected_entity_token: str
    config: AgentConfig | None = None
    expected_current_config_digest: str | None = None


@dataclass(frozen=True, slots=True)
class MigrationOwnerResult:
    value: Agent | HomeReceipt | bool | dict[str, object] | None = None
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
    data_json: str | None = None

    @classmethod
    def from_exception(cls, error: BaseException) -> MigrationFailure:
        data = getattr(error, "data", None)
        data_json = (
            json.dumps(data, sort_keys=True, allow_nan=False)
            if isinstance(data, dict)
            else None
        )
        return cls(
            type(error).__name__, str(error)[:1000],
            getattr(error, "code", None),
            getattr(error, "category", None),
            getattr(error, "phase", None),
            getattr(error, "actor", None),
            data_json,
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
            if self.data_json is not None:
                value = json.loads(self.data_json)
                if isinstance(value, dict):
                    error.data = value
            return error
        return RuntimeError(self.message)


class MigrationOwnerPort(Protocol):
    def migration_owner_call(self, command: MigrationOwnerCall) -> MigrationOwnerResult: ...


def config_rebind_quiescence(
    agent: Agent,
    *,
    desired_state: object | None,
    liveness: Any,
    runtime_launches: Any,
) -> dict[str, object]:
    """Read all identity-owned desired/runtime evidence and fail closed."""

    state = desired_state.load() if desired_state is not None else None
    desired_rows: list[dict[str, object]] = [
        {"kind": "headless", "harness": spec.harness, "name": spec.name}
        for spec in getattr(state, "harnesses", ())
        if spec.harness in DECLARED_HARNESSES and spec.name in {agent.actor, agent.uri}
    ]
    desired_rows.extend(
        {"kind": "interactive", "actor": session.actor}
        for session in getattr(state, "interactive_sessions", ())
        if session.actor in {agent.actor, agent.uri}
    )
    desired_rows.extend(
        {"kind": "pending-session-effect", "actor": effect.actor}
        for effect in getattr(state, "pending_session_agent_effects", ())
        if effect.actor in {agent.actor, agent.uri}
    )
    supervisor_running = liveness.verdict(agent.uri, state)
    binding = liveness.binding(agent.uri)
    active_launch = runtime_launches.active_for(agent.actor)
    destroy_reserved = runtime_launches.destroy_reserved(agent.actor)
    desired_unknown = desired_state is None
    desired_present = bool(desired_rows)
    runtime_present = bool(
        supervisor_running is True
        or active_launch
        or destroy_reserved
        or binding is not None
    )
    return {
        "quiescent": not (desired_unknown or desired_present or runtime_present),
        "desired": desired_present,
        "runtime": runtime_present,
        "desiredStateReadable": not desired_unknown,
        "desiredRows": desired_rows,
        "supervisorRunning": supervisor_running,
        "activeRuntimeLaunch": active_launch,
        "destroyReserved": destroy_reserved,
        "binding": None if binding is None else binding.to_json(),
        "proofSource": "identity-owner/durable-desired-state/supervisor",
    }


def bind_config_quiescent(
    agent: Agent,
    *,
    desired_state: object | None,
    liveness: Any,
    runtime_launches: Any,
    registry: Any,
    config: AgentConfig,
    expected_current_config_digest: str,
) -> tuple[Agent, dict[str, object]]:
    """Prove quiescence and commit the binding CAS as one owner step.

    The desired-state authority is fenced across both the proof and the
    mutation, so a writer either lands before the proof (which then fails) or
    after the CAS (which is the operator's later intent, not a hijacked one).
    """

    fence = getattr(desired_state, "config_rebind_fence", None)
    if desired_state is not None and not callable(fence):
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_NOT_QUIESCENT,
            "the durable desired-state authority cannot be fenced for the "
            "binding mutation",
            data={
                "proofSource": "identity-owner/durable-desired-state",
                "fenceAvailable": False,
            },
        )
    with fence() if callable(fence) else nullcontext(None) as fenced:
        # Inside the fence, read the durable authority rather than a
        # projection that a blocked writer may not have published yet.
        source = desired_state if fenced is None else fenced
        proof = config_rebind_quiescence(
            agent,
            desired_state=source,
            liveness=liveness,
            runtime_launches=runtime_launches,
        )
        if proof.get("quiescent") is not True:
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_NOT_QUIESCENT,
                "agent configuration can be rebound only when durable desired "
                "state, supervisor, and active runs are quiescent",
                data=proof,
            )
        updated = registry.bind_agent_config(
            agent.actor,
            expected_entity_token=agent.entity_token,
            config=config,
            expected_current_config_digest=expected_current_config_digest,
        )
    return updated, proof


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
        kind: Literal[
            "snapshot", "home-receipt", "stopped", "update-config",
            "bind-config-quiescent", "quiescence",
        ],
        config: AgentConfig | None = None,
        expected_current_config_digest: str | None = None,
    ) -> Agent | HomeReceipt | bool | dict[str, object] | None:
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
                expected_current_config_digest,
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

    def bind_config_quiescent(
        self, actor: str, *, config: AgentConfig,
        expected_current_config_digest: str,
    ) -> tuple[Agent, dict[str, object]]:
        if actor not in {self._agent.actor, self._agent.uri}:
            raise ValueError("configuration rebind identity changed")
        value = self._call(
            "bind-config-quiescent", config, expected_current_config_digest
        )
        if not isinstance(value, dict):
            raise RuntimeError(
                "migration owner returned invalid quiescent binding facts"
            )
        bound = value.get("agent")
        proof = value.get("proof")
        if not isinstance(bound, Agent) or not isinstance(proof, dict):
            raise RuntimeError(
                "migration owner returned invalid quiescent binding facts"
            )
        self._agent = bound
        return bound, proof

    def quiescence(self, actor: str) -> dict[str, object]:
        if actor not in {self._agent.actor, self._agent.uri}:
            raise ValueError("configuration rebind identity changed")
        value = self._call("quiescence")
        if not isinstance(value, dict):
            raise RuntimeError("migration owner returned invalid quiescence proof")
        return value

    def stopped(self, actor: str, entity_token: str) -> bool | None:
        if actor != self._agent.uri or entity_token != self._agent.entity_token:
            return None
        value = self._call("stopped")
        return value if isinstance(value, bool) or value is None else None


def run_migration_effect(effect: MigrationEffect, owner: MigrationOwnerPort) -> MigrationOutcome:
    """Run native/FS work off mailbox with only typed reads/CAS from its owner."""

    proxy = _RegistryProxy(effect, owner)
    request = effect.request
    try:
        agent = proxy.require(request.actor)
        if request.method == "agent.config.rebind":
            return MigrationOutcome.success(
                run_config_rebind(
                    proxy,
                    agent.actor,
                    source=request.source,
                    rollback_receipt_id=request.rollback_receipt_id,
                    dry_run=request.dry_run,
                )
            )
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
                runtime_preparers=dict(effect.runtime_preparers),
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
            runtime_preparers=dict(effect.runtime_preparers),
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
