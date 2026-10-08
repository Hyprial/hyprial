"""Daemon startup sequence: run(), startup phases, ownership lock, pid file, log migration and duplicate-instance detection."""

from __future__ import annotations

from __future__ import annotations
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TYPE_CHECKING
from hyprial.daemon.impl.adapters.lark.outbound.gateway import GatewayIoAuthority
from hyprial.kernel import DaemonStartupPhase
from hyprial.kernel import migrate_pre_trajectory_logs
from hyprial.kernel import PersistentConfiguration
from hyprial.daemon.impl.transport import (
    zenoh_environment_flag,
)
from hyprial.daemon.impl.lifecycle.duplicate_watch  import DUPLICATE_INSTANCE_EVENT
from hyprial.daemon.impl.processes.shutdown_stall  import (
    STALL_DUMP_SIGNAL,
    arm_shutdown_stall_dump,
    register_stall_signal,
    stall_dump_path,
)
from hyprial.daemon.impl.configuration.ownership  import DaemonOwnershipBusy, DaemonStateOwnershipFence
from hyprial.kernel import (
    parse_channel_uri,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.netendpoints.endpoints import (
    _lock_wait_timeout,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
)


def _mirror_startup_event_to_stderr(event: str, **fields: Any) -> None:
    """Mirror one startup failure event to stderr as a single-line JSON envelope.

    The daemon's structured events go to ``logs/daemon.jsonl`` -- a file the
    CLI's failure-report path does not read. What ``hyprial init`` summarizes on a
    failed start is the launch capture: the daemon child's stdout+stderr,
    owned by this launch behind a marker. An event that never reaches stderr
    is an event the failure report cannot name, which is how a daemon that
    died binding its IPC socket reported ``daemonEvents: []``.

    Two rules keep this channel safe:

    * Fields carry only values the daemon authors itself (event names, phase
      names, counts) -- never exception text or anything derived from
      external input. The structured log redacts; stderr does not, and the
      summary reader must not become a channel for bytes a failure dragged
      in. (The summary keeps only event names regardless; this rule is what
      keeps the raw launch log clean for the human who opens it next.)
    * Called from exactly two startup branches: a ``step()`` failure in
      ``run()`` and a degraded restore in ``_restore_harnesses``. A daemon
      that talks on stderr on its normal path turns the launch log into a
      noise source; ``tests/test_startup_event_reconciliation.py`` pins the
      call sites.

    Never raises: this runs on failure paths, and a broken mirror must not
    mask the failure it describes.
    """

    try:
        print(
            json.dumps({"event": event, **fields}, separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )
    except Exception:  # noqa: BLE001 - the mirror must never mask its failure
        pass

class _ContextBoundProviderAuthRunner:
    """Hold Agent home custody for one complete model-vendor auth invocation."""

    def __init__(
        self,
        context: Any,
        runner: Callable[..., Any],
        custody: Callable[[Any], Any],
    ) -> None:
        self._context = context
        self._runner = runner
        self._custody = custody

    def __call__(
        self,
        model_vendor: str,
        on_device_code: Callable[[Any], None],
        *,
        stop: threading.Event | None = None,
    ) -> Any:
        with self._custody(self._context):
            return self._runner(model_vendor, on_device_code, stop=stop)


class _StartupMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _on_startup_duplicate_detected(self, detail: dict[str, Any]) -> None:
        """Home guard's copied-home verdict: log it and keep it for ps/doctor."""

        self._startup_duplicate = dict(detail)
        self._log("error", "daemon", DUPLICATE_INSTANCE_EVENT, **detail)

    def _on_duplicate_check_failed(self, detail: str) -> None:
        """The background copied-home check died: warn, never silent."""

        self._log("warn", "daemon", "daemon.identity.duplicate_check_failed", detail=detail)

    def _duplicate_instance_payload(self) -> JsonObject:
        """Duplicate-instance verdict for ps/doctor, from both detectors.

        Two sources: the mesh watch (foreign generations of this node
        identity seen on liveliness) and the home guard's pre-claim
        copied-home detection.  The mesh half only sees peers that declare
        a generation liveliness token -- a pre-generation duplicate is
        invisible to it and only the startup detection (same machine)
        covers that case.  That limit ships in the payload so no reader
        can mistake this for whole-mesh coverage.
        """

        mesh = (
            self._duplicate_watch.status_payload()
            if self._duplicate_watch is not None
            else {"active": False, "meshPeerGenerations": []}
        )
        return {
            "active": bool(mesh["active"]) or self._startup_duplicate is not None,
            "meshPeerGenerations": mesh["meshPeerGenerations"],
            "startupRecord": self._startup_duplicate,
            "meshDetectionCoverage": (
                "only peers that declare a generation liveliness token; "
                "a pre-generation duplicate is invisible to mesh detection "
                "and is covered only by startup copied-home detection on "
                "the same machine"
            ),
        }

    def run(
        self,
        *,
        ownership_stream: Any | None = None,
        identity_transaction_stream: Any | None = None,
    ) -> None:
        if ownership_stream is not None:
            if self._lock_stream is not None:
                raise RuntimeError("daemon state ownership is already installed")
            self._lock_stream = ownership_stream
        previous_handlers: dict[int, Any] = {}
        try:
            self._home_guard.claim()
        except BaseException:
            # ``daemon_run`` transfers the already-held ownership descriptor
            # before the home guard is claimed.  A guard failure must release
            # that descriptor, but must not run the broad partial-runtime
            # shutdown path used after startup has begun.
            if ownership_stream is not None and self._lock_stream is not None:
                self._lock_stream.close()
                self._lock_stream = None
            raise
        # Startup announces each phase for the same reason shutdown does, and
        # for a sharper one: everything between resolving endpoints and
        # `daemon.ready` used to emit nothing at all. A daemon stuck in here
        # was indistinguishable from a daemon doing nothing -- 66 seconds of
        # silence with no way to tell which step owned them, while the caller
        # gave up on a ready timeout and reported a failure naming no cause.
        # A `begin` with no `end` names the step; the elapsed time on each
        # `end` is what turns "slow startup" into a number that can be
        # compared against that timeout.
        def step(operation: Callable[[], Any], phase: DaemonStartupPhase) -> Any:
            try:
                return self._startup_step(operation, phase)
            except BaseException:
                # The launch summary reads the launch capture (stderr), not
                # daemon.jsonl -- mirror the failure name there or a failed
                # `hyprial init` reports `daemonEvents: []`.
                _mirror_startup_event_to_stderr(
                    "daemon.start.failed", phase=phase.value
                )
                raise

        run_failed = False
        failure_type: str | None = None
        failure_site: str | None = None
        try:
            # ⚠️ Nothing above `_migrate_logs_at_startup` may write a log line.
            # Migration decides what to archive by looking at what this home
            # already contains, so a phase event emitted first would be a log
            # this startup created being treated as one it inherited -- which
            # is exactly what `test_empty_home_is_marked_before_new_contract_
            # logs_are_created` exists to catch. These two steps are also the
            # cheap ones; the silence this instrumentation was added for was
            # never here.
            if self._lock_stream is None:
                self._acquire_lock()
            previous_handlers = self._install_signal_handlers()
            # Armed here rather than at teardown: the point of the signal path
            # is that it answers at *any* moment, including while the daemon is
            # perfectly healthy but not responding to something else.
            self._register_stall_signal_if_owned()
            try:
                self._migrate_logs_at_startup()
            except Exception as error:  # noqa: BLE001 - startup must continue
                self._warn_log_migration_failure(error)
            # ⛔ Emitted HERE, not where the migration runs.  The owner rewrite
            # happens in __init__, long before the logger exists, and the line
            # above is the boundary this file states in the comment overhead:
            # nothing may write a log line until `_migrate_logs_at_startup`
            # has decided what this home inherited.
            #
            # ⚠️ The first version of this emit sat next to the logger's
            # construction and fired only when `rewrites > 0`.  That passed CI
            # for the wrong reason -- no test migrates anything, so the line
            # was never written -- while on a real migrating node it would
            # have written a pre-archival log every time.  A branch that only
            # runs in production is only tested in production.
            #
            # Unconditional, including the zero: firing only on a non-zero
            # count restores the "ran, found nothing" / "never ran" ambiguity
            # this count exists to remove, and the zero is precisely the
            # reading that says a node is already migrated.
            self._log(
                "info",
                "daemon",
                "identity.owner_migration.applied",
                owner=self.owner,
                rewrittenCells=self._owner_migration_rewrites,
            )
            self._warn_if_worker_proxy_absent()
            step(self._start_runtime, DaemonStartupPhase.ACTOR_RUNTIME)
            # Activate only when a protected record or persisted intent exists.
            # Otherwise the first service RPC performs the same composition;
            # an unconfigured daemon owns no service threads or polling work.
            step(self._start_service_connect, DaemonStartupPhase.SERVICE_CONNECT)
            step(self._start_server, DaemonStartupPhase.IPC_SERVER)
            # daemon.json now means "serving", not "restored": it is written
            # at the phase-① boundary -- socket bound, accept about to run,
            # ping answerable -- so the CLI can key its readiness probe on it
            # without waiting for restore.  Everyone reading it as "restore
            # complete" was already wrong once: the CLI's 0.5s `ps` probes
            # filled the backlog because accept had not started, marker or no.
            step(self._write_pid_file, DaemonStartupPhase.PID_FILE)
            if self._usage_cache is not None:
                step(self._usage_cache.start, DaemonStartupPhase.USAGE_CACHE)
            step(self._autoupdate.start, DaemonStartupPhase.AUTOUPDATE)
            # Restore leaves the startup path here.  Adapter workers receive
            # this socket path and may use it as soon as their native stream
            # becomes ready (including history replay), so the socket must
            # exist while they restore -- and now answers them: the accept
            # loop runs on the main thread while restore finishes in the
            # background, light methods (ping/shutdown) are served throughout,
            # and heavy methods get an immediate DAEMON_RESTORING rather than
            # parking in the listen backlog until accept starts.
            #
            # Shape (b) of the two candidates: restore goes to a thread, the
            # main thread enters `_serve`.  Signal handling and the exit path
            # keep their main-thread assumptions; the thread's wait for the
            # fleet is bounded by the per-start settlement timers the actor
            # arms at admission (a reconcile strategy parameter), and restore
            # progress is observed through the phase-③ readiness reports,
            # never through daemon.json.
            self._start_restore_thread(step)
            # The inherited identity-transaction descriptor is the daemon's
            # startup authorization.  Release it only once this generation is
            # ready to accept ping/shutdown; the parent keeps its duplicate
            # through post-launch verification.  Direct ``daemon run`` uses
            # this same path, so it cannot race a login stop/commit window.
            if identity_transaction_stream is not None:
                identity_transaction_stream.close()
                identity_transaction_stream = None
            self._serve()
            # A restore step that escapes its own recovery must still fail the
            # daemon, as it did when restore ran inline -- just through the
            # stop path now that the accept loop owns the main thread.
            if self._restore_error is not None:
                raise self._restore_error
        except BaseException as error:
            # Cleanup success must not erase the failure that sent us here.
            # Production leaves from the finally block below via os._exit, so
            # the exception itself never reaches the parent process; carry its
            # existence into the status chosen after every shutdown step runs.
            run_failed = True
            failure_type = type(error).__name__
            frame = error.__traceback__
            for _ in range(32):
                if frame is None:
                    break
                code = frame.tb_frame.f_code
                failure_site = f"{Path(code.co_filename).name}:{frame.tb_lineno}:{code.co_name}"
                frame = frame.tb_next
            # Do not retain traceback frames or log exception values/locals.
            frame = None
            raise
        finally:
            if identity_transaction_stream is not None:
                identity_transaction_stream.close()
                identity_transaction_stream = None
            try:
                trace = getattr(self, "_stop_request_trace", None)
                observed = trace.last if trace is not None else None
                lease_status = getattr(getattr(self, "_home_guard", None), "status", None)
                try:
                    lease_observation = lease_status() if callable(lease_status) else None
                except Exception:
                    # A diagnostic must not replace shutdown or its failure.
                    lease_observation = None
                self._log(
                    "info", "daemon", "daemon.stopping", nodeId=self.node_id,
                    stopRequestSource=observed[0] if observed else "unrecorded",
                    stopSignal=observed[1] if observed else None,
                    runFailed=run_failed, failureType=failure_type,
                    failureSite=failure_site,
                    homeLease=lease_observation,
                )
                self._close()
            finally:
                # Armed *before* the closing steps rather than after them.
                # After, a hang inside `_home_guard.close` would leave nothing
                # armed at all -- the guarantee would be missing in exactly the
                # case it exists for. It costs nothing when the steps finish,
                # because the process is gone before its timer matures.
                self._arm_exit_backstop()
                shutdown_errors = self._finish_shutdown_and_leave(
                    previous_handlers, failed=run_failed
                )
                # Reached only when this daemon does not own the process; in
                # production `_finish_shutdown_and_leave` does not return.
                # ⚠️ Raised rather than swallowed so a failed teardown step is
                # not something the suite can pass through in silence.
                if shutdown_errors:
                    raise BaseExceptionGroup(
                        "daemon shutdown steps failed", list(shutdown_errors)
                    )

    def _startup_step(
        self, operation: Callable[[], Any], phase: DaemonStartupPhase
    ) -> Any:
        """Run one bounded startup phase through the shared event contract."""

        if not isinstance(phase, DaemonStartupPhase):
            raise TypeError("daemon startup phases must use DaemonStartupPhase")
        phase_name = phase.value
        self._log_trace("info", "daemon.start.begin", phase=phase_name)
        started = time.monotonic()
        try:
            result = operation()
        except BaseException as error:
            self._log_trace(
                "warn",
                "daemon.start.failed",
                phase=phase_name,
                errorType=type(error).__name__,
                error=str(error)[:500],
                elapsedMs=int((time.monotonic() - started) * 1000),
            )
            raise
        self._log_trace(
            "info",
            "daemon.start.end",
            phase=phase_name,
            elapsedMs=int((time.monotonic() - started) * 1000),
        )
        return result

    def owns_process_exit(self) -> None:
        """Declare that this daemon *is* the process, so it may force the exit.

        Off by default, and the default is the safe one. A DaemonApplication
        does not always own the interpreter it runs in -- the test suite starts
        real daemons in-process, and forcing an exit there kills the host, not
        a daemon. That is not hypothetical: arming this unconditionally took
        down a full pytest run at 28% with the backstop's own exit code.

        So only the entry point that spawned an interpreter *to be* a daemon
        turns it on: `hyprial daemon run`.
        """

        self._owns_process_exit = True

    def _arm_shutdown_stall_dump_if_owned(self) -> bool:
        """Arm the stall watchdog, but only when this process is ours to watch.

        Arming a process-wide timer is a process-level act, exactly like ending
        the process, so it answers to the same question and must not grow a
        second answer to it. `_arm_exit_backstop` already asks
        `_owns_process_exit`; two independent readings of "do we own this
        interpreter" would drift, and the day they disagree nothing reports it.

        ⚠️ Not a precaution -- the ungated version did damage. A full suite
        armed this from an in-process `_close()`, and thirty seconds later it
        printed seventy-nine thread stacks into an unrelated end-to-end test
        that was waiting on a thirty-second subprocess budget. The diagnostic
        became the disturbance, in a process it had no business watching.
        """

        if not self._owns_process_exit:
            return False
        return arm_shutdown_stall_dump()

    def _register_stall_signal_if_owned(self) -> object | None:
        """Wire the on-demand stack dump, but only in a process that is ours.

        Third of the three process-level acts behind one predicate, with the
        arming watchdog and the forced exit. ⚠️ This one needs the gate most,
        and it is the one that would have been easiest to leave ungated,
        because its damage is the only kind that produces no failure:

            forcing an exit      kills the host      -> the suite stops dead
            dumping to stderr    floods the host     -> a test goes red
            claiming a signal    silently replaces the host's own handler
                                 -> nothing fails, and one day somebody's
                                    program stops responding to SIGUSR1

        The first two announce themselves. The third is only discovered by
        whoever eventually depended on the behaviour we took away.
        """

        if not self._owns_process_exit:
            return None
        handle = register_stall_signal(path=stall_dump_path(self.state_dir))
        self._stall_signal_handle = handle
        if handle is not None:
            self._log_trace(
                "info",
                "daemon.stall.dump.armed",
                signal=STALL_DUMP_SIGNAL,
                stackDump=str(stall_dump_path(self.state_dir)),
                detail=(
                    "send this signal to have the daemon write every thread's "
                    "stack to the file above; no privileges and no restart"
                ),
            )
        return handle

    def _migrate_logs_at_startup(self) -> None:
        result = migrate_pre_trajectory_logs(self.state_dir)
        if result is None:
            return
        self._log(
            "info",
            "daemon",
            "logs.migrated",
            fileCount=result.file_count,
        )

    def _warn_if_worker_proxy_absent(self) -> None:
        """One warning when model-vendor workers will connect directly.

        The 2026-09-25 incident was a restart from a shell with no usable
        proxy: nothing said so, and it took a 40-minute hung worker to find
        out.  This is the cheap half of noticing -- a fact about the
        configuration read at startup, with no network probe.  A damaged
        setting is reported too, because every worker launch will now fail
        on it.
        """

        from hyprial.identity import (
            WORKER_PROXY_SETTINGS_KEY,
            WorkerProxyError,
            ambient_proxy_absent,
            read_worker_proxy,
        )

        try:
            setting = read_worker_proxy(self.hyprial_home)
        except WorkerProxyError as error:
            self._log(
                "warn",
                "daemon",
                "daemon.proxy.settings_invalid",
                code=error.code,
                detail=str(error),
            )
            return
        if ambient_proxy_absent(setting, os.environ):
            self._log(
                "warn",
                "daemon",
                "daemon.proxy.absent",
                detail=(
                    f"no {WORKER_PROXY_SETTINGS_KEY} setting and no "
                    "HTTP(S)_PROXY/ALL_PROXY in the daemon environment: "
                    "model-vendor workers will connect directly"
                ),
            )

    def _worker_proxy_status_json(self) -> dict[str, object]:
        """``workerProxy`` for status/ps: what the NEXT worker launch uses.

        Read at request time, like the launch itself, so it never shows a
        value the daemon cached at startup.  A damaged setting is reported,
        not raised: ``ps`` must stay answerable while it is broken.
        """

        from hyprial.identity import WorkerProxyError, read_worker_proxy

        try:
            setting = read_worker_proxy(self.hyprial_home)
        except WorkerProxyError as error:
            return {"configured": False, "error": error.code, "detail": str(error)}
        if setting is None:
            return {"configured": False}
        return {"configured": True, **setting.to_json()}

    def _warn_log_migration_failure(self, error: Exception) -> None:
        detail = str(error) or type(error).__name__
        print(
            f"WARNING: pre-trajectory log migration failed: {detail}",
            file=sys.stderr,
            flush=True,
        )
        try:
            self._log(
                "warn",
                "daemon",
                "logs.migration_failed",
                errorType=type(error).__name__,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - stderr is the final warning seam
            pass

    def load_persistent_configuration(self) -> PersistentConfiguration:
        """Load the exact persistent config validated during normal startup."""

        return self.persistent_config.load()

    def configure_user_adapters(
        self, configuration: PersistentConfiguration
    ) -> tuple[str, ...]:
        """Attach receiver-owned Lark adapters from validated local config."""

        adapter_configs = {
            adapter.name: adapter for adapter in configuration.channels.gateways
        }
        instances: dict[str, GatewayIoAuthority] = {}
        configured: list[str] = []
        for profile in self.user_profiles.list():
            adapter_uri = profile.squire_adapter
            if (
                adapter_uri is None
                or profile.preferred_receiver.machine != self.node_id
            ):
                continue
            parsed_adapter = parse_channel_uri(adapter_uri)
            adapter_name = parsed_adapter[2] if parsed_adapter is not None else adapter_uri
            adapter_config = adapter_configs.get(adapter_name)
            if adapter_config is None:
                continue
            adapter = instances.get(adapter_config.name)
            if adapter is None:
                secret_path = (
                    self.hyprial_home
                    / "secrets"
                    / f"{adapter_config.credential_ref}.json"
                )
                raw_secret = json.loads(secret_path.read_text(encoding="utf-8"))
                app_secret = (
                    raw_secret.get("appSecret")
                    if isinstance(raw_secret, dict)
                    else None
                )
                if not isinstance(app_secret, str) or not app_secret:
                    raise ValueError(
                        f"credential {adapter_config.credential_ref} is missing appSecret"
                    )
                adapter = self._lark_gateway_with_scope_recovery(
                    adapter_config, app_secret, self.state_dir, owned=True,
                    logger=self._gateway_logger,
                )
                instances[adapter_config.name] = adapter
            self.user_adapters.register(adapter_uri, adapter)
            configured.append(adapter_uri)
        return tuple(sorted(configured))

    def _expire_previous_generation_lifecycle_state(self) -> None:
        """U0c startup sweep: cancel the dead generation's lifecycle receipts.

        Three moves, in order:

        1. BACKFILL -- the durable receipts (desired-state harness/session
           + the agents registry) are each a domain's atomic attestation
           that an effect RAN; the ones whose journal completion never
           landed (daemon died between the domain commit and the journal
           write) are journaled now, so compensation can undo them like any
           other completed effect.
        2. ROLL BACK orphans -- receipts the journal cannot account for
           (lost/legacy databases) are undone domain-locally; this is what
           collects the U0b ghost receipts on homes whose journal predates
           them.
        3. EXPIRE -- no receipt crosses the generation; the leftovers are
           retirement-handshake crash windows and are deleted.

        All of this runs BEFORE the harness actor is constructed (its
        restore-deferral set is seeded from incomplete receipts -- a dead
        generation's receipt must never hand a resource to an executor that
        no longer exists) and BEFORE the lifecycle manager boots (its
        recovery must find no cross-generation receipts and no resumable
        forward progress).  The journal is never pruned: it is the
        compensation input and the U0b rescue evidence.
        """

        claims = [
            *self.desired_state.lifecycle_effect_claims(),
            *self._agent_session_domains.lifecycle_effect_claims(),
        ]
        backfilled = (
            self._state_persistence.settled_journal.backfill_domain_attested_effects(
                tuple(claims)
            )
            if claims
            else ()
        )
        rolled_back, expired = (
            self.desired_state.expire_interrupted_lifecycle_receipts()
        )
        agent_receipts = self._agent_session_domains.expire_lifecycle_receipts()
        if backfilled or rolled_back or expired or agent_receipts:
            self._log(
                "info",
                "daemon",
                "lifecycle.receipts.expired",
                backfilled=len(backfilled),
                rolledBack=rolled_back,
                desiredStateReceipts=expired,
                agentRegistryReceipts=agent_receipts,
                detail=(
                    "no lifecycle receipt crosses a daemon generation: "
                    "domain-attested effects were journaled for compensation, "
                    "orphan admissions rolled back, retirement crash windows "
                    "deleted; interrupted sagas are compensated, never resumed"
                ),
            )

    def _reload_user_adapters(self) -> None:
        """Refresh receiver-owned adapters after setup changed local config."""

        self.configure_user_adapters(self.load_persistent_configuration())

    def _gossip_for_startup(self) -> bool:
        gossip = not self.network_isolated and zenoh_environment_flag(
            "HYPRIAL_ZENOH_GOSSIP"
        )
        self._startup_network["gossip"] = gossip
        return gossip

    def _acquire_lock(self) -> None:
        # A previous daemon for this state directory releases this flock only at
        # the very end of _close(), *after* the control socket it advertises has
        # already been unlinked. `hyprial daemon stop` reports success as soon as
        # that socket disappears (cli.py) and IsolatedDaemon.sigterm() likewise
        # only waits for socket-absence, so a rapid restart routinely overtakes
        # the old process's teardown. Failing immediately on that overlap
        # surfaced as DAEMON_START_FAILED on the third rapid restart in E2E-006.
        # Wait for the lock to become free (bounded) instead of losing the race.
        timeout = _lock_wait_timeout()
        try:
            fence = DaemonStateOwnershipFence.acquire(
                self.state_dir, timeout=timeout
            )
        except DaemonOwnershipBusy as error:
            raise RuntimeError(
                "daemon lock for this state directory is still held after "
                f"{timeout:g}s; another daemon is running or a previous "
                "one has not finished shutting down"
            ) from error
        self._lock_stream = fence.detach()

    def _write_pid_file(self) -> None:
        path = self.state_dir / "daemon.json"
        temporary = self.state_dir / f".daemon.json.{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "pid": os.getpid(),
                    "nodeId": self.node_id,
                    "socket": str(self.socket_path),
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _build_provider_auth_coordinator(self) -> Any:
        """Wire provider-auth relogin/alert coordination (spec 2026-09-14).

        Returns None when the feature cannot be wired: a daemon that starts
        without it is degraded (no auth-failure alerts), while a daemon that
        cannot start because its *alerting* feature failed is the worse
        object -- the same call autoupdate.alert makes.  The degradation is
        logged, not silent.
        """

        try:
            import shutil
            import socket

            from hyprial.daemon.impl.autoupdate.alert import notify_owner
            from hyprial.identity import (
                DeviceLoginRunner,
            )
            from hyprial.identity import ProviderAuthAuthority as ProviderAuthCoordinator
            store = self.user_profiles
            profile = (
                store.get_by_owner(self.owner) if store.path.exists() else None
            )
            agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
            auth_path = (
                Path(agent_dir).expanduser() / "auth.json"
                if agent_dir
                else Path.home() / ".pi" / "agent" / "auth.json"
            )
            pi_binary = shutil.which("pi")

            def runtime_context_valid(context: Any) -> bool:
                try:
                    current = self.agents.require(context.actor)
                except Exception:  # noqa: BLE001 -- stale context is rejection
                    return False
                return (
                    current.uri == context.actor
                    and current.entity_token == context.entity_token
                )

            def runtime_helper(context: Any) -> Any:
                from hyprial.identity import (
                    apply_runtime_environment_profile,
                )

                environment = apply_runtime_environment_profile(
                    os.environ, context.environment()
                )
                return _ContextBoundProviderAuthRunner(
                    context,
                    DeviceLoginRunner(
                        pi_command=(pi_binary,) if pi_binary else ("pi",),
                        environment=environment,
                    ),
                    self._agent_runtime_launch_custody,
                )

            return ProviderAuthCoordinator(
                profile_store=store,
                owner_key=profile.owner_key if profile is not None else None,
                notifier=lambda text, *, idempotency_key: notify_owner(
                    hyprial_home=self.hyprial_home,
                    state_dir=self.state_dir,
                    text=text,
                    idempotency_key=idempotency_key,
                ),
                helper_runner=DeviceLoginRunner(
                    pi_command=(pi_binary,) if pi_binary else ("pi",)
                ),
                auth_path=auth_path,
                host=socket.gethostname(),
                stop=self.stop_event,
                logger=lambda event, **fields: self._log(
                    "info", "daemon", event, **fields
                ),
                runtime_helper_factory=runtime_helper,
                runtime_context_validator=runtime_context_valid,
            )
        except Exception as error:  # noqa: BLE001 -- see docstring
            self._log(
                "warn",
                "daemon",
                "provider.auth.init.failed",
                errorType=type(error).__name__,
            )
            return None
