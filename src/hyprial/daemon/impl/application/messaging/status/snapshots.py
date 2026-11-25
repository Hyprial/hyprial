"""The per-request worker-status snapshot and local presence projection."""

from __future__ import annotations

from __future__ import annotations
import contextlib
import time
from hyprial.daemon.impl.adapters.spec import is_adapter_provider
from collections import defaultdict
from collections.abc import Iterator
from hyprial.daemon.impl.harnesses.capabilities import worker_spec
from hyprial.daemon.impl.lifecycle.primitives import session_ref_for
from typing import TYPE_CHECKING
from hyprial.daemon.impl.desired_state  import (
    DesiredState,
)
from hyprial.kernel import HarnessLaunchSpec, ipc_errors
if TYPE_CHECKING:
    pass



class _WorkerStatusSnapshot:
    """One request's shared view of managed-worker state (ps / agent.list / agent.get).

    The ps storm this replaces: every agent verdict paid one supervisor
    ``status()`` round trip of its own, and ``_canonical_harness_uri``'s
    fallback paid one desired-state load per connector per verdict — N agents
    × M connectors of full loads for a single ps.  Building the two lookup
    tables once per request collapses both to constants, and every row in the
    response then reads the same tables, so one ps is internally consistent
    by construction.  That row consistency is an intentional semantics
    choice, not a side effect of the fix.
    """

    def __init__(
        self,
        *,
        statuses: tuple[dict[str, object], ...],
        running_by_actor: dict[str, bool],
        session_ref_by_actor: dict[str, str | None],
        desired: "DesiredState | None" = None,
        run_states: dict | None = None,
        start_failures: dict | None = None,
    ) -> None:
        self.statuses = statuses
        self.running_by_actor = running_by_actor
        self.session_ref_by_actor = session_ref_by_actor
        # The one desired-state load this request paid (card 259 P1 meets
        # the request snapshot): _actor_status_snapshot reuses it instead of
        # loading a second time.  None when the store had nothing to give.
        self.desired = desired
        self.run_states = run_states or {}
        self.start_failures = start_failures or {}


class _WorkerSnapshotMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    @contextlib.contextmanager
    def _worker_status_snapshot(self, *, sessions=None) -> Iterator[_WorkerStatusSnapshot]:
        """Install one shared worker-status snapshot for the calling request.

        The liveness ``worker_running`` callback is injected once at
        construction time, so a per-request snapshot cannot be threaded
        through it as an argument — it is swapped behind this handle instead.
        The handle is a thread-local because IPC clients each serve on their
        own thread: one request's tables never leak into a concurrent
        request's verdicts.
        """

        snapshot = self._build_worker_status_snapshot(sessions=sessions)
        previous = self._current_worker_snapshot()
        self._worker_snapshot_local.current = snapshot
        try:
            yield snapshot
        finally:
            self._worker_snapshot_local.current = previous

    def _cell_run_state_rows(self, *, sessions=None):
        domains = getattr(self, "_agent_session_domains", None)
        if domains is None:
            return {}
        actor_registry = getattr(self, "_cell_actor_registry", None)
        return domains.cells.run_states(self._cell_interactive_facts(sessions=sessions), {},
                                        now_ms=time.time_ns() // 1_000_000,
                                        monotonic_ms=time.monotonic_ns() // 1_000_000,
                                        cell_actors=actor_registry)

    def _cell_run_state_field(self, actor):
        snapshot = self._current_worker_snapshot()
        rows = snapshot.run_states if snapshot is not None else self._cell_run_state_rows()
        return {"runState": rows[actor]} if actor in rows else {}

    def _startup_failure_field(self, agent):
        snapshot = self._current_worker_snapshot()
        if snapshot is None:
            return {}
        row = getattr(snapshot, "start_failures", {}).get(agent.uri)
        if row is None:
            return {}
        operation, error, failed_at = row
        target = operation.target
        if (operation.observed_entity_token != agent.entity_token
                or target.harness.model != agent.model
                or target.harness.model_provider != agent.provider
                or target.harness.cwd != agent.cwd):
            return {}
        if target.harness.harness != (agent.preferred_harness or agent.last_harness):
            return {}
        message = "Harness did not become ready; check its configuration."
        if "configured Pi model is unavailable" in error:
            message = "configured Pi model is unavailable; check the selected model configuration"
        return {"startupFailure": {"operationId": operation.operation_id,
                "code": ipc_errors.HARNESS_START_FAILED, "message": message, "failedAtMs": failed_at}}

    def _build_worker_status_snapshot(self, *, sessions=None) -> _WorkerStatusSnapshot:
        """One supervisor round trip + one desired-state load, as two tables.

        Kind lookup uses the same unambiguous worker resolver as individual
        reads. ``running_by_actor`` scans statuses in order and claims both
        the canonical URI and the bare name;
        ``session_ref_by_actor`` scans harness specs in order and records each
        member's ref, so a later same-name member is not hidden by setdefault.
        """

        report = getattr(self._harnesses, "status", None)
        statuses = report() if callable(report) else ()
        if not isinstance(statuses, tuple | list):
            statuses = ()
        try:
            desired_loaded = self.desired_state.load()
            specs = desired_loaded.harnesses
        except Exception:  # noqa: BLE001 - mirrors _canonical_harness_uri
            desired_loaded = None
            specs = ()
        # Group once to keep snapshot construction linear in fleet size.
        # Adapter metadata must not supply an agent's pinned owner.
        grouped: dict[str, list[HarnessLaunchSpec]] = defaultdict(list)
        for spec in specs:
            grouped[spec.name].append(spec)
        specs_by_name = {
            name: worker_spec(candidates, name) for name, candidates in grouped.items()
        }
        specs_by_status_id = {
            f"{spec.harness}:{spec.member or spec.name}": spec for spec in specs
        }
        running_by_actor: dict[str, bool] = {}
        for status in statuses:
            if not isinstance(status, dict):
                continue
            name = status.get("name")
            if not isinstance(name, str) or not name:
                continue
            if status.get("adapter") is not None or is_adapter_provider(status.get("runtime")):
                continue
            running = bool(status.get("running"))
            status_spec = specs_by_status_id.get(str(status.get("id")))
            running_by_actor.setdefault(
                self._canonical_harness_uri(
                    name, status_spec or specs_by_name.get(name)
                ),
                running,
            )
            running_by_actor.setdefault(name, running)
        read_refs = getattr(self._harnesses, "projected_worker_session_refs", None)
        refs = read_refs() if callable(read_refs) else {}
        session_ref_by_actor: dict[str, str | None] = {}
        for spec in specs:
            session_ref_by_actor[
                self._canonical_harness_uri(spec.name, spec)
            ] = session_ref_for(
                refs,
                harness=spec.harness,
                name=spec.name,
                member=spec.member,
            )
        read_failures = getattr(getattr(self, "_lifecycle_manager", None), "read_start_failures", None)
        return _WorkerStatusSnapshot(
            start_failures=read_failures() if callable(read_failures) else {},
            statuses=tuple(statuses),
            running_by_actor=running_by_actor,
            session_ref_by_actor=session_ref_by_actor,
            desired=desired_loaded,
            run_states=_WorkerSnapshotMixin._cell_run_state_rows(self, sessions=sessions),
        )
