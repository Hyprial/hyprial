"""Daemon adapter for PAC actors; delegates to the existing lifecycle manager."""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Protocol

from hyprial.kernel import HarnessLaunchSpec, HookEventPublisher, DECLARED_HARNESSES, DaemonRequestError, ipc_errors
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation
from hyprial.daemon.impl.pac.actors.coordinator import (
    ActorCoordinator,
    FileLaunchResolver,
    LaunchSpec,
    RuntimeObservation,
    throttle_cleanup_retries,
)
from hyprial.daemon.impl.pac.actors.clocks import (
    PAC_CLOCK_BATCH as _PAC_CLOCK_BATCH,
    PAC_CLOCK_QUEUE_JOBS as _PAC_CLOCK_QUEUE_JOBS,
    run_clock_worker,
)
from hyprial.daemon.impl.pac.actors.marker import pac_worker_marker
from hyprial.daemon.impl.pac.graphs.reactor import NotificationSender
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path


class GraphClockAuthority(Protocol):
    def clock_tick(self, graph_id: str) -> dict[str, Any]: ...

    def clock_tick_many(self, graph_ids: Any, **kwargs: Any) -> dict[str, Any]: ...




#: One actor reconcile slower than this gets its own log line.  Close->reclaim
#: took 73 s on a 1 s tick with nothing logged (2026-09-24); per-job duration
#: is what tells a slow reconcile from one that never ran.
_PAC_RECONCILE_SLOW_MS = 2000

# When both actor classes are pending, seven admissions go to open graphs and
# the eighth goes to closed cleanup.  The persistent turn makes that guarantee
# hold even when the queue exposes only one free slot on each resident tick.
_ACTOR_CLASS_CYCLE = 8

# The actor supply is event-driven: a durable graph change (activate/close,
# flag/workflow node change, worker lifecycle) wakes exactly the affected
# actor(s) through the graph authority.  This constant is the *fallback*
# period of the one periodic safety sweep that catches a missed event.  On
# 2026-10-13 the every-tick full re-derivation supplied ~119 open-graph actors
# per second, drove ~107 effect completions/s and saturated the 128-slot
# authority mailbox (``pac.actor.queue_full``).  A missed event now costs at
# most this period of lag, at ~1/300th the supplied commands; 300 s matches
# ``PAC_GC_INTERVAL_SECONDS`` and is bounded further by every later event on
# the same graph.
PAC_RECONCILE_FALLBACK_SECONDS = 300.0

# Tier 2 of the fallback supply: an actor whose activation settled at
# ``desired='up', op='done'`` is *running*, and a running harness process can
# die without emitting any PAC event (the harness-outcome write is keyed by
# ``message_id``, and an idle death has none).  This sweep is therefore the
# only periodic observer for that class, so it is retained -- but on the
# cheap tier rather than every tick: 30 s is the tier #1431 already used
# (``_ACTOR_IDLE_REFRESH_TICKS = 30``), i.e. the pre-existing cadence, not a
# new mechanism.  Today that is 3 nodes, ~0.10 supplied jobs/s.
PAC_RECONCILE_RUNNING_REFRESH_SECONDS = 30.0


def _stop_fence_matches(nickname: str | None, identity_marker: str) -> bool:
    """Whether a stop fence names exactly the observed desired-state record.

    A PAC marker fence must equal the record's nickname.  The empty fence is
    the coordinator's stop for a receipt-proven agent whose record carries no
    PAC marker; it never matches a record that does carry one.
    """

    if nickname == identity_marker:
        return True
    return identity_marker == "" and pac_worker_marker(nickname) is None


class DaemonActorRuntime:
    def __init__(self, application: Any) -> None:
        self.application = application

    def _spec(self, actor_name: str) -> HarnessLaunchSpec | None:
        # Resolve only the worker collection, including migrated name collisions.
        return self.application.desired_state.harness_spec(
            actor_name
        )

    def observe(self, actor_name: str) -> RuntimeObservation:
        spec = self._spec(actor_name)
        agent = self.application.agents.get(actor_name)
        pinned = bool(getattr(agent, "pinned_adapters", ()))
        if spec is None:
            return RuntimeObservation(
                False,
                pinned=pinned,
                interactive_online=self._interactive_online(agent, None),
            )
        actor = self.application._canonical_harness_uri(actor_name, spec)
        running = self.application._managed_worker_running(actor)
        return RuntimeObservation(
            pinned=pinned,
            interactive_online=self._interactive_online(agent, actor),
            present=running is True,
            identity_marker=pac_worker_marker(spec.nickname),
            harness=spec.harness,
            agent_entity_token=(agent.entity_token if agent is not None else None),
            # A non-running lifecycle record is still a durable desired intent.
            # Terminal PAC cleanup must deactivate it before declaring the worker
            # absent, or the harness reconciler can start it again indefinitely.
            retirement_pending=running is not True,
        )

    def _interactive_online(self, agent: Any, actor: str | None) -> bool:
        """Whether a person may hold a live interactive session on this agent.

        Read from the agent record, not the worker spec: cleanup completes
        only after the spec is deactivated, so GC and the destroy recheck
        always observe a spec-less worker.  Fails closed: a session registry
        or presence that cannot answer counts as in use.
        """

        names = {
            name
            for name in (actor, getattr(agent, "uri", None), getattr(agent, "actor", None))
            if isinstance(name, str) and name
        }
        if not names:
            return False
        try:
            matched = names & set(self.application._registered_interactive_actors())
        except Exception:  # noqa: BLE001 - unknown session state is in use
            return True
        if not matched:
            return False
        presence = getattr(self.application, "_presence", None)
        if presence is None:
            return True
        try:
            return any(bool(presence.actor_online(name)) for name in matched)
        except Exception:  # noqa: BLE001 - unknown presence is in use
            return True

    @staticmethod
    def _launch_spec(
        actor_name: str, launch: LaunchSpec, marker: str
    ) -> HarnessLaunchSpec:
        if launch.harness not in DECLARED_HARNESSES:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"{actor_name} has an invalid harness; lifecycle requires a declared worker harness",
            )
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
        if launch.cwd_policy == "create" and launch.cwd is not None:
            Path(launch.cwd).mkdir(parents=True, exist_ok=True)
        spec = self._launch_spec(actor_name, launch, identity_marker)
        self.application._run_lifecycle_operation(
            LifecycleOperation.create(
                operation_id, self.application._lifecycle_spec(spec)
            )
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
        if spec is None or not _stop_fence_matches(spec.nickname, identity_marker):
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
        summary: str,
        expires_at_ms: int | None = None,
    ) -> str:
        delivery_io = self.application._pac_notification_io
        if delivery_io is None:
            raise RuntimeError("PAC typed inbox delivery is unavailable")
        # The planned sender is the principal whose verified action caused
        # this notification (a flag set, the graph run), or the graph's
        # creator when nothing did (deadline, timeout).  Allen, 2026-09-25:
        # ownership and the source of a handoff are separate; a hard-coded
        # service identity (the dispatch coordinator) is not acceptable.
        # WP2 §3.7: register the PAC task link before the typed inbox write,
        # and only count the in-flight request once delivery really happened.
        binder = getattr(self.application, "_link_task_binder", None)
        on_delivered = (
            binder(
                sender=sender,
                recipient=recipient,
                conversation_id=conversation_id,
            )
            if binder is not None
            else None
        )
        delivered = delivery_io.deliver(
            effect_id=f"pac:{idempotency_key}",
            sender=sender,
            target=recipient,
            conversation_id=conversation_id,
            text=text,
            summary=summary,
            expires_at_ms=expires_at_ms,
        )
        if on_delivered is not None:
            on_delivered(str(delivered.message_id))
        return str(delivered.message_id)

    def send_workflow_request(self, request, *, text, idempotency_key):
        remote = self.application._remote_workflow
        if remote is None:
            # Startup recovery must not lose the remote request binding.
            from hyprial.kernel import parse_agent_uri

            principal = parse_agent_uri(request["owner"])
            if principal and principal[:2] != (
                self.application.owner,
                self.application.node_id,
            ):
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
        graph_authority: GraphClockAuthority | None = None,
        hook_publisher: HookEventPublisher | None = None,
    ) -> None:
        self.database = default_database_path(state_dir)
        self.reference_root = reference_root
        self.runtime = runtime
        self.sender = sender
        self.daemon_epoch = daemon_epoch
        self.logger = logger
        self._graph_authority = graph_authority
        self._hook_publisher = hook_publisher
        self._actor_queue: queue.Queue[tuple[str, str] | None] = queue.Queue(
            maxsize=128
        )
        self._clock_queue: queue.Queue[list[str] | None] = queue.Queue(
            maxsize=max(1, _PAC_CLOCK_QUEUE_JOBS // _PAC_CLOCK_BATCH)
        )
        # Cadence admission is deliberately independent of graph I/O. A
        # single pending scan coalesces ticks while the database is slow.
        self._scan_queue: queue.Queue[bool | None] = queue.Queue(maxsize=1)
        self._active_actors: set[tuple[str, str]] = set()
        self._active_clocks: set[str] = set()
        self._actor_scan_offsets = {"open": 0, "closed": 0}
        self._actor_class_turn = 0
        self._clock_scan_offset = 0
        self._actor_queue_saturated = False
        # Monotonic deadline of the next fallback actor sweep.  0.0 makes the
        # first cadence after startup a full sweep, which recovers events the
        # previous daemon epoch may have missed.  Guarded by ``_lock``.
        self._actor_fallback_due = 0.0
        # Monotonic deadline of the next *running-subset* sweep.  0.0 makes the
        # first cadence after startup observe running actors too, so a restart
        # also recovers a settled actor whose harness died mid-crash.
        self._actor_running_due = 0.0
        # Last skip reason logged per (graph, node): logged on change only, so
        # a skip that repeats every tick is one line, not one per second.
        self._skip_reasons: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self._clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000
        self._closed = False
        # Last, and before the worker threads: the failure branch reports
        # through `self.logger`, and a future provisioning step that consults
        # `self._closed` or the queue must find them built.
        self._provision_database()
        attach = getattr(self._graph_authority, "attach_reconciler", None)
        if callable(attach):
            attach(
                self.runtime,
                self.reference_root,
                self.daemon_epoch,
                on_skip=self._note_skip,
                logger=self.logger,
                on_actor_wake=self.note_actor_dirty,
            )
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
        self._scan_thread = threading.Thread(
            target=self._scan_worker,
            name="hyprial-pac-scan",
            daemon=True,
        )
        self._threads = (*self._actor_threads, self._clock_thread, self._scan_thread)
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
            PacGraphStore(
                self.database, read_only=self._graph_authority is not None
            ).close()
        except Exception as error:  # noqa: BLE001 - startup must not depend on it
            self.logger(
                "error",
                "pac",
                "pac.database.provision_failed",
                database=str(self.database),
                detail=str(error),
            )

    def submit_tick(self) -> None:
        """Coalesce a cadence without opening SQLite on the timer thread."""

        with self._lock:
            if self._closed:
                return
            try:
                self._scan_queue.put_nowait(True)
            except queue.Full:
                # The pending scan will read the latest durable graph state.
                pass

    def note_actor_dirty(self, graph_id: str, node_id: str | None = None) -> int:
        """Event hook: enqueue a targeted reconcile for the affected actor(s).

        The graph authority calls this after a durable change that can make an
        actor actionable (graph activate/close, a flag or workflow node change,
        a worker lifecycle mutation).  Unlike the fallback sweep this does not
        apply the settled gate: the event is the authority that something
        changed.  Returns the number of jobs enqueued; a full queue drops the
        signal and the fallback sweep is the net.
        """

        jobs = (
            [(graph_id, node_id)]
            if node_id is not None
            else self._graph_actor_jobs(graph_id)
        )
        added = 0
        with self._lock:
            if self._closed:
                return 0
            for job in jobs:
                if job in self._active_actors:
                    continue
                try:
                    self._actor_queue.put_nowait(job)
                except queue.Full:
                    break
                self._active_actors.add(job)
                added += 1
        return added

    def _graph_actor_jobs(self, graph_id: str) -> list[tuple[str, str]]:
        """Every actor node of one graph, for a graph-level event wake."""

        if not self.database.exists():
            return []
        store = PacGraphStore(self.database, read_only=True)
        try:
            return [
                (graph_id, str(node.node_id))
                for node in store.nodes(graph_id)
                if node.kind == "actor"
            ]
        except Exception:  # noqa: BLE001 - a wake must never break a graph write
            return []
        finally:
            store.close()

    def _scan_worker(self) -> None:
        while True:
            scan = self._scan_queue.get()
            if scan is None or self._closed:
                return
            try:
                self._scan_and_admit()
            except Exception as error:  # noqa: BLE001 - next cadence retries durable state
                self.logger(
                    "error",
                    "pac",
                    "pac.scan.failed",
                    errorType=type(error).__name__,
                    detail=str(error)[:500],
                )

    def _scan_and_admit(self, *, force_actors: bool = False) -> None:
        """One resident cadence: always supply clocks; supply actors when due.

        Actor supply is event-driven (see :meth:`note_actor_dirty`); this scan
        is the fallback supply, and it has two tiers (``force_actors`` runs
        both, which is how the deterministic test probe and the first cadence
        after startup recover durable state):

        * the dirty set -- a committed in-transition activation, a pending or
          attention cleanup, or a closed receipt that still lacks its cleanup
          intent -- once per :data:`PAC_RECONCILE_FALLBACK_SECONDS`; and
        * the running subset (``desired='up', op='done'``), which no event can
          announce a death for, once per
          :data:`PAC_RECONCILE_RUNNING_REFRESH_SECONDS`.

        Never-activated nodes and settled rows that are not running are
        outside both tiers: they cannot change durable state, and re-deriving
        them every tick is what saturated the authority mailbox on
        2026-10-13.  The timer that calls :meth:`submit_tick` still runs at the
        maintenance cadence -- only the *supplied jobs* shrink.
        """

        if not self.database.exists():
            return
        now = time.monotonic()
        actor_due = force_actors or now >= self._actor_fallback_due
        running_due = force_actors or now >= self._actor_running_due
        store = PacGraphStore(self.database, read_only=True)
        try:
            # The clock-job set is pushed down to SQL: the predicate is the
            # same four cells as before, but the query no longer fetches the
            # whole graph history to filter it in Python.  Closed-graph
            # cleanup has its own query and does not arrive here.
            _clock_rows, clock_jobs = store.tick_clock_jobs()
            # Read-only compatibility for the pinned schema-15 candidate.
            # Schema 16 cleanup intents become authoritative after dev sync.
            has_cleanup_intents = (
                store._db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='workflow_worker_cleanup_intents'"
                ).fetchone()
                is not None
            )
            cleanup_column = (
                "c.state AS cleanup_state,c.attempts AS cleanup_attempts,"
                "c.updated_at AS cleanup_updated_at,"
                if has_cleanup_intents
                else "NULL AS cleanup_state,NULL AS cleanup_attempts,"
                "NULL AS cleanup_updated_at,"
            )
            cleanup_join = (
                "LEFT JOIN workflow_worker_cleanup_intents c "
                "ON c.graph_id=n.graph_id AND c.actor_node=n.node_id "
                if has_cleanup_intents
                else ""
            )
            cleanup_pending = (
                "c.state IN ('pending','attention')" if has_cleanup_intents else "0"
            )
            cleanup_absent = "c.state IS NULL" if has_cleanup_intents else "1"
            # Added to the open-graph gate only on the running tier: a running
            # actor is re-observed on the cheap cadence so a silent harness
            # death is bounded by PAC_RECONCILE_RUNNING_REFRESH_SECONDS.
            settled_running = (
                " OR (a.desired='up' AND a.op='done')" if running_due else ""
            )
            # The fallback sweep is the missed-event net, so it must not
            # re-derive a settled actor (``op='done'``: no reconcile can
            # change its durable state) or a never-activated node (no
            # committed request yet).  Both classes arrive by event; the
            # 2026-10-13 storm was 100% these.  A pending cleanup is always
            # actionable, settled or not.
            actor_rows = (
                list(
                    store._db.execute(
                        "SELECT n.graph_id,n.node_id,g.activated_at,g.closed_at,"
                        "a.desired,a.op,"
                        + cleanup_column
                        + "r.graph_id AS receipt_graph_id "
                        "FROM graphs g CROSS JOIN nodes n ON n.graph_id=g.graph_id "
                        "LEFT JOIN actor_activations a "
                        "ON a.graph_id=n.graph_id AND a.node_id=n.node_id "
                        + cleanup_join
                        + "LEFT JOIN workflow_worker_receipts r "
                        "ON r.graph_id=n.graph_id AND r.actor_node=n.node_id "
                        "WHERE n.kind='actor' AND ("
                        "(g.activated_at IS NOT NULL AND g.closed_at IS NULL AND ("
                        + cleanup_pending
                        + " OR (a.desired IS NOT NULL "
                        "AND (a.op IS NULL OR a.op!='done'))"
                        + settled_running
                        + ")) OR "
                        "(g.closed_at IS NOT NULL AND ("
                        + cleanup_pending
                        + " OR ("
                        + cleanup_absent
                        + " AND r.graph_id IS NOT NULL) OR ("
                        + cleanup_absent
                        + " AND r.graph_id IS NULL AND a.desired IS NOT NULL "
                        "AND (a.desired!='down' OR a.op!='done'))))) "
                        "ORDER BY n.graph_id,n.node_id"
                    )
                )
                if actor_due or running_due
                else []
            )
            worktree_jobs = [
                (str(row["graph_id"]), str(row["node_id"]))
                for row in store._db.execute(
                    "SELECT w.graph_id,w.node_id FROM workflow_worktrees w "
                    "JOIN graphs g ON g.graph_id=w.graph_id "
                    "JOIN workflow_worker_cleanup_intents c ON c.graph_id=w.graph_id "
                    "AND c.actor_node=w.actor_node "
                    "WHERE g.closed_at IS NOT NULL AND w.state='cleanup_pending' "
                    "AND c.state='complete' ORDER BY w.updated_at,w.graph_id,w.node_id "
                    "LIMIT 16"
                )
            ]
            open_actor_jobs = [
                (str(row["graph_id"]), str(row["node_id"]))
                for row in actor_rows
                if row["activated_at"] is not None and row["closed_at"] is None
            ]
            # A closed graph is actionable only while its durable lifecycle
            # state lets reconcile make progress.  Managed workers require
            # their receipt-owned pending cleanup intent; unmanaged actors
            # keep the pre-workflow down reconciliation until it settles.
            closed_actor_jobs = [
                (str(row["graph_id"]), str(row["node_id"]))
                for row in actor_rows
                if row["closed_at"] is not None
                and (
                    row["cleanup_state"] in ("pending", "attention")
                    or (
                        row["cleanup_state"] is None
                        and row["receipt_graph_id"] is not None
                    )
                    or (
                        row["cleanup_state"] is None
                        and row["receipt_graph_id"] is None
                        and row["desired"] is not None
                        and (row["desired"] != "down" or row["op"] != "done")
                    )
                )
            ]
            effects = getattr(self._graph_authority, "actor_effect_keys", None)
            with self._lock:
                active = frozenset(self._active_actors)
            if callable(effects):
                active |= effects()
            closed_actor_jobs = throttle_cleanup_retries(
                actor_rows, closed_actor_jobs, self._clock_ms(), active
            )
        finally:
            store.close()
        warnings: list[tuple[str, dict[str, object]]] = []
        with self._lock:
            if self._closed:
                return
            if actor_due:
                self._actor_fallback_due = now + PAC_RECONCILE_FALLBACK_SECONDS
            if running_due:
                self._actor_running_due = now + (
                    PAC_RECONCILE_RUNNING_REFRESH_SECONDS
                )
            if not clock_jobs:
                self._clock_scan_offset = 0
            clock_offset = (
                self._clock_scan_offset % len(clock_jobs) if clock_jobs else 0
            )
            rotated_clocks = clock_jobs[clock_offset:] + clock_jobs[:clock_offset]
            pending = [
                graph_id
                for graph_id in rotated_clocks
                if graph_id not in self._active_clocks
            ]
            for start in range(0, len(pending), _PAC_CLOCK_BATCH):
                batch = pending[start:start + _PAC_CLOCK_BATCH]
                try:
                    self._clock_queue.put_nowait(batch)
                except queue.Full:
                    self._clock_scan_offset = (
                        clock_offset + start
                    ) % len(clock_jobs)
                    warnings.append(
                        (
                            "pac.clock.queue_full",
                            {
                                "graphId": batch[0],
                                "batchSize": len(batch),
                                # capacity stays expressed in clock jobs: the
                                # queue admits batches, the policy is about jobs
                                "capacity": _PAC_CLOCK_QUEUE_JOBS,
                            },
                        )
                    )
                    break
                self._active_clocks.update(batch)
            if actor_due or running_due:
                self._admit_actor_sweep(open_actor_jobs, closed_actor_jobs, warnings)

        for event, fields in warnings:
            self.logger("warn", "pac", event, **fields)
        reconcile_worktree = getattr(
            self._graph_authority, "reconcile_worktree", None
        )
        if callable(reconcile_worktree):
            for graph_id, node_id in worktree_jobs:
                reconcile_worktree(graph_id, node_id)

    def _admit_actor_sweep(
        self,
        open_actor_jobs: list[tuple[str, str]],
        closed_actor_jobs: list[tuple[str, str]],
        warnings: list[tuple[str, dict[str, object]]],
    ) -> None:
        """Enqueue one bounded, fair sweep of the fallback actor rows.

        Caller holds ``_lock``.  ``warnings`` is appended to, never logged
        here: logging under the lock would serialize the worker threads
        behind an I/O call.
        """

        actor_groups = (
            ("open", open_actor_jobs),
            ("closed", closed_actor_jobs),
        )
        available = self._actor_queue.maxsize - self._actor_queue.qsize()
        pending: dict[str, list[tuple[int, tuple[str, str]]]] = {}
        base_offsets: dict[str, int] = {}
        for group, jobs in actor_groups:
            if not jobs:
                self._actor_scan_offsets[group] = 0
                pending[group] = []
                base_offsets[group] = 0
                continue
            offset = self._actor_scan_offsets[group] % len(jobs)
            base_offsets[group] = offset
            rotated = jobs[offset:] + jobs[:offset]
            pending[group] = [
                (job_index, job)
                for job_index, job in enumerate(rotated)
                if job not in self._active_actors
            ]

        positions = {"open": 0, "closed": 0}
        while available:
            open_waiting = positions["open"] < len(pending["open"])
            closed_waiting = positions["closed"] < len(pending["closed"])
            if not open_waiting and not closed_waiting:
                break
            if open_waiting and closed_waiting:
                group = (
                    "closed"
                    if self._actor_class_turn == _ACTOR_CLASS_CYCLE - 1
                    else "open"
                )
                self._actor_class_turn = (
                    self._actor_class_turn + 1
                ) % _ACTOR_CLASS_CYCLE
            else:
                group = "open" if open_waiting else "closed"

            job_index, job = pending[group][positions[group]]
            positions[group] += 1
            try:
                self._actor_queue.put_nowait(job)
            except queue.Full:
                positions[group] -= 1
                break
            self._active_actors.add(job)
            jobs = open_actor_jobs if group == "open" else closed_actor_jobs
            self._actor_scan_offsets[group] = (
                base_offsets[group] + job_index + 1
            ) % len(jobs)
            available -= 1

        open_waiting = positions["open"] < len(pending["open"])
        closed_waiting = positions["closed"] < len(pending["closed"])
        deferred_group: str | None = None
        if open_waiting and closed_waiting:
            deferred_group = (
                "closed"
                if self._actor_class_turn == _ACTOR_CLASS_CYCLE - 1
                else "open"
            )
        elif open_waiting:
            deferred_group = "open"
        elif closed_waiting:
            deferred_group = "closed"
        deferred = sum(
            len(pending[group]) - positions[group]
            for group in ("open", "closed")
        )
        if deferred == 0:
            self._actor_queue_saturated = False
        elif not self._actor_queue_saturated:
            warnings.append(
                (
                    "pac.actor.queue_full",
                    {
                        "capacity": self._actor_queue.maxsize,
                        "deferredJobs": deferred,
                        "jobClass": deferred_group,
                    },
                )
            )
            self._actor_queue_saturated = True

    def _clock_worker(self) -> None:
        """Thread target; the drain loop lives in ``actors.clocks``."""

        run_clock_worker(self)

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
                reconcile = getattr(self._graph_authority, "reconcile_actor", None)
                actorized = callable(reconcile)
                if actorized:
                    admission = reconcile(job[0], job[1])
                    if getattr(admission, "value", "accepted") != "accepted":
                        self.logger(
                            "warn",
                            "pac",
                            "pac.actor.queue_full",
                            graphId=job[0],
                            nodeId=job[1],
                            admission=str(getattr(admission, "value", admission)),
                        )
                else:
                    store = PacGraphStore(self.database)
                    try:
                        coordinator = ActorCoordinator(
                            store,
                            self.runtime,
                            daemon_epoch=self.daemon_epoch,
                            resolver=FileLaunchResolver(self.reference_root),
                            sender=self.sender,
                            on_skip=on_skip,
                            logger=self.logger,
                            publisher=self._hook_publisher,
                        )
                        coordinator.reconcile(job[0], job[1])
                    finally:
                        store.close()
                if not skipped and not actorized:
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
        with self._lock:
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
        stop(self._scan_queue, 1)
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)
