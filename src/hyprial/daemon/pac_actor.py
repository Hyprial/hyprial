"""Daemon adapter for PAC actors; delegates to the existing lifecycle manager."""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from hyprial.daemon.desired_state import HarnessLaunchSpec
from hyprial.daemon.lifecycle_manager import LifecycleOperation
from hyprial.pac.lifecycle import (
    ActorCoordinator,
    FileLaunchResolver,
    LaunchSpec,
    RuntimeObservation,
)
from hyprial.pac.reactor import NotificationSender, PacReactor
from hyprial.pac.store import PacGraphStore, default_database_path

#: One actor reconcile slower than this gets its own log line.  Close->reclaim
#: took 73 s on a 1 s tick with nothing logged (2026-09-24); per-job duration
#: is what tells a slow reconcile from one that never ran.
_PAC_RECONCILE_SLOW_MS = 2000


class DaemonActorRuntime:
    def __init__(self, application: Any) -> None:
        self.application = application

    def _spec(self, actor_name: str) -> HarnessLaunchSpec | None:
        return next(
            (
                item
                for item in self.application.desired_state.load().harnesses
                if item.name == actor_name and item.harness != "lark"
            ),
            None,
        )

    def observe(self, actor_name: str) -> RuntimeObservation:
        spec = self._spec(actor_name)
        if spec is None:
            return RuntimeObservation(False)
        actor = self.application._canonical_harness_uri(actor_name, spec)
        running = self.application._managed_worker_running(actor)
        return RuntimeObservation(
            present=running is True,
            identity_marker=(spec.nickname if spec.nickname and spec.nickname.startswith("pac:") else None),
            harness=spec.harness,
        )

    @staticmethod
    def _launch_spec(actor_name: str, launch: LaunchSpec, marker: str) -> HarnessLaunchSpec:
        candidate = HarnessLaunchSpec(
            harness=launch.harness,
            name=actor_name,
            headless=True,
            args=launch.args,
            cwd=launch.cwd,
            nickname=marker,
            model_provider=launch.provider,
            model=launch.model,
        )
        return HarnessLaunchSpec.from_json(candidate.to_json(), "pac actor launch")

    def start(
        self,
        actor_name: str,
        launch: LaunchSpec,
        *,
        operation_id: str,
        identity_marker: str,
    ) -> RuntimeObservation:
        spec = self._launch_spec(actor_name, launch, identity_marker)
        self.application._run_lifecycle_operation(
            LifecycleOperation.create(operation_id, self.application._lifecycle_spec(spec))
        )
        return self.observe(actor_name)

    def _interruption_reason(self, identity_marker: str) -> str:
        """Classify this PAC-owned stop from the graph's durable close fact."""

        parts = identity_marker.split(":", 2)
        if len(parts) < 3 or parts[0] != "pac":
            return "graph-cleanup"
        state_dir = getattr(self.application, "state_dir", None)
        if state_dir is None:
            return "graph-cleanup"
        store = PacGraphStore(default_database_path(state_dir), read_only=True)
        try:
            graph = store.graph(parts[1])
            if graph is None or graph["closed_at"] is None:
                return "graph-cleanup"
            workflow = store._db.execute(
                "SELECT state FROM workflow_graphs WHERE graph_id=?", (parts[1],)
            ).fetchone()
            if workflow is not None:
                if workflow["state"] == "cancelled":
                    return "graph-cancelled"
                if workflow["state"] in {"completed", "failed"}:
                    return "graph-settled"
                # A graph close is visible before the workflow projector can
                # stamp its terminal state only on the explicit cancel path.
                return "graph-cancelled"
            row = store._db.execute(
                "SELECT data_json FROM journal "
                "WHERE graph_id=? AND type='graph_closed' "
                "ORDER BY at DESC, rowid DESC LIMIT 1",
                (parts[1],),
            ).fetchone()
            if row is not None:
                detail = json.loads(row["data_json"])
                if detail.get("state") == "cancelled":
                    return "graph-cancelled"
            return "graph-settled"
        finally:
            store.close()

    def stop(
        self,
        actor_name: str,
        launch: LaunchSpec,
        *,
        operation_id: str,
        identity_marker: str,
    ) -> RuntimeObservation:
        spec = self._spec(actor_name)
        # Never stop a same-name connector whose durable marker is not ours.
        if spec is None or spec.nickname != identity_marker:
            return self.observe(actor_name)
        lifecycle_spec = replace(
            self.application._lifecycle_spec(spec),
            interruption_reason=self._interruption_reason(identity_marker),
        )
        self.application._run_lifecycle_operation(
            LifecycleOperation.deactivate(operation_id, lifecycle_spec)
        )
        return self.observe(actor_name)


class DaemonPacNotificationSender:
    def __init__(self, application: Any) -> None:
        self.application = application

    def send(
        self,
        *,
        recipient: str,
        text: str,
        sender: str,
        conversation_id: str,
        idempotency_key: str,
        expires_at_ms: int | None = None,
    ) -> str:
        delivery_io = self.application._pac_notification_io
        if delivery_io is None:
            raise RuntimeError("PAC typed inbox delivery is unavailable")
        # The planned sender is the principal whose verified action caused
        # this notification (a flag set, the graph run), or the graph's
        # creator when nothing did (deadline, timeout).  Allen, 2026-09-25:
        # ownership and the source of a handoff are separate; a hard-coded
        # service identity (formerly `mfu-coordinator`) is not acceptable.
        delivered = delivery_io.deliver(
            effect_id=f"pac:{idempotency_key}",
            sender=sender,
            target=recipient,
            conversation_id=conversation_id,
            text=text,
            expires_at_ms=expires_at_ms,
        )
        return str(delivered.message_id)

    def send_workflow_request(self, request, *, text, idempotency_key):
        remote = self.application._remote_workflow
        if remote is None:
            # Startup recovery must not lose the remote request binding.
            from hyprial.uri import parse_agent_uri
            principal = parse_agent_uri(request["owner"])
            if principal and principal[:2] != (self.application.owner, self.application.node_id):
                raise RuntimeError("remote workflow service is not running")
            return None
        return remote.send(request, text=text, idempotency_key=idempotency_key)


class PacActorService:
    """Resident PAC maintenance with independent clock and actor queues.

    Every active, open graph gets a clock job even when it has no actor nodes.
    Actor reconciliation keeps its own bounded worker pool so an actor backlog
    cannot make the resident clock loop silently miss deadlines.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        reference_root: Path,
        runtime: DaemonActorRuntime,
        sender: NotificationSender,
        daemon_epoch: str,
        logger: Callable[..., None],
        workers: int = 4,
    ) -> None:
        self.database = default_database_path(state_dir)
        self.reference_root = reference_root
        self.runtime = runtime
        self.sender = sender
        self.daemon_epoch = daemon_epoch
        self.logger = logger
        self._actor_queue: queue.Queue[tuple[str, str] | None] = queue.Queue(maxsize=128)
        self._clock_queue: queue.Queue[str | None] = queue.Queue(maxsize=128)
        self._active_actors: set[tuple[str, str]] = set()
        self._active_clocks: set[str] = set()
        # Last skip reason logged per (graph, node): logged on change only, so
        # a skip that repeats every tick is one line, not one per second.
        self._skip_reasons: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self._closed = False
        # Last, and before the worker threads: the failure branch reports
        # through `self.logger`, and a future provisioning step that consults
        # `self._closed` or the queue must find them built.
        self._provision_database()
        self._actor_threads = tuple(
            threading.Thread(
                target=self._actor_worker,
                name=f"hyprial-pac-actor-{index}",
                daemon=True,
            )
            for index in range(workers)
        )
        self._clock_thread = threading.Thread(
            target=self._clock_worker,
            name="hyprial-pac-clock",
            daemon=True,
        )
        self._threads = (*self._actor_threads, self._clock_thread)
        for thread in self._threads:
            thread.start()

    def _provision_database(self) -> None:
        """Create and migrate the pac-graph database on the daemon's startup.

        ``submit_tick`` returns early when this file is missing, so without
        this the daemon reaches ``serving`` with PAC permanently inert -- and
        silently, because "no database" and "nothing to do" take the same
        branch.  Nothing else on the startup path opens it: every other
        creation site is either the ``workflow commands` CLI or a test that builds its
        own store, which is why 3900+ green tests never asked who provisions
        it in production.

        ⛔ Not in ``submit_tick``: creating it there ties provisioning to the
        first tick, whose own precondition is that the file already exists.

        ``connect`` both creates a fresh database and migrates an existing one
        (``CREATE TABLE IF NOT EXISTS`` plus ``migrate``), so the fresh-home and
        upgraded-home paths need no separate handling here.

        A failure must not stop the daemon -- PAC is one subsystem and the node
        has other work -- but it must not be swallowed either, or this returns
        as the same silent inertness.  Log it at ``error`` and carry on.
        """

        try:
            PacGraphStore(self.database).close()
        except Exception as error:  # noqa: BLE001 - startup must not depend on it
            self.logger(
                "error",
                "pac",
                "pac.database.provision_failed",
                database=str(self.database),
                detail=str(error),
            )

    def submit_tick(self) -> None:
        """Admit one resident cadence without deriving clocks from actors."""

        if self._closed or not self.database.exists():
            return
        store = PacGraphStore(self.database)
        try:
            graphs = list(
                store._db.execute(
                    "SELECT graph_id, activated_at, closed_at "
                    "FROM graphs ORDER BY graph_id"
                )
            )
            clock_jobs = [
                str(graph["graph_id"])
                for graph in graphs
                if graph["activated_at"] is not None and graph["closed_at"] is None
            ]
            actor_jobs = [
                (row.graph_id, row.node_id)
                for graph in graphs
                for row in store.nodes(str(graph["graph_id"]))
                if row.kind == "actor"
            ]
        finally:
            store.close()
        with self._lock:
            for graph_id in clock_jobs:
                if graph_id in self._active_clocks:
                    continue
                try:
                    self._clock_queue.put_nowait(graph_id)
                except queue.Full:
                    self.logger(
                        "warn",
                        "pac",
                        "pac.clock.queue_full",
                        graphId=graph_id,
                        capacity=self._clock_queue.maxsize,
                    )
                    break
                self._active_clocks.add(graph_id)
            for job in actor_jobs:
                if job in self._active_actors:
                    continue
                try:
                    self._actor_queue.put_nowait(job)
                except queue.Full:
                    self.logger(
                        "warn",
                        "pac",
                        "pac.actor.queue_full",
                        capacity=self._actor_queue.maxsize,
                    )
                    break
                self._active_actors.add(job)

    def _clock_worker(self) -> None:
        while True:
            graph_id = self._clock_queue.get()
            if graph_id is None:
                return
            if self._closed:
                # close() has begun: drop the remaining backlog instead of
                # draining it.  Queued ticks are a replayable view of the
                # graphs table -- the next service instance's submit_tick
                # re-derives every clock job, and tick side effects are
                # SQL-idempotent across restarts -- so discarding them here
                # loses nothing, while draining them would tie close()
                # latency to the backlog size.
                return
            try:
                store = PacGraphStore(self.database)
                try:
                    PacReactor(store, sender=self.sender).tick_clocks(graph_id)
                finally:
                    store.close()
            except Exception as error:  # noqa: BLE001 - isolate one graph from the service
                self.logger(
                    "error",
                    "pac",
                    "pac.clock.tick_failed",
                    graphId=graph_id,
                    errorType=type(error).__name__,
                    detail=str(error)[:500],
                )
            finally:
                with self._lock:
                    self._active_clocks.discard(graph_id)

    def _actor_worker(self) -> None:
        while True:
            job = self._actor_queue.get()
            if job is None:
                return
            if self._closed:
                # close() has begun: defer the remaining backlog rather than
                # drain it.  The queue is not the system of record --
                # submit_tick re-derives every actor job from the nodes
                # table on the next daemon epoch, and reconcile() converges
                # the durable actor_activations rows idempotently -- so a
                # dropped job is reconciled by the next service instance,
                # not lost.  Draining here would also *start* new actors
                # while the daemon is tearing down, which is the opposite
                # of what close() is for.
                return
            skipped: list[str] = []

            def on_skip(
                graph_id: str, node_id: str, reason: str, detail: dict[str, Any]
            ) -> None:
                skipped.append(reason)
                self._note_skip(graph_id, node_id, reason, detail)

            started = time.monotonic()
            try:
                store = PacGraphStore(self.database)
                try:
                    coordinator = ActorCoordinator(
                        store,
                        self.runtime,
                        daemon_epoch=self.daemon_epoch,
                        resolver=FileLaunchResolver(self.reference_root),
                        sender=self.sender,
                        on_skip=on_skip,
                    )
                    coordinator.reconcile(job[0], job[1])
                finally:
                    store.close()
                if not skipped:
                    with self._lock:
                        self._skip_reasons.pop(job, None)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if elapsed_ms >= _PAC_RECONCILE_SLOW_MS:
                    self.logger(
                        "warn",
                        "pac",
                        "pac.actor.reconcile_slow",
                        graphId=job[0],
                        nodeId=job[1],
                        elapsedMs=elapsed_ms,
                    )
            except Exception as error:  # noqa: BLE001 - isolate one actor from the service
                self.logger(
                    "error",
                    "pac",
                    "pac.actor.reconcile_failed",
                    graphId=job[0],
                    nodeId=job[1],
                    errorType=type(error).__name__,
                    detail=str(error)[:500],
                )
            finally:
                with self._lock:
                    self._active_actors.discard(job)

    def _note_skip(
        self, graph_id: str, node_id: str, reason: str, detail: dict[str, Any]
    ) -> None:
        with self._lock:
            if self._skip_reasons.get((graph_id, node_id)) == reason:
                return
            self._skip_reasons[(graph_id, node_id)] = reason
        self.logger(
            "warn",
            "pac",
            "pac.actor.reconcile_skipped",
            graphId=graph_id,
            nodeId=node_id,
            reason=reason,
            **detail,
        )

    def close(self, timeout: float = 5.0) -> bool:
        self._closed = True
        deadline = time.monotonic() + timeout

        def stop(queue_: queue.Queue[Any], count: int) -> None:
            for _index in range(count):
                try:
                    queue_.put(None, timeout=max(0.0, deadline - time.monotonic()))
                except queue.Full:
                    return

        # A full queue is exactly when a non-blocking sentinel would be lost.
        # Wait only inside the caller's close budget so draining work can make
        # room without turning shutdown into an unbounded wait.
        stop(self._clock_queue, 1)
        stop(self._actor_queue, len(self._actor_threads))
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)
