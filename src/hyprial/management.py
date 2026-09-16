"""Typed management domain shared by live IPC and fenced offline bootstrap."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from hyprial.daemon.desired_state import DesiredStateStore, HarnessLaunchSpec
from hyprial.daemon.harness_ports import HarnessAdapterRegistrationProjection
from hyprial.daemon.ownership import DaemonStateOwnershipFence


class ManagementError(RuntimeError):
    code = "MANAGEMENT_ERROR"


@dataclass(frozen=True, slots=True)
class EnsureSquireRegistryCommand:
    owner: str
    machine: str
    spec: HarnessLaunchSpec
    cwd: str
    model_provider: str
    model: str
    preferred_harness: str
    adapter: str | None = None
    start: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "machine": self.machine,
            "spec": self.spec.to_json(),
            "cwd": self.cwd,
            "provider": self.model_provider,
            "model": self.model,
            "preferredHarness": self.preferred_harness,
            "adapter": self.adapter,
            "start": self.start,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> EnsureSquireRegistryCommand:
        def required(label: str) -> str:
            value = payload.get(label)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be a non-empty string")
            return value

        raw_spec = payload.get("spec")
        if not isinstance(raw_spec, dict):
            raise ValueError("spec must be an object")
        adapter = payload.get("adapter")
        if adapter is not None and (not isinstance(adapter, str) or not adapter):
            raise ValueError("adapter must be a non-empty string or null")
        return cls(
            required("owner"),
            required("machine"),
            HarnessLaunchSpec.from_json(raw_spec, "management.squire.spec"),
            required("cwd"),
            required("provider"),
            required("model"),
            required("preferredHarness"),
            adapter,
            payload.get("start") is True,
        )


@dataclass(frozen=True, slots=True)
class SquireRegistryResult:
    changed: tuple[str, ...]
    agent_uri: str
    worker: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        return {
            "changed": list(self.changed),
            "agentUri": self.agent_uri,
            "worker": self.worker,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> SquireRegistryResult:
        changed = payload.get("changed")
        agent_uri = payload.get("agentUri")
        worker = payload.get("worker")
        if not isinstance(changed, list) or not all(
            isinstance(item, str) for item in changed
        ):
            raise ValueError("management changed must be a string array")
        if not isinstance(agent_uri, str) or not agent_uri:
            raise ValueError("management agentUri must be a non-empty string")
        if not isinstance(worker, dict):
            raise ValueError("management worker must be an object")
        return cls(tuple(changed), agent_uri, dict(worker))


class HarnessManagementPort(Protocol):
    def stage_harness_desired(self, spec: HarnessLaunchSpec) -> bool: ...

    def snapshot_adapter_registration(
        self, name: str
    ) -> HarnessAdapterRegistrationProjection: ...

    def remove_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool: ...

    def restore_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool: ...


class AgentManagementPort(Protocol):
    owner: str
    machine: str

    def get(self, actor: str) -> Any | None: ...

    def create(self, actor: str, **configuration: object) -> Any: ...

    def pins(self) -> dict[str, str]: ...

    def pin(self, adapter: str, actor: str) -> str | None: ...

    def restore_pin_if_absent(self, adapter: str, actor: str) -> str | None: ...

    def unpin_if(self, adapter: str, expected_actor: str | None) -> str | None: ...


@dataclass(frozen=True, slots=True)
class AdapterRegistrySnapshot:
    harness: HarnessAdapterRegistrationProjection
    agent_pin: str | None


class AdapterRemovalTransaction:
    def __init__(
        self,
        harnesses: HarnessManagementPort,
        agents: AgentManagementPort,
        snapshot: AdapterRegistrySnapshot,
    ) -> None:
        self._harnesses = harnesses
        self._agents = agents
        self.snapshot = snapshot
        self.committed = False

    def commit(self) -> None:
        if self.committed:
            return
        try:
            self._harnesses.remove_adapter_registration(self.snapshot.harness)
        except BaseException as operation_error:
            # The actor may have durably applied the mutation before its reply
            # was lost or the facade raised.  Restoring is compare-fenced and
            # idempotent when the old registration is still present, so always
            # compensate rather than guessing whether the call took effect.
            try:
                self._harnesses.restore_adapter_registration(self.snapshot.harness)
            except BaseException as rollback_error:
                raise BaseExceptionGroup(
                    "adapter harness removal and rollback failed",
                    [operation_error, rollback_error],
                ) from operation_error
            raise
        try:
            if self.snapshot.agent_pin is not None:
                self._agents.unpin_if(
                    self.snapshot.harness.name, self.snapshot.agent_pin
                )
        except BaseException as operation_error:
            rollback_errors: list[BaseException] = []
            if self.snapshot.agent_pin is not None:
                try:
                    self._agents.restore_pin_if_absent(
                        self.snapshot.harness.name, self.snapshot.agent_pin
                    )
                except BaseException as rollback_error:
                    rollback_errors.append(rollback_error)
            try:
                self._harnesses.restore_adapter_registration(self.snapshot.harness)
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
            if rollback_errors:
                raise BaseExceptionGroup(
                    "adapter registry commit and rollback failed",
                    [operation_error, *rollback_errors],
                ) from operation_error
            raise
        self.committed = True

    def rollback(self) -> None:
        if not self.committed:
            return
        rollback_errors: list[BaseException] = []
        if self.snapshot.agent_pin is not None:
            try:
                self._agents.restore_pin_if_absent(
                    self.snapshot.harness.name, self.snapshot.agent_pin
                )
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        try:
            self._harnesses.restore_adapter_registration(self.snapshot.harness)
        except BaseException as rollback_error:
            rollback_errors.append(rollback_error)
        if rollback_errors:
            raise BaseExceptionGroup(
                "adapter registry rollback failed", rollback_errors
            )
        self.committed = False


class RegistryManagementHandler:
    """Shared orchestration; concrete mutations stay behind actor ports."""

    def __init__(
        self,
        harnesses: HarnessManagementPort,
        agents: AgentManagementPort,
        *,
        start_harness: Callable[[HarnessLaunchSpec], dict[str, object]] | None = None,
    ) -> None:
        self._harnesses = harnesses
        self._agents = agents
        self._start_harness = start_harness
        self._lock = threading.RLock()

    def ensure_squire(
        self, command: EnsureSquireRegistryCommand
    ) -> SquireRegistryResult:
        with self._lock:
            if (command.owner, command.machine) != (
                self._agents.owner,
                self._agents.machine,
            ):
                # Both segments are compared, and the message names BOTH sides.
                #
                # ⚠️ This matters more after owner became a user identity.  It
                # used to come from the host login, so a mismatch here was
                # almost always the owner — two machines disagreeing simply by
                # being two machines.  Now owner is the same value across every
                # machine a user owns, so a mismatch is almost always the
                # <machine> segment instead.  An error naming neither side
                # leaves the reader unable to tell which one moved, and the
                # likely answer changed underneath them.
                raise ManagementError(
                    "Squire identity does not match this management authority: "
                    f"requested owner={command.owner!r} machine={command.machine!r}, "
                    f"authority owner={self._agents.owner!r} "
                    f"machine={self._agents.machine!r}"
                )
            changed: list[str] = []
            if not command.start and self._harnesses.stage_harness_desired(
                command.spec
            ):
                changed.append("desiredState.providers")
            agent = self._agents.get("squire")
            if agent is None:
                agent = self._agents.create(
                    "squire",
                    cwd=command.cwd,
                    provider=command.model_provider,
                    model=command.model,
                    preferred_harness=command.preferred_harness,
                )
                changed.append("agent")
            worker: dict[str, object] = {"started": False}
            if command.start:
                if self._start_harness is None:
                    raise ManagementError(
                        "--start requires a running daemon management authority"
                    )
                launch = self._start_harness(command.spec)
                worker = {"started": True, "result": launch}
                if launch.get("changed") is True:
                    changed.append("desiredState.providers")
            agent_uri = str(getattr(agent, "uri"))
            if command.adapter is not None:
                current = self._agents.pins().get(command.adapter)
                if current != agent_uri:
                    self._agents.pin(command.adapter, "squire")
                    changed.append("adapter.pin")
            return SquireRegistryResult(tuple(changed), agent_uri, worker)

    @contextmanager
    def adapter_removal(self, name: str) -> Iterator[AdapterRemovalTransaction]:
        with self._lock:
            snapshot = AdapterRegistrySnapshot(
                self._harnesses.snapshot_adapter_registration(name),
                self._agents.pins().get(name),
            )
            transaction = AdapterRemovalTransaction(
                self._harnesses, self._agents, snapshot
            )
            try:
                yield transaction
            except BaseException as operation_error:
                try:
                    transaction.rollback()
                except BaseException as rollback_error:
                    raise BaseExceptionGroup(
                        "adapter registry operation and rollback failed",
                        [operation_error, rollback_error],
                    ) from operation_error
                raise


class _NeverLaunch:
    def start(self, _spec: HarnessLaunchSpec) -> object:
        raise AssertionError("offline management must never launch a harness")


class OfflineManagementLease:
    """Fail-fast offline actor authority held under the daemon ownership flock."""

    def __init__(self, state_dir: Path, *, owner: str, machine: str) -> None:
        self.state_dir = Path(state_dir)
        self.owner = owner
        self.machine = machine
        self._fence: DaemonStateOwnershipFence | None = None
        self._domains: Any | None = None
        self._harnesses: Any | None = None
        self.handler: RegistryManagementHandler | None = None

    def __enter__(self) -> RegistryManagementHandler:
        from hyprial.daemon.composition import AgentSessionDomains
        from hyprial.daemon.supervisor import ManagedHarnessRuntime

        self._fence = DaemonStateOwnershipFence.acquire(self.state_dir)
        desired = DesiredStateStore(self.state_dir / "desired-state.json")
        try:
            self._domains = AgentSessionDomains(
                database=self.state_dir / "agents.sqlite3",
                desired_state=desired,
                owner=self.owner,
                node_id=self.machine,
                daemon_epoch=f"offline-management:{uuid4().hex}",
                worker_running=lambda _actor: None,
                clock=time.monotonic,
            )
            self._harnesses = ManagedHarnessRuntime(
                _NeverLaunch(), desired_state=desired
            )
            self.handler = RegistryManagementHandler(
                self._harnesses, self._domains.agents
            )
            return self.handler
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_error: object) -> None:
        self.handler = None
        if self._harnesses is not None:
            self._harnesses.stop()
            self._harnesses = None
        if self._domains is not None:
            self._domains.close()
            self._domains = None
        if self._fence is not None:
            self._fence.close()
            self._fence = None
