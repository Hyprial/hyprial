"""Typed management domain shared by live IPC and fenced offline bootstrap.

Decision notes (phase B, S10; Allen 2026-11-03 "squire is an ordinary agent"):

- 2026-11-20 (S2b, remove-squire-setup-surface): management.squire.ensure,
  its command/result types and the template/start/save ports only it used
  are deleted.  What remains is the adapter-removal transaction and the
  fenced offline lease, both generic adapter/registry operations.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from hyprial.daemon.impl.desired_state import DesiredStateStore

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessAdapterRegistrationProjection
from hyprial.daemon.impl.configuration.ownership import DaemonStateOwnershipFence


class ManagementError(RuntimeError):
    code = "MANAGEMENT_ERROR"


class HarnessManagementPort(Protocol):
    def snapshot_adapter_registration(
        self, name: str
    ) -> HarnessAdapterRegistrationProjection: ...

    def remove_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool: ...

    def restore_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AdapterRegistrySnapshot:
    harness: HarnessAdapterRegistrationProjection
    agent_pin: str | None


class AdapterRemovalTransaction:
    def __init__(
        self,
        harnesses: HarnessManagementPort,
        snapshot: AdapterRegistrySnapshot,
        *,
        recovery: bool = False,
    ) -> None:
        self._harnesses = harnesses
        self.snapshot = snapshot
        self.committed = False
        self.unsettled = recovery
        self._recovery = recovery

    def commit(self) -> None:
        if self._recovery:
            raise ManagementError("adapter recovery is compensation-only")
        if self.unsettled:
            raise ManagementError("adapter registry compensation remains unsettled")
        if self.committed:
            return
        self.unsettled = True
        try:
            self._harnesses.remove_adapter_registration(self.snapshot.harness)
        except BaseException as operation_error:
            try:
                self.rollback()
            except BaseException as rollback_error:
                raise BaseExceptionGroup(
                    "adapter registry commit and rollback failed",
                    [operation_error, rollback_error],
                ) from operation_error
            raise
        self.committed = True
        self.unsettled = False

    def rollback(self) -> None:
        if not self.committed and not self.unsettled:
            return
        self.unsettled = True
        try:
            self._harnesses.restore_adapter_registration(self.snapshot.harness)
        except BaseException as rollback_error:
            raise BaseExceptionGroup(
                "adapter registry rollback failed", [rollback_error]
            ) from rollback_error
        self.committed = False
        self.unsettled = False


class RegistryManagementHandler:
    """Shared orchestration; concrete mutations stay behind actor ports."""

    def __init__(
        self,
        harnesses: HarnessManagementPort,
    ) -> None:
        self._harnesses = harnesses
        self._lock = threading.RLock()

    @contextmanager
    def adapter_removal(self, name: str) -> Iterator[AdapterRemovalTransaction]:
        with self._lock:
            snapshot = AdapterRegistrySnapshot(
                self._harnesses.snapshot_adapter_registration(name),
                None,
            )
            transaction = AdapterRemovalTransaction(self._harnesses, snapshot)
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

    def __init__(
        self,
        state_dir: Path,
        *,
        owner: str,
        machine: str,
        hyprial_home: Path,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.hyprial_home = Path(hyprial_home)
        self.owner = owner
        self.machine = machine
        self._fence: DaemonStateOwnershipFence | None = None
        # P7: one registry for this offline process, shared by the agent
        # domains and the (never-launching) harness runtime.
        self._actor_hosts: Any | None = None
        self._domains: Any | None = None
        self._harnesses: Any | None = None
        self.handler: RegistryManagementHandler | None = None

    def __enter__(self) -> RegistryManagementHandler:
        from hyprial.daemon.impl.composition import AgentSessionDomains
        from hyprial.daemon.impl.composition.actors import ActorHostRegistry
        from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime

        self._fence = DaemonStateOwnershipFence.acquire(self.state_dir)
        desired = DesiredStateStore(self.state_dir / "desired-state.json")
        self._actor_hosts = ActorHostRegistry()
        try:
            self._domains = AgentSessionDomains(
                database=self.state_dir / "agents.sqlite3",
                desired_state=desired,
                owner=self.owner,
                node_id=self.machine,
                daemon_epoch=f"offline-management:{uuid4().hex}",
                hyprial_home=self.hyprial_home,
                worker_running=lambda _actor: None,
                clock=time.monotonic,
                actor_hosts=self._actor_hosts,
            )
            self._harnesses = ManagedHarnessRuntime(
                _NeverLaunch(),
                desired_state=desired,
                actor_host=self._actor_hosts.host_for("offline-management-harnesses"),
            )
            self.handler = RegistryManagementHandler(self._harnesses)
            return self.handler
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_error: object) -> None:
        self.handler = None
        if self._harnesses is not None:
            self._harnesses.stop()
            self._harnesses = None
        domains_closed = True
        if self._domains is not None:
            domains_closed = self._domains.close(shutdown_hosts=False)
            self._domains = None
        if self._actor_hosts is not None:
            actor_hosts = self._actor_hosts
            self._actor_hosts = None
            if (
                domains_closed
                and not actor_hosts.shutdown(5.0)
                and not any(error is not None for error in _error)
            ):
                raise RuntimeError(
                    "offline management actor host did not shut down"
                )
        if self._fence is not None:
            self._fence.close()
            self._fence = None
