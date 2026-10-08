"""Daemon shutdown authority: budgeted close steps, signal handling, exit backstop and stall traces."""

from __future__ import annotations

from __future__ import annotations
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from types import FrameType
from typing import Any, TYPE_CHECKING
from hyprial.daemon.impl.transport import (
    KeySpace,
)
from hyprial.daemon.impl.bootstrap.exit_independence  import do_not_exit, finish_shutdown
from hyprial.daemon.impl.processes.shutdown_stall  import (
    dump_live_threads,
    live_thread_summary,
    stall_dump_path,
)
from hyprial.kernel import (
    DAEMON_CLOSE_BUDGET_SECONDS,
    DAEMON_CLOSE_STEP_BUDGETS,
    DAEMON_EXIT_BACKSTOP_SECONDS,
    TURN_HOOK_CLOSE_TIMEOUT_SECONDS,
)
from hyprial.daemon.impl.configuration.identity import (
    classify_target_identity,
)
from hyprial.kernel import (
    ADAPTER_URI_PREFIX,
    ROUTE_URI_PREFIX,
    TARGET_KIND_HOST,
    canonical_agent_uri,
    canonical_user_uri,
    parse_channel_uri,
)
if TYPE_CHECKING:
    pass



_CLOSE_BUDGET_SECONDS = DAEMON_CLOSE_BUDGET_SECONDS

_CLOSE_STEP_BUDGETS = DAEMON_CLOSE_STEP_BUDGETS

_EXIT_BACKSTOP_SECONDS = DAEMON_EXIT_BACKSTOP_SECONDS

_TURN_HOOK_CLOSE_TIMEOUT = TURN_HOOK_CLOSE_TIMEOUT_SECONDS

_CLOSE_UNBUDGETED_STEPS = (
    ("ipc-server", "closes a listening socket"),
    ("reserve-fd", "a single os.close"),
    ("ipc-clients", "closes accepted sockets already marked closing"),
    ("socket-file", "one unlink"),
    ("daemon-json", "one unlink"),
    ("recipient-wake-observer", "clears the committed-presence observer under an in-memory lock; no I/O or worker join"),
    (
        "directory",
        "UNBOUNDED as a whole: rebuild-hook removal and native registration "
        "undeclare have no shared end-to-end deadline; the subsequent presence "
        "authority drain has its own separate 1s budget",
    ),
    ("routine-service", "joins a queue the step before it drained"),
    ("workflow-service", "same shape as routine-service"),
    (
        "degraded-workflow-component",
        "retries close() on workflow/remote/PAC-actor handles that already failed "
        "to drain at a degraded startup; same shapes as the steps above, and the "
        "list is empty unless startup degraded",
    ),
    ("remote-workflow-registrations", "Zenoh subscriber undeclare; no Python worker join, native transport has no deadline"),
    ("route-registrations", "in-memory unregister; observed 16ms"),
    ("adapters", "signals workers; observed 24ms"),
    (
        "agent-activity",
        "drains an in-memory queue, then at most one conditional UPDATE per actor "
        "seen since the last 1s maintenance tick; each write is bounded only by "
        "the registry's 5s sqlite busy_timeout",
    ),
    ("inbox", "flushes a sqlite handle"),
    ("inbox-store", "closes that handle"),
    ("usage-cache", "stops a daemon thread"),
    (
        "orgfs-runtime",
        "drains only finite blob recoveries; each network query has a 3s timeout, "
        "but total time scales with finite blob size, so there is no honest fixed budget",
    ),
    ("actor-runtime", "pykka stop; its actors are daemon threads"),
    # ⚠️ The one that is not fine. No timeout, and 5.437s in the 2026-08-31
    # shutdown -- the largest unbudgeted contributor, and the reason this file
    # no longer claims a guarantee.
    ("zenoh-transport", "UNBOUNDED, observed 5.437s -- see the note above"),
    (
        "forwarding-sidecar",
        "UNBOUNDED after protocol/terminate deadlines: final killed-process wait has no timeout",
    ),
    ("home-lock", "closes the lock stream"),
)

_EXIT_CODE_STUCK = 75

_RESTORE_THREAD_JOIN_TIMEOUT = 2.0

class _StopRequestTrace:
    """One diagnostic observation, with no stop policy or application reference."""

    def __init__(self) -> None:
        self.last: tuple[str, int | None] | None = None

    def request(
        self, event: threading.Event, source: str, signal_number: int | None = None
    ) -> None:
        # One immutable assignment keeps source/signal coherent. This is the
        # most recently observed request, not a cross-thread causality claim.
        self.last = (source, signal_number)
        event.set()

    def bind(self, event: threading.Event, source: str) -> Callable[[], None]:
        # Keep the same Event identity the old bound event.set callback held;
        # retaining this recorder must not keep a whole application alive.
        return lambda: self.request(event, source)


class _ShutdownMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _finish_shutdown_and_leave(
        self, previous_handlers: Any, *, failed: bool = False
    ) -> tuple[BaseException, ...]:
        """Discharge the last obligations, record who is still here, then go.

        Allen's ruling, 2026-08-30: **hyprial's graceful exit must not be built on
        anyone else's.** Once our own teardown is done, end the process rather
        than returning and letting the interpreter join threads belonging to
        dependencies -- `websockets` refuses daemon threads on purpose so that
        open connections are not "terminated brutally", `anyio`'s selector
        joins without a timeout from an atexit hook, and the HTTP client that
        `mcp` pulls in transitively starts two websocket threads that never set
        `daemon` at all. Only the first is a deliberate tradeoff, but the
        remedy does not depend on which: our departure stops being their
        decision.

        (That third library is named indirectly on purpose. It is retired from
        this codebase's own surface, and the retirement gate scans product
        source for its name as a plain substring -- so naming it here, even to
        explain a shutdown hazard it still creates through `mcp`, would read as
        resurrecting it. `tests/test_exit_independence.py` names it.)

        ⚠️ The exit belongs exactly here and nowhere else. Earlier drops work
        we owe. Later is the region those threads live in, and that region is
        not ours to wait in.

        🔑 `live_thread_summary` is taken **before** leaving, and it is not
        decoration. Once this lands, the backstop stops firing, every visible
        symptom of the leak disappears, and the leak itself is untouched -- we
        merely outrun it. Without this record the next reader would have no way
        to tell "fixed" from "no longer observable", and a fix that deletes the
        observation of whether the problem persists cannot report its own
        relapse. The list getting *shorter* is the success metric; the list
        staying the same means only that we are faster than it now.

        Not exiting is the default, and the steps run either way: an embedded
        daemon owes the same teardown, and only the departure is withheld.
        """

        def record_departure() -> None:
            self._log_trace(
                "info",
                "daemon.exiting",
                nodeId=self.node_id,
                owned=self._owns_process_exit,
                threads=list(live_thread_summary()),
            )

        def flush_records() -> None:
            """Last step, and it has to be last -- `os._exit` drops buffers.

            ⚠️ Added because the dependency was invisible. The departure record
            survives today only because `Logger._append` writes with a raw
            `os.write`, so nothing of ours is sitting in a Python buffer when
            the process ends. That is true, and nothing said it: the steps
            tuple read as though flushing had been considered and found
            unnecessary, when in fact it had been considered *in the docstring*
            and then not expressed here. Whoever adds a buffered writer above
            would be relying on a property this code never claimed.

            So the guarantee becomes explicit rather than inherited. The std
            streams are what a future step is most likely to reach for, and
            flushing them costs nothing on a path that is about to end.
            """

            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except BaseException:  # noqa: BLE001 - never block the exit
                    pass

        return finish_shutdown(
            steps=(
                self._close_home_lease,
                lambda: self._restore_signal_handlers(previous_handlers),
                record_departure,
                flush_records,
            ),
            exit_process=(
                self._exit_process if self._owns_process_exit else do_not_exit
            ),
            failed=failed,
        )

    def _close_home_lease(self) -> None:
        if not self._home_guard.close(5.0):
            raise RuntimeError("home lease authority did not release before deadline")

    def _arm_exit_backstop(self) -> None:
        """Guarantee the process dies once it has finished dying.

        Everything above has run: the lock is released, the socket is gone,
        `daemon.json` is gone. This daemon is over. But CPython will not exit
        until it has joined every non-daemon thread, and a thread parked in a
        `queue.get()` with no sentinel is never coming back -- so the process
        stays alive holding nothing, and its connector fleet, still attached to
        its open pipes, stays alive with it.

        That is not a tidy leak. Those orphans outlive the daemon that wanted
        them, keep serving actors nobody is coordinating, and stall the *next*
        daemon's start on resources they still hold -- which is how one hung
        exit turned into a bus that could not come back without killing 134
        processes by hand (2026-08-29).

        A daemon that has released its identity has no business voting on
        whether the process continues, so this stops asking. `os._exit` skips
        interpreter shutdown entirely, which is exactly the part that hangs.

        The thread is itself a daemon thread, so arming this can never be what
        keeps the process alive; and if a clean exit happens first, the timer
        dies with the interpreter and nothing is forced.

        ⚠️ This is a backstop, not the fix. Threads that cannot be joined are
        still a defect and are still worth repairing one by one. The value of
        this is that it holds for the thread nobody has written yet.
        """

        if not self._owns_process_exit:
            # Embedded in somebody else's interpreter; their exit is theirs.
            return

        def _force_exit() -> None:
            time.sleep(_EXIT_BACKSTOP_SECONDS)
            # Reached only if interpreter shutdown is still parked on a join --
            # the state nobody has managed to observe, because by the time an
            # operator looks the process is gone or the machine was restarted.
            # So the evidence is taken here, on the way out.  Without it every
            # forced exit produces the same line, saying that the process did
            # not leave and never which thread kept it: a year of those logs
            # would not narrow the search by one thread.
            try:
                threads = list(live_thread_summary())
            except BaseException:  # noqa: BLE001 - a diagnostic must not block the exit
                threads = []
            # Named in the record on purpose. A dump nobody can locate is the
            # same as no dump, and this one used to land in the daemon's
            # per-launch stderr capture -- which the restart that follows the
            # stall replaces within seconds.
            #
            # ⚠️ Resolved defensively for the same reason the inventory above
            # is: everything in this function is diagnostics running on the
            # shutdown path, and a diagnostic that raises here would stop the
            # forced exit -- turning the thing that guarantees departure into
            # one more reason to stay. That is not theoretical; an earlier
            # revision resolved this eagerly and a caller without `state_dir`
            # left the backstop silently never firing.
            try:
                dump_path = stall_dump_path(self.state_dir)
            except BaseException:  # noqa: BLE001 - see above
                dump_path = None
            self._log_trace(
                "warn",
                "daemon.exit.forced",
                afterSeconds=_EXIT_BACKSTOP_SECONDS,
                threads=threads,
                stackDump=str(dump_path) if dump_path is not None else None,
                detail=(
                    "shutdown completed but the process did not exit; forcing "
                    "so the connector fleet is not orphaned"
                ),
            )
            dump_live_threads(path=dump_path)
            sys.stderr.flush()
            os._exit(_EXIT_CODE_STUCK)

        threading.Thread(
            target=_force_exit,
            name="daemon-exit-backstop",
            daemon=True,
        ).start()

    def _close(self) -> None:
        """Shut down, saying which resource is being closed and what failed.

        The previous version collected exceptions into a list and surfaced
        them once as ``ExceptionGroup("daemon shutdown failed", errors)`` --
        so an operator learned that two things failed and never which two.
        Worse, that shape cannot report the failure that actually matters
        here: a close that HANGS never raises, so it contributes nothing to
        the list and leaves no trace at all. The log went quiet and the
        process stayed alive, which reads as "still starting" when it is in
        fact "never finished stopping" -- and those two need opposite
        remedies.

        So each step announces itself before running and confirms after.
        A hang is then visible as a `begin` with no matching `end`, which is
        the only way a blocking close can be seen from outside.
        """

        errors: list[BaseException] = []

        # Armed before the first step, because the backstop covers *this*
        # function hanging, and anything armed later would sit behind the very
        # hang it exists to explain.  It complements the per-resource trace
        # below rather than duplicating it: a `begin` with no `end` names the
        # door that did not open, the dump names who is standing behind it.
        #
        self._arm_shutdown_stall_dump_if_owned()

        def attempt(operation: Callable[[], Any], resource: str) -> None:
            self._log_trace("info", "daemon.close.begin", resource=resource)
            started = time.monotonic()
            try:
                operation()
            except BaseException as error:
                errors.append(error)
                self._log_trace(
                    "warn",
                    "daemon.close.failed",
                    resource=resource,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                    elapsedMs=int((time.monotonic() - started) * 1000),
                )
            else:
                self._log_trace(
                    "info",
                    "daemon.close.end",
                    resource=resource,
                    elapsedMs=int((time.monotonic() - started) * 1000),
                )

        # From here on a missing user transport is teardown, not configuration:
        # workflow alerts drained during shutdown retry after restart.
        self._user_delivery_settled = False
        def close_autoupdate() -> None:
            if not self._autoupdate.stop(
                timeout=dict(_CLOSE_STEP_BUDGETS)["autoupdate"]
            ):
                stage = getattr(self._autoupdate, "stop_incomplete_stage", None)
                raise RuntimeError(
                    f"auto-update authority did not drain: {stage or 'unknown'}"
                )

        attempt(close_autoupdate, "autoupdate")
        # Before any collaborator the restore thread might be inside is torn
        # down.  Bounded on purpose -- see the join method's docstring.
        attempt(self._join_restore_thread, "restore-thread")

        if self._server is not None:
            server = self._server
            self._server = None
            attempt(server.close, "ipc-server")
        if self._accept_reserve_fd is not None:
            reserve_fd = self._accept_reserve_fd
            self._accept_reserve_fd = None
            attempt(lambda: os.close(reserve_fd), "reserve-fd")
        # A socket close cannot interrupt a handler already inside a domain
        # wait. If any worker remains, keep every domain it may still touch
        # alive and let a later close retry after the worker settles.
        try:
            self._close_ipc_clients()
        except BaseException as error:
            self._log_trace(
                "warn", "daemon.close.failed", resource="ipc-clients",
                errorType=type(error).__name__, error=str(error)[:500],
            )
            raise
        if self._ipc_request_owner is not None:
            request_owner = self._ipc_request_owner
            if not request_owner.close(5.0):
                raise RuntimeError(
                    "IPC requests did not settle before domain teardown"
                )
            self._ipc_request_owner = None
        if self._service_manager is not None:
            service_timeout = dict(_CLOSE_STEP_BUDGETS)["service-connect"]
            if not self._close_service_connect(service_timeout):
                raise RuntimeError(
                    "service-connect authority did not drain before shared dependencies"
                )
        # GC may be inside route/routine/Agent destruction. Drain it before
        # any of those dependencies, including a collector retained by startup
        # degradation. Other degraded component close semantics are unchanged.
        if self._pac_gc is not None:
            pac_gc = self._pac_gc
            if not pac_gc.close(5.0):
                raise RuntimeError("PAC GC service did not stop before deadline")
            self._pac_gc = None
        from hyprial.daemon.impl.pac.actors.gc  import PacGc
        for collector in tuple(self._degraded_workflow_handles):
            if isinstance(collector, PacGc):
                if not collector.close(5.0):
                    raise RuntimeError("degraded PAC GC service did not stop before deadline")
                self._degraded_workflow_handles.remove(collector)
        if self._dispatch_diagnostics is not None:
            diagnostics = self._dispatch_diagnostics
            if not diagnostics.close(5.0):
                raise RuntimeError("dispatch diagnostics did not drain")
            self._dispatch_diagnostics = None
        attempt(lambda: self.socket_path.unlink(missing_ok=True), "socket-file")
        attempt(lambda: (self.state_dir / "daemon.json").unlink(missing_ok=True), "daemon-json")
        self._maintenance_generation += 1
        if not self._maintenance_scheduler.shutdown(5.0):
            raise RuntimeError(
                "maintenance callback did not stop; refusing unsafe domain teardown"
            )
        if getattr(self, "_stop_recipient_wake_observer", None) is not None:
            stop_recipient_wake_observer = self._stop_recipient_wake_observer
            self._stop_recipient_wake_observer = None
            attempt(stop_recipient_wake_observer, "recipient-wake-observer")
        if self._dispatch_cadence is not None:
            cadence = self._dispatch_cadence
            if not cadence.close(5.0):
                raise RuntimeError(
                    "dispatch runtime did not drain; refusing unsafe domain teardown"
                )
            self._dispatch_cadence = None
        if self._forwarding_cadence is not None:
            cadence = self._forwarding_cadence
            if not cadence.close(5.0):
                raise RuntimeError(
                    "forwarding effects did not drain; refusing unsafe sidecar teardown"
                )
            self._forwarding_cadence = None
        if self._restore_wake_cadence is not None:
            cadence = self._restore_wake_cadence
            if not cadence.close(5.0):
                raise RuntimeError("restore wake effects did not drain")
            self._restore_wake_cadence = None
        if self._session_route_coordinator is not None:
            coordinator = self._session_route_coordinator
            if not coordinator.close(5.0):
                raise RuntimeError(
                    "session route effects did not drain; refusing unsafe route teardown"
                )
            self._session_route_coordinator = None
        if self._runtime is not None:
            runtime = self._runtime
            try:
                if runtime.stop() is False:
                    raise RuntimeError("dispatch runtime did not drain")
            except BaseException as error:
                raise RuntimeError(
                    "dispatch runtime did not drain; refusing unsafe domain teardown"
                ) from error
            self._runtime = None
        for attribute in ("_quota_watchdog", "_inbox_watchdog"):
            watchdog = getattr(self, attribute)
            if watchdog is not None:
                if not watchdog.close(5.0):
                    raise RuntimeError(
                        f"{attribute[1:]} did not drain before delivery teardown"
                    )
                setattr(self, attribute, None)
        if self._operator_alarm_delivery is not None:
            operator_alarms = self._operator_alarm_delivery
            self._operator_alarm_delivery = None

            def close_operator_alarms() -> None:
                if not operator_alarms.close(
                    dict(_CLOSE_STEP_BUDGETS)["operator-alarms"]
                ):
                    raise RuntimeError("operator alarm delivery did not drain")

            attempt(close_operator_alarms, "operator-alarms")
        attempt(self._flush_agent_activity, "agent-activity")
        if self._routine_service is not None:
            if self._routine_coordinator is not None:
                coordinator = self._routine_coordinator
                if not coordinator.close(5.0):
                    raise RuntimeError("routine coordinator did not drain")
                self._routine_coordinator = None
            routine_service = self._routine_service
            self._routine_service = None
            attempt(routine_service.close, "routine-service")
        if self._remote_workflow is not None:
            remote_workflow = self._remote_workflow
            remote_workflow.close_registrations()
            if not remote_workflow.shutdown(5.0):
                raise RuntimeError("remote workflow handlers did not drain before deadline")
            self._remote_workflow = None
        if self._workflow_service is not None:
            workflow_service = self._workflow_service
            workflow_service.close()
            self._workflow_service = None
        for handle in self._degraded_workflow_handles:
            attempt(handle.close, "degraded-workflow-component")
        self._degraded_workflow_handles.clear()
        if self._pac_actor_service is not None:
            pac_actor_service = self._pac_actor_service
            if not pac_actor_service.close(5.0):
                raise RuntimeError("PAC actor service did not drain before deadline")
            self._pac_actor_service = None
        if self._pac_graph_authority is not None:
            graph_authority = self._pac_graph_authority
            if not graph_authority.close(5.0):
                raise RuntimeError(
                    "PAC graph authority did not drain before sender teardown"
                )
            self._pac_graph_authority = None
        if self._lifecycle_manager is not None:
            lifecycle_manager = self._lifecycle_manager
            if not lifecycle_manager.drain(5.0):
                raise RuntimeError("lifecycle manager did not drain before deadline")
            self._lifecycle_manager = None
        for lifecycle_port in self._lifecycle_domain_ports:
            if not lifecycle_port.drain(5.0):
                errors.append(
                    RuntimeError(
                        f"{lifecycle_port.domain} lifecycle port did not drain"
                    )
                )
        self._lifecycle_domain_ports = ()
        if self._route_registration is not None:
            route_registration = self._route_registration
            route_registration.close_registrations()
            if not route_registration.drain(5.0):
                raise RuntimeError("route registration did not drain before deadline")
            self._route_registration = None
            self._routes = None
        if self._lifecycle_router is not None:
            lifecycle_router = self._lifecycle_router
            self._lifecycle_router = None
            lifecycle_router.close()
        if not self._agent_keep.close(5.0):
            raise RuntimeError("agent keep authority did not drain")
        if not self._restore_policy.close(5.0):
            raise RuntimeError("restore policy authority did not drain")
        self._restore_policy_finalizer.detach()
        if not self._blocking_failures.close(5.0):
            raise RuntimeError("blocking failure authority did not drain")
        self._blocking_failures_finalizer.detach()
        self._close_harnesses_before_agent_domains()
        with self._registry_management_lock:
            management = self._registry_management
            self._registry_management = None
        if management is not None and not management.close(5.0):
            raise RuntimeError("management authority did not drain")
        if self._hook_bus is not None:
            hook_bus = self._hook_bus
            self._hook_bus = None
            self._daemon_hooks = None

            def close_hook_bus() -> None:
                timeout = dict(_CLOSE_STEP_BUDGETS)["hook-bus"]
                if not hook_bus.close(timeout):
                    raise RuntimeError("hook bus did not drain")

            attempt(close_hook_bus, "hook-bus")
        if self._turn_hooks is not None:
            turn_hooks = self._turn_hooks
            self._turn_hooks = None

            def close_turn_hooks() -> None:
                if not turn_hooks.close(_TURN_HOOK_CLOSE_TIMEOUT):
                    raise RuntimeError("turn-hook recap writer did not drain")

            attempt(close_turn_hooks, "turn-hooks")
        if self._adapters is not None:
            adapters = self._adapters
            self._adapters = None
            attempt(adapters.stop, "adapters")
        self._lark_client = None
        if self._lark_events is not None:
            self._lark_events.close()
            self._lark_events = None
        if getattr(self, "_recipient_wakes", None) is not None:
            recipient_wakes = self._recipient_wakes
            if not recipient_wakes.close(5.0):
                raise RuntimeError(
                    "recipient wakes did not settle before inbox teardown"
                )
            self._recipient_wakes = None
        if self._inbox is not None:
            inbox = self._inbox
            self._inbox = None
            shutdown_inbox = getattr(inbox, "shutdown", None)
            if callable(shutdown_inbox):
                attempt(shutdown_inbox, "inbox")
            else:
                # Isolated legacy test fixtures can still inject the old
                # state engine directly. Production composition never does.
                inbox_type = type(inbox)
                if (
                    inbox_type.__module__ == "hyprial.daemon.impl.inbox.service"
                    and inbox_type.__name__ == "InboxService"
                ):
                    attempt(lambda: inbox_type.close(inbox), "inbox-store")
        self._inbox_coordinator = None
        if self._usage_cache is not None:
            usage_cache = self._usage_cache
            self._usage_cache = None
            if usage_cache.stop(timeout=2.0) is False:
                errors.append(RuntimeError("usage authority did not drain"))
        if self._orgfs_runtime is not None:
            orgfs_runtime = self._orgfs_runtime
            self._orgfs_runtime = None
            self._org_context_bridge = None
            attempt(orgfs_runtime.close, "orgfs-runtime")
        for resource_name in (
            "_actor_token",
            "_duplicate_watch",
            "_org_endpoint",
            "_user_endpoint",
            "_status_endpoint",
            "_endpoint",
            "_directory",
        ):
            resource = getattr(self, resource_name)
            if resource is not None:
                if resource_name == "_directory":
                    attempt(resource.close, "directory")
                else:
                    resource.close()
                setattr(self, resource_name, None)
        if not self.user_adapters.close(5.0):
            raise RuntimeError("user adapter gateways did not drain")
        if not self._route_gateway_cache.close(5.0):
            raise RuntimeError("outbound route gateways did not drain")
        for gateway_owner in tuple(self._outbound_gateway_owners):
            if not gateway_owner.close(5.0):
                raise RuntimeError("outbound Lark gateway did not drain")
        self._outbound_gateway_owners.clear()
        self._presence = None
        self._user_delivery = None
        if self._fetch_receipt_publisher is not None:
            publisher = self._fetch_receipt_publisher
            if not publisher.close(5.0):
                raise RuntimeError(
                    "fetch receipt hints did not drain before transport teardown"
                )
            self._fetch_receipt_publisher = None
        if self._transport is not None:
            transport = self._transport
            transport.close()
            self._transport = None
        if self._forwarding_supervisor is not None:
            supervisor = self._forwarding_supervisor
            supervisor.close()
            self._forwarding_supervisor = None
        if self._forwarding_discovery is not None:
            forwarding_discovery = self._forwarding_discovery
            self._forwarding_discovery = None
            attempt(forwarding_discovery.close, "forwarding-sidecar")
        if self._lock_stream is not None:
            lock_stream = self._lock_stream
            self._lock_stream = None
            attempt(lock_stream.close, "home-lock")
        if not self.user_profiles.close(5.0):
            errors.append(RuntimeError("user profile authority did not drain"))
        if not self._state_persistence.close(5.0):
            errors.append(RuntimeError("state persistence authority did not drain"))
        if not self._maintenance_watchdog.close(timeout=5.0):
            raise RuntimeError("maintenance watchdog still owns reporting work")
        if not self._gateway_logger.close(timeout=5.0):
            errors.append(RuntimeError("gateway log writer did not drain"))
        if not self._logger.close(timeout=5.0):
            errors.append(RuntimeError("daemon log writer did not drain"))
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("daemon shutdown failed", errors)

    def _close_harnesses_before_agent_domains(self) -> None:
        """Retire launch users before their Agent settlement dependency."""

        if self._harnesses is not None:
            harnesses = self._harnesses
            stop_harnesses = getattr(harnesses, "stop", None)
            if callable(stop_harnesses):
                stop_harnesses()
            if getattr(harnesses, "drain_complete", True) is False:
                raise RuntimeError("harness domain did not drain before deadline")
            self._harnesses = None
        provider_auth = getattr(self, "_provider_auth", None)
        if provider_auth is not None:
            if not provider_auth.close(5.0):
                raise RuntimeError("provider auth authority did not drain")
            self._provider_auth = None
        if self._agent_session_domains is not None:
            agent_session_domains = self._agent_session_domains
            if not agent_session_domains.close():
                raise RuntimeError(
                    "session/agent domains did not drain before deadline"
                )
            self._agent_domains_finalizer.detach()

    def _trace_presence_announcement(self) -> None:
        if not getattr(self, "_trace_presence_enabled", False):
            return
        try:
            self._log(
                "info", "zenoh", "zenoh.presence.announced", nodeId=self.node_id,
                key=KeySpace().actor_liveliness(self.node_id),
            )
        except Exception:
            pass

    def _trace_presence_observation(self, event: Any) -> None:
        # The existing logger admits bytes without waiting for file I/O.
        self._log(
            "info", "zenoh", "zenoh.presence.observed",
            nodeId=self.node_id, key=event.key, kind=event.kind,
            change=event.change, sampleGeneration=event.sample_generation,
            presenceGeneration=event.presence_generation,
            transportGeneration=event.transport_generation,
            callbackHistoryComplete=event.history_complete,
            admission=event.admission,
        )

    def _trace_presence_snapshot(self, hosts: list[str] | None) -> None:
        if not getattr(self, "_trace_presence_enabled", False):
            return
        try:
            transport = self._transport.projection()
            presence = self._presence.inner.presence_projection()
            presence_hosts = sorted(
                actor for actor in presence.actors
                if classify_target_identity(actor) == TARGET_KIND_HOST
            )
            self._log(
                "info", "zenoh", "zenoh.presence.snapshot", nodeId=self.node_id,
                hosts=[host[:512] for host in hosts[:32]] if hosts is not None else None,
                hostsTruncated=len(hosts) > 32 if hosts is not None else False,
                transportGeneration=transport.generation,
                callbackHistoryComplete=transport.callbacks_complete,
                pending=transport.pending, registrations=transport.registrations,
                rejected=transport.rejected, staleCallbacks=transport.stale_callbacks,
                presenceGeneration=presence.generation,
                presenceComplete=presence.complete,
                presenceRejected=presence.rejected,
                presenceActors=[actor[:512] for actor in sorted(presence.actors)[:32]],
                presenceActorsTruncated=len(presence.actors) > 32,
                presenceHosts=[host[:512] for host in presence_hosts[:32]],
                presenceHostsTruncated=len(presence_hosts) > 32,
            )
        except Exception:
            # Diagnostics are best effort; they must not change hosts output.
            pass

    def _request_stop(self, source: str, signal_number: int | None = None) -> None:
        trace = getattr(self, "_stop_request_trace", None)
        if trace is None:
            self.stop_event.set()
        else:
            trace.request(self.stop_event, source, signal_number)

    def _install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}

        def stop(_signum: int, _frame: FrameType | None) -> None:
            self._request_stop("signal", _signum)

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, stop)
        return previous

    @staticmethod
    def _restore_signal_handlers(previous: dict[int, Any]) -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    def _owner_alert_notifier(self, text: str, *, idempotency_key: str):
        """The owner-DM channel for alerts (autoupdate.alert.notify_owner)."""

        from hyprial.daemon.impl.autoupdate.alert import notify_owner

        return notify_owner(
            hyprial_home=self.hyprial_home,
            state_dir=self.state_dir,
            text=text,
            idempotency_key=idempotency_key,
        )

    def _owner_requester_addresses(self) -> frozenset[str]:
        """Addresses whose fail-loud notices already reach this owner.

        A notice diverted away from a person's chat is handed to the owner
        instead.  When the waiting sender IS the owner -- their squire adapter,
        one of its routes, or their ``user:`` address -- the hand-off puts the
        same machine text in front of the same person, one wrapper deeper, so
        the runtime logs those instead (observed 2026-09-26).

        Degrades to the ``user:`` addresses when the profile or its gateway
        cannot be read; the degradation is logged, not silent.
        """

        addresses: set[str] = set()
        try:
            addresses.add(canonical_user_uri(self.owner))
            profile = self.user_profiles.resolve(self.owner)
            if profile is None:
                return frozenset(addresses)
            addresses.add(canonical_user_uri(profile.owner_key))
            adapter_names: set[str] = set()
            squire_adapter = profile.squire_adapter
            if squire_adapter:
                parsed = parse_channel_uri(squire_adapter)
                adapter_names.add(
                    parsed[2] if parsed is not None else squire_adapter
                )
            if profile.delivery_agent is not None:
                agent = self.agents.get(profile.delivery_agent)
                agent_uri = (
                    agent.uri
                    if agent is not None
                    else canonical_agent_uri(
                        self.owner, self.node_id, profile.delivery_agent
                    )
                )
                addresses.add(agent_uri)
                adapter_names.update(
                    adapter
                    for adapter, pinned in self._adapter_pins().items()
                    if pinned == agent_uri
                )
            if not adapter_names:
                return frozenset(addresses)
            for gateway in self.load_persistent_configuration().channels.gateways:
                if gateway.name not in adapter_names:
                    continue
                # The gateway model is Lark-only, so this is the same
                # ``adapter:lark:<name>`` shape an inbound notice carries.
                addresses.add(f"{ADAPTER_URI_PREFIX}lark:{gateway.name}")
                addresses.update(
                    f"{ROUTE_URI_PREFIX}{gateway.name}:{route.name}"
                    for route in gateway.routes
                )
        except Exception as error:  # noqa: BLE001 -- degraded, and it says so
            self._log(
                "warn",
                "daemon",
                "availability_loud.owner_addresses_degraded",
                detail=str(error) or type(error).__name__,
            )
        return frozenset(addresses)

    def _report_legacy_agent_home(self, actor: str, reason: str) -> None:
        """Emit the legacy fallback once for each actor in this daemon epoch."""

        with self._legacy_agent_home_log_lock:
            if actor in self._legacy_agent_home_logged:
                return
            self._legacy_agent_home_logged.add(actor)
        self._log(
            "warn",
            "daemon",
            "agent.home.legacy_path",
            actor=actor,
            reason=reason,
        )
