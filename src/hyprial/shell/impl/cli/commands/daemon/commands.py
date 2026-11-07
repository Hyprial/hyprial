"""``hyprial daemon run/stop/status``."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
import time
from typing import Any
from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import ipc_errors
import os
import typer
from hyprial.shell.impl.cli.commands.common.groups import group

from hyprial.shell.impl.cli.commands.common.daemon_stop import _stop_daemon_for_operator
from hyprial.shell.impl.cli.commands.common.support import (
    DAEMON_START_IDLE_BUDGET_SECONDS,
    JsonObject,
)


daemon_app = group(help="Run, stop, start, and inspect the Harness daemon.")


@daemon_app.command("status")
def daemon_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show exactly the daemon portion of ``hyprial daemon status``."""
    services = get_services()

    def operation() -> JsonObject:
        try:
            result = services.runtime.request("ps", restore_wait=0.0)
        except ipc_errors.DaemonRestoringError:
            probe = services._daemon_probe(timeout=2.0)
            daemon = {
                key: probe[key]
                for key in (
                    "running",
                    "pid",
                    "epoch",
                    "nodeId",
                    "owner",
                    "socket",
                    "phase",
                )
                if key in probe
            }
            restoring = True
            restore_pending = probe.get("restorePending", True)
        except ipc_errors.DaemonUnavailableError:
            daemon = {"running": False}
            restoring = None
            restore_pending = None
        else:
            if not isinstance(result, dict) or not isinstance(
                result.get("daemon"), dict
            ):
                raise services.CliError(
                    "INVALID_RESPONSE", "daemon ps result must contain daemon"
                )
            daemon = result["daemon"]
            restoring = result.get("restoring")
            restore_pending = result.get("restorePending")
        return {
            "ok": True,
            **({"restoring": restoring} if isinstance(restoring, bool) else {}),
            **(
                {"restorePending": restore_pending}
                if isinstance(restore_pending, bool)
                else {}
            ),
            "daemon": daemon,
        }

    services.runtime.execute(operation, json_output=json_output)


@daemon_app.command("start")
def daemon_start(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Start the daemon in the background and return once it is serving.

    This is the same launch primitive ``init`` uses.  Use ``daemon run`` when a
    supervisor or process manager owns the foreground daemon process.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        launched = services._launch_daemon_process(
            ready_timeout=DAEMON_START_IDLE_BUDGET_SECONDS
        )
        return launched.to_payload()

    services.runtime.execute(operation, json_output=json_output)


class _IdentityHold:
    """The identity lock ``daemon run`` holds until ready, droppable while it waits.

    Lock order stays identity -> daemon.lock: :meth:`yielded` releases the
    identity lock for one wait, then re-takes it and re-checks the expected
    generation (a login may commit in that window) before the next fence try.
    """

    def __init__(
        self, transaction: Any, acquire: Callable[[], Any], verify: Callable[[], None]
    ) -> None:
        self._transaction: Any | None = transaction
        self._acquire = acquire
        self._verify = verify

    @contextmanager
    def yielded(self) -> Iterator[None]:
        self.close()
        yield
        self._transaction = self._acquire()
        self._verify()

    def detach(self) -> Any:
        transaction = self._transaction
        if transaction is None:
            raise RuntimeError("identity transaction lock is not held")
        self._transaction = None
        return transaction.detach()

    def close(self) -> None:
        transaction = self._transaction
        self._transaction = None
        if transaction is not None:
            transaction.close()


def _acquire_daemon_fence(
    services: Any,
    state_dir: Any,
    *,
    timeout: float,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[dict[str, object]], None] | None = None,
    json_output: bool = False,
    wait_while_healthy: bool = True,
    yield_identity: Callable[[], AbstractContextManager[None]] | None = None,
):
    """Take the daemon lock, waiting while a healthy daemon holds it.

    The bounded first attempt keeps the shutdown-overlap behaviour the daemon
    start path already relies on. After that, Allen's ruling (2026-10-29):
    block while a serving daemon answers and take over when it exits, instead of
    exiting -- exiting is what made launchd's KeepAlive restart the same losing
    process 2,900 times. The wait is observable: every poll reports
    `waiting-for-lock` with the holder pid.

    ``wait_while_healthy=False`` (a daemon no supervisor relaunches) keeps
    only the bounded first attempt: an unsupervised loser exits instead of
    lingering as a second process behind a healthy owner.

    ``yield_identity`` (``daemon run``): every wait -- the bounded first
    attempt included -- happens inside it, so the identity lock is never held
    across a sleep; each fence try is a single non-blocking attempt.
    """

    from hyprial.daemon import (
        DaemonOwnershipBusy,
        DaemonStateOwnershipFence,
        wait_for_state_ownership,
    )

    pause = sleep or time.sleep
    if yield_identity is None:
        try:
            return DaemonStateOwnershipFence.acquire(state_dir, timeout=timeout)
        except DaemonOwnershipBusy as error:
            if not wait_while_healthy:
                raise services.CliError(error.code, str(error)) from error
        nap = pause
    else:
        def nap(seconds: float) -> None:
            with yield_identity():
                pause(seconds)

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                return DaemonStateOwnershipFence.acquire(state_dir, timeout=0.0)
            except DaemonOwnershipBusy as error:
                if time.monotonic() >= deadline:
                    if not wait_while_healthy:
                        raise services.CliError(error.code, str(error)) from error
                    break
            nap(0.1)

    def _holder() -> int | None:
        try:
            value = services._daemon_probe(timeout=0.5)
        except Exception:  # noqa: BLE001 - no answer means "not healthy"
            return None
        daemon = value.get("daemon") if isinstance(value, dict) else None
        source = daemon if isinstance(daemon, dict) else value
        pid = source.get("pid") if isinstance(source, dict) else None
        return pid if type(pid) is int and pid > 0 else None

    report = log or (
        lambda event: _report_lock_wait(event, json_output=json_output)
    )
    try:
        return wait_for_state_ownership(
            state_dir,
            acquire=lambda path: DaemonStateOwnershipFence.acquire(path, timeout=0.0),
            probe_holder=_holder,
            sleep=nap,
            log=report,
        )
    except DaemonOwnershipBusy as error:
        raise services.CliError(error.code, str(error)) from error


def _report_lock_wait(event: dict[str, object], *, json_output: bool) -> None:
    """Report a wait through the CLI output channel, on stderr only.

    Production runs the supervised daemon as ``daemon run --json``, where stdout
    is reserved for the single result object; ``progress`` writes one NDJSON
    event per line under ``--json`` and the human sentence otherwise, and keeps
    every write inside the output module (a direct ``sys.stderr.write`` here is
    an output-discipline violation).
    """

    if event.get("event") != "waiting-for-lock":
        return
    from hyprial.shell.impl.cli.output import progress

    progress(
        "daemon.lock.waiting",
        f"daemon.lock busy: waiting for lock (holder_pid={event.get('holder_pid')})",
        json_output=json_output,
        holderPid=event.get("holder_pid"),
    )


@daemon_app.command("run")
def daemon_run(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON after shutdown."),
) -> None:
    """Run the daemon in the foreground until SIGINT or SIGTERM (debug/process manager)."""
    services = get_services()

    # A service manager (launchd, systemd) appends this process's stdout/stderr
    # to one file that outlives every restart, so each record has to name both
    # when it was written and which daemon wrote it (M-hq-adjutant-0002).
    # Installed around the whole launch: the result envelope, progress lines,
    # startup failures and daemon-internal ``print`` calls all leave here.
    from hyprial.shell.impl.daemon_wiring import daemon_stdio_stamp

    stamp, stamped_stdio = daemon_stdio_stamp()

    # Pop the launch expectation before anything else runs, so no descendant
    # (worker, harness, autoupdate child) can inherit a stale generation.
    from hyprial.shell.impl.login.generation import (
        IDENTITY_GENERATION_STALE_EXIT_CODE,
        IdentityGenerationStale,
        service_managed,
        take_expected_generation,
        take_launch_id,
        verify_expected_generation,
    )

    expected_generation = take_expected_generation(os.environ)
    launch_id = take_launch_id(os.environ)

    def operation() -> JsonObject:
        from hyprial.identity import IdentityTransactionBusy
        services = get_services()
        services.require_initialized_hyprial_home()
        from hyprial.daemon import DaemonApplication
        from hyprial.daemon import application_lock_wait_timeout as _lock_wait_timeout
        from hyprial.identity import IdentityTransactionLock

        state_dir = services._state_dir()
        home = services._hyprial_home()

        def verify() -> None:
            if expected_generation is None:
                return
            try:
                verify_expected_generation(home, expected_generation)
            except IdentityGenerationStale as error:
                from hyprial.shell.impl.cli.output import report_error

                report_error(
                    services.CliError(
                        error.code,
                        str(error),
                        {
                            "expectedIdentityGeneration": error.expected,
                            "identityGeneration": error.current,
                        },
                    ),
                    json_output=json_output,
                )
                raise typer.Exit(code=IDENTITY_GENERATION_STALE_EXIT_CODE)

        def acquire() -> Any:
            try:
                return IdentityTransactionLock.acquire(home, timeout=_lock_wait_timeout())
            except IdentityTransactionBusy as error:
                raise services.CliError(error.code, str(error)) from error

        # Acquired here, by this process, before identity is read (no
        # inherited descriptor): a login commits and releases first.  The one
        # exception is a <=0.5.5.3.3 upgrade tail that still hands its held
        # descriptor over; contending with it would deadlock on our own fd.
        legacy = IdentityTransactionLock.adopt_legacy(home, os.environ)
        hold = _IdentityHold(legacy if legacy is not None else acquire(), acquire, verify)
        try:
            verify()
            fence = _acquire_daemon_fence(
                services,
                state_dir,
                timeout=_lock_wait_timeout(),
                json_output=json_output,
                wait_while_healthy=service_managed(os.environ),
                # Never sit on the identity lock while waiting for daemon.lock:
                # a login would see IDENTITY_TRANSACTION_BUSY for the whole
                # wait.  The legacy descriptor is still shared with its
                # launcher, so it cannot be dropped and re-taken.
                yield_identity=None if legacy is not None else hold.yielded,
            )
            with fence:
                from hyprial.shell.impl.daemon_wiring import daemon_dependencies, usage_factory

                runtime = DaemonApplication.from_environment(
                    state_dir=state_dir, socket_path=services._socket_path(),
                    usage_factory=usage_factory, **daemon_dependencies(),
                )
                # From here on a line can name the daemon instance, not just the
                # operating-system process a supervisor happened to launch.
                stamp.bind_epoch(runtime.epoch)
                # This interpreter exists to be the daemon, so the daemon is
                # allowed to end it if shutdown leaves a thread that will not join.
                runtime.owns_process_exit()
                runtime.run(
                    ownership_stream=fence.detach(),
                    identity_transaction_stream=hold.detach(),
                    launch_id=launch_id,
                )
        finally:
            hold.close()
        return {"ok": True, "stopped": True}

    with stamped_stdio:
        services.runtime.execute(
            operation, json_output=json_output, allow_missing_home=True
        )


@daemon_app.command("stop")
def daemon_stop(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    expected_epoch: str | None = typer.Option(
        None, "--expected-epoch", help="Refuse to stop a different daemon generation.",
    ),
) -> None:
    """Ask the isolated daemon owning this socket to stop gracefully."""
    services = get_services()

    action = (
        _stop_daemon_for_operator if expected_epoch is None
        else lambda: _stop_daemon_for_operator(expected_epoch=expected_epoch)
    )
    services.runtime.execute(action, json_output=json_output)
