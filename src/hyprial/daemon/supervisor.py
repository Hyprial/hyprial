"""Compatibility facade for the actor-owned harness lifecycle domain.

All mutable lifecycle state lives in :mod:`hyprial.daemon.harness_actor`.  This
module deliberately retains the established ``ManagedHarnessRuntime`` public API
while big-bang wiring migrates callers to typed command/projection ports.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from .api import HarnessDelivery, HarnessLauncher, HarnessResult
from .desired_state import DesiredStateStore, HarnessLaunchSpec
from .harness_ports import HarnessAdapterRegistrationProjection
from .harness_actor import (
    HarnessRestoreSummary,
    HarnessRuntimeActor,
    HarnessRuntimeFacade,
)
from hyprial.contracts.readiness import ReadinessReport

_LOGGER = logging.getLogger("hyprial.daemon.supervisor")


def _start_timeout_seconds() -> float:
    raw = os.environ.get("HYPRIAL_HARNESS_START_TIMEOUT")
    if raw is None:
        return 60.0
    try:
        value = float(raw)
    except ValueError:
        return 60.0
    return value if value >= 0 else 60.0


def _start_backoff_max_seconds() -> float:
    raw = os.environ.get("HYPRIAL_HARNESS_START_BACKOFF_MAX")
    if raw is None:
        return 60.0
    try:
        value = float(raw)
    except ValueError:
        return 60.0
    return value if value > 0 else 60.0


_cooldown_deprecation_warned = False


def _warn_cooldown_env_deprecated() -> None:
    """One-time notice that the cooldown knobs no longer do anything.

    Failed is terminal since the retrying/failed split: there is no cooldown
    and no self-heal probe, so HYPRIAL_HARNESS_QUARANTINE_COOLDOWN{,_MAX} are
    inert.  Reading them still, only to say so, beats silently ignoring an
    operator's explicit configuration.
    """

    global _cooldown_deprecation_warned
    if _cooldown_deprecation_warned:
        return
    set_vars = [
        name
        for name in (
            "HYPRIAL_HARNESS_QUARANTINE_COOLDOWN",
            "HYPRIAL_HARNESS_QUARANTINE_COOLDOWN_MAX",
        )
        if os.environ.get(name) is not None
    ]
    if not set_vars:
        return
    _cooldown_deprecation_warned = True
    _LOGGER.warning(
        "%s no longer have any effect: harness quarantine self-heal was "
        "removed; a harness whose failure budget is spent stays failed "
        "until an explicit start",
        "/".join(set_vars),
    )


def _failure_budget() -> int:
    """Consecutive start failures before a harness is failed for good.

    ``HYPRIAL_HARNESS_QUARANTINE_THRESHOLD`` is the deprecated pre-split name for
    the same budget and is still honored; 0 disables termination (retries
    continue forever under backoff).
    """

    raw = os.environ.get("HYPRIAL_HARNESS_FAILURE_BUDGET")
    if raw is None:
        raw = os.environ.get("HYPRIAL_HARNESS_QUARANTINE_THRESHOLD")
    if raw is None:
        return 3
    try:
        value = int(raw)
    except ValueError:
        return 3
    return value if value >= 0 else 3


class ManagedHarnessRuntime:
    """Stateless public facade delegating to one ``HarnessRuntimeActor``."""

    def __init__(
        self,
        launcher: HarnessLauncher,
        *,
        clock: Callable[[], float] | None = None,
        restart_backoff_seconds: float = 0.0,
        start_timeout_seconds: float | None = None,
        start_backoff_max_seconds: float | None = None,
        failure_budget: int | None = None,
        start_max_workers: int = 4,
        actor_mailbox_capacity: int = 128,
        process_identity_reader: Callable[[int], str | None] | None = None,
        orphan_state_path: Path | None = None,
        orphan_logger: Callable[..., None] | None = None,
        desired_state: DesiredStateStore | None = None,
    ) -> None:
        if restart_backoff_seconds < 0:
            raise ValueError("restart backoff must not be negative")
        timeout = (
            _start_timeout_seconds()
            if start_timeout_seconds is None
            else start_timeout_seconds
        )
        if timeout < 0:
            raise ValueError("start timeout must not be negative")
        max_backoff = (
            _start_backoff_max_seconds()
            if start_backoff_max_seconds is None
            else start_backoff_max_seconds
        )
        budget = (
            _failure_budget()
            if failure_budget is None
            else failure_budget
        )
        _warn_cooldown_env_deprecated()
        actor_kwargs: dict[str, object] = {}
        if process_identity_reader is not None:
            actor_kwargs["identity_reader"] = process_identity_reader
        # Read-only compatibility seam used by transfer fixtures while the
        # composition root moves to typed lifecycle ports.
        self._launcher = launcher
        self._actor = HarnessRuntimeActor(
            launcher,
            clock=clock or time.monotonic,
            restart_backoff_seconds=restart_backoff_seconds,
            start_timeout_seconds=timeout,
            start_backoff_max_seconds=max_backoff,
            failure_budget=budget,
            start_max_workers=start_max_workers,
            mailbox_capacity=actor_mailbox_capacity,
            orphan_state_path=orphan_state_path,
            orphan_logger=orphan_logger,
            desired_state=desired_state,
            **actor_kwargs,
        )
        self._facade = HarnessRuntimeFacade(
            self._actor,
            reply_timeout=max(65.0, timeout + 1.0),
        )

    @staticmethod
    def _key(spec: HarnessLaunchSpec) -> str:
        return f"{spec.harness}:{spec.name}"

    @property
    def last_errors(self) -> dict[str, str]:
        return self._facade.projection.last_errors()

    @property
    def _failed(self) -> frozenset[str]:
        """Read-only compatibility view for pre-big-bang diagnostics/tests."""

        return self._facade.projection.failed()

    def restore(
        self, desired: tuple[HarnessLaunchSpec, ...]
    ) -> HarnessRestoreSummary:
        return self._facade.restore(desired)

    def reconcile(self) -> int:
        return self._facade.reconcile()

    def start(self, spec: HarnessLaunchSpec) -> bool:
        return self._facade.start(spec)

    def remove(self, harness: str, name: str) -> bool:
        return self._facade.remove(harness, name)

    def drain_failed_events(self) -> tuple[str, ...]:
        return self._facade.drain_failed_events()

    def drain_readiness_reports(self) -> tuple[ReadinessReport, ...]:
        return self._facade.drain_readiness_reports()

    def status(self) -> tuple[dict[str, object], ...]:
        return self._facade.status()

    def collect_orphans(self) -> int:
        return self._actor.collect_orphans()

    def orphan_status(self) -> tuple[dict[str, object], ...]:
        return self._actor.orphan_status()

    def streaming_actors(self) -> tuple[str, ...]:
        return self._facade.streaming_actors()

    def dispatch(self, name: str, delivery: HarnessDelivery) -> bool:
        return self._facade.dispatch(name, delivery)

    def wait_ready(self, harness: str, name: str, timeout: float) -> bool:
        return self._facade.wait_ready(harness, name, timeout)

    def session_refs(self) -> dict[tuple[str, str], str]:
        return self._facade.session_refs()

    def projected_session_refs(self) -> dict[tuple[str, str], str]:
        return self._facade.projection.session_refs()

    def reconcile_session_refs(self) -> dict[tuple[str, str], str]:
        return self._facade.reconcile_session_refs()

    def stage_harness_desired(self, spec: HarnessLaunchSpec) -> bool:
        return self._facade.stage_harness_desired(spec)

    def snapshot_adapter_registration(
        self, name: str
    ) -> HarnessAdapterRegistrationProjection:
        return self._facade.snapshot_adapter_registration(name)

    def remove_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool:
        return self._facade.remove_adapter_registration(snapshot)

    def restore_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool:
        return self._facade.restore_adapter_registration(snapshot)

    def drain_results(self) -> tuple[HarnessResult, ...]:
        return self._facade.drain_results()

    def drain_progress(self) -> tuple[object, ...]:
        return self._facade.drain_progress()

    def bind_liveness(self, harness: str, name: str, binding: object) -> bool:
        return self._facade.bind_liveness(harness, name, binding)

    @property
    def drain_complete(self) -> bool:
        return self._facade._last_drain_complete

    def stop(self, timeout: float = 5.0) -> None:
        self._facade.stop(timeout)


__all__ = ["HarnessRestoreSummary", "ManagedHarnessRuntime"]
