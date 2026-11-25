# Decision record: daemon-first-z6pre-owned-logout-20261117
# - An owned start asks the application to own the process exit unless the run
#   stops for a pure logout; after such a stop the same process enters the
#   lifecycle loop that an ownerless start already uses.
# - The expected identity generation is a launch check and is not repeated on
#   that re-entry: the login record is gone after a logout.
# - The run may return without ending the process, so the interpreter is
#   left through the loop's leave on every return or raise (failed on raise).
# - A home whose owner survives the logout (HYPRIAL_OWNER, partial removal)
#   cannot be admitted by the bootstrap phase, so that process ends as before.
# - W2A-LOGIN-INPROC (2026-11-21): retired <=0.5.5.3.3 adopt_legacy descriptor
#   handoff; daemon run always acquires IdentityTransactionLock in-process and
#   yields it while waiting for daemon.lock so logins do not block on wait.
"""``hyprial daemon run/stop/status``."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
import time
from typing import Any
from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.error import codes
from hyprial.kernel import ipc_errors
import os
import typer
from hyprial.shell.impl.cli.commands.common.groups import group

from hyprial.shell.impl.cli.commands.common.daemon_stop import _stop_daemon_for_operator
from hyprial.shell.impl.cli.commands.common.ensure.scope import ACCOUNT_REQUIRED
from hyprial.shell.impl.cli.commands.common.support import (
    DAEMON_START_IDLE_BUDGET_SECONDS,
    JsonObject,
)


daemon_app = group(help="Run, stop, start, and inspect the Harness daemon.")

BOOTSTRAP_REFUSAL_CODES = frozenset(
    {codes.CALLER_NOT_AUTHORIZED, codes.METHOD_NOT_FOUND, ACCOUNT_REQUIRED}
)


def confirmed_bootstrap_refusal(
    services: Any, error: Exception
) -> tuple[dict[str, Any], Any]:
    """The probe of a daemon that is in bootstrap; otherwise raise ``error``."""

    from hyprial.shell.impl.cli.commands.daemon.bootstrap import probed_daemon

    try:
        daemon, restore_pending = probed_daemon(services)
    except (services.CliError, ipc_errors.DaemonRequestError):
        raise error from None
    if daemon.get("phase") != "bootstrap":
        raise error
    return daemon, restore_pending


@daemon_app.command("status")
def daemon_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show exactly the daemon portion of ``hyprial daemon status``."""
    services = get_services()

    def operation() -> JsonObject:
        try:
            result = services.runtime.request("ps", restore_wait=0.0)
        except services.CliError as error:
            if error.code not in BOOTSTRAP_REFUSAL_CODES:
                raise
            daemon, restore_pending = confirmed_bootstrap_refusal(services, error)
            restoring = None
        except ipc_errors.DaemonRestoringError:
            from hyprial.shell.impl.cli.commands.daemon.bootstrap import (
                probed_daemon,
            )

            daemon, restore_pending = probed_daemon(services, restore_default=True)
            restoring = True
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

    services.runtime.execute(operation, json_output=json_output, ensure_daemon=False)


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

    services.runtime.execute(operation, json_output=json_output, ensure_daemon=False)


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
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit JSON after shutdown."),
    bootstrap: bool = typer.Option(
        False,
        "--bootstrap",
        help="Explicitly cold-start an empty home and become its daemon (same process).",
    ),
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

    loop_entered = False

    def operation() -> JsonObject:
        nonlocal loop_entered
        services = get_services()
        home = services._hyprial_home()
        from hyprial.shell.impl.daemon_wiring.onboarding import (
            OwnerlessLaunchError,
            build_daemon_application,
            home_owner_or_none,
            launch_ownerless_daemon,
            stopped_for_logout,
        )

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

        def run_lifecycle_loop() -> None:
            nonlocal loop_entered
            loop_entered = True
            try:
                launch_ownerless_daemon(
                    home=home,
                    state_dir=services._state_dir(),
                    socket_path=services._socket_path(),
                    application_factory=build_daemon_application,
                    launch_id=launch_id,
                    exit_unless_logout=True,
                    bind_epoch=stamp.bind_epoch,
                )
            except OwnerlessLaunchError as error:
                raise services.CliError(error.code, str(error)) from error

        if home_owner_or_none(os.environ, home) is None:
            verify()
            run_lifecycle_loop()
            return {"ok": True, "stopped": True}
        from hyprial.identity import IdentityTransactionBusy

        from hyprial.daemon import DaemonApplication
        from hyprial.daemon import application_lock_wait_timeout as _lock_wait_timeout
        from hyprial.identity import IdentityTransactionLock

        state_dir = services._state_dir()

        def acquire() -> Any:
            try:
                return IdentityTransactionLock.acquire(home, timeout=_lock_wait_timeout())
            except IdentityTransactionBusy as error:
                raise services.CliError(error.code, str(error)) from error

        hold = _IdentityHold(acquire(), acquire, verify)
        logged_out = False
        try:
            verify()
            fence = _acquire_daemon_fence(
                services,
                state_dir,
                timeout=_lock_wait_timeout(),
                json_output=json_output,
                wait_while_healthy=service_managed(os.environ),
                yield_identity=hold.yielded,
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
                runtime.exit_unless_logout()
                loop_entered = True
                runtime.run(
                    ownership_stream=fence.detach(),
                    identity_transaction_stream=hold.detach(),
                    launch_id=launch_id,
                )
                logged_out = stopped_for_logout(runtime)
        finally:
            hold.close()
        if logged_out:
            if home_owner_or_none(os.environ, home) is None:
                run_lifecycle_loop()
        return {"ok": True, "stopped": True}

    interpreter_owned = bool((ctx.obj or {}).get("interpreterOwned"))
    with stamped_stdio:
        try:
            services.runtime.execute(
                operation, json_output=json_output, allow_missing_home=True, ensure_daemon=False
            )
        except BaseException:
            if interpreter_owned and loop_entered:
                from hyprial.shell.impl.daemon_wiring.onboarding import (
                    leave_ownerless_loop,
                )

                leave_ownerless_loop(failed=True)
            raise
        if interpreter_owned and loop_entered:
            from hyprial.shell.impl.daemon_wiring.onboarding import (
                leave_ownerless_loop,
            )

            leave_ownerless_loop()


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
    services.runtime.execute(action, json_output=json_output, ensure_daemon=False)
