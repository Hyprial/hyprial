"""The per-request worker-status snapshot and local presence projection."""

from __future__ import annotations

from __future__ import annotations
import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING
from hyprial.daemon.impl.desired_state  import (
    DesiredState,
)
from hyprial.kernel import HarnessLaunchSpec
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
    ) -> None:
        self.statuses = statuses
        self.running_by_actor = running_by_actor
        self.session_ref_by_actor = session_ref_by_actor
        # The one desired-state load this request paid (card 259 P1 meets
        # the request snapshot): _actor_status_snapshot reuses it instead of
        # loading a second time.  None when the store had nothing to give.
        self.desired = desired


class _WorkerSnapshotMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    @contextlib.contextmanager
    def _worker_status_snapshot(self) -> Iterator[_WorkerStatusSnapshot]:
        """Install one shared worker-status snapshot for the calling request.

        The liveness ``worker_running`` callback is injected once at
        construction time, so a per-request snapshot cannot be threaded
        through it as an argument — it is swapped behind this handle instead.
        The handle is a thread-local because IPC clients each serve on their
        own thread: one request's tables never leak into a concurrent
        request's verdicts.
        """

        snapshot = self._build_worker_status_snapshot()
        previous = self._current_worker_snapshot()
        self._worker_snapshot_local.current = snapshot
        try:
            yield snapshot
        finally:
            self._worker_snapshot_local.current = previous

    def _build_worker_status_snapshot(self) -> _WorkerStatusSnapshot:
        """One supervisor round trip + one desired-state load, as two tables.

        Both lookups reproduce their per-call predecessors exactly, including
        first-match-wins ordering: ``running_by_actor`` scans statuses in
        order and claims both the canonical URI and the bare name;
        ``session_ref_by_actor`` scans harness specs in order and keeps the
        first spec's ref even when that ref is absent (None).
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
        # First-by-name, the same lookup _canonical_harness_uri's fallback
        # performs when no spec is passed.
        specs_by_name: dict[str, HarnessLaunchSpec] = {}
        for spec in specs:
            specs_by_name.setdefault(spec.name, spec)
        running_by_actor: dict[str, bool] = {}
        for status in statuses:
            if not isinstance(status, dict):
                continue
            name = status.get("name")
            if not isinstance(name, str) or not name:
                continue
            if status.get("runtime") == "lark":
                continue
            running = bool(status.get("running"))
            running_by_actor.setdefault(
                self._canonical_harness_uri(name, specs_by_name.get(name)), running
            )
            running_by_actor.setdefault(name, running)
        read_refs = getattr(self._harnesses, "projected_worker_session_refs", None)
        refs = read_refs() if callable(read_refs) else {}
        session_ref_by_actor: dict[str, str | None] = {}
        for spec in specs:
            if spec.harness == "lark":
                continue
            session_ref_by_actor.setdefault(
                self._canonical_harness_uri(spec.name, spec),
                refs.get((spec.harness, spec.name)),
            )
        return _WorkerStatusSnapshot(
            statuses=tuple(statuses),
            running_by_actor=running_by_actor,
            session_ref_by_actor=session_ref_by_actor,
            desired=desired_loaded,
        )
