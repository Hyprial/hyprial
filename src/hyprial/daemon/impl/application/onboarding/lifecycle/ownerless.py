# Decision record:
# - The daemon process owns the bootstrap wire and the serving application in
#   one interpreter.  A successful initialize closes only the bootstrap socket;
#   the runtime transfers both continuous locks to the serving application.
# - What follows each way ``serve`` or the serving application ends:
#   ``promote`` runs the serving application; ``stop`` exits; ``reopen`` (a
#   pre-initialize logout) admits a fresh bootstrap wire.  After the serving
#   application returns, its explicit ``stop_reason`` decides: ``logout``
#   reopens bootstrap in the same process, anything else (``shutdown``, a
#   signal, ownership loss) exits and never reopens one.  The reason is the
#   application's own public policy, not the diagnostic stop trace.
# - ``initialize-unacknowledged`` exits with ``BOOTSTRAP_INITIALIZE_UNACKNOWLEDGED``.
#   The login behind the claim is already committed, so a fresh bootstrap
#   admission would be refused (an authenticated home needs normal startup);
#   the claim stays ``promoting`` until the host's ``close`` marks it unknown,
#   and the next start takes the normal path, where recovery owns it.  This
#   covers a reply that could not be written and, in the wire host, an
#   exception while encoding the reply or inside the registry after the claim
#   was durably accepted (the armed post-reply action is taken and dropped).
# - ``shutdown`` racing an accepted initialize: the wire host's stop is sticky
#   and wins, so ``serve`` returns ``stop`` even when a promotion was already
#   armed.  The claim is then marked unknown by the host's ``close`` and both
#   locks are released there; the next start recovers the claim.  No serving
#   application is started for a stop that was acknowledged to a client.
# - Signals: one handler pair is installed for the whole loop.  Between
#   phases (including the gap between a logout stop and the next bootstrap
#   wire) a SIGINT or SIGTERM only sets a flag, which the loop honours before
#   it opens another wire, right after it opens one and right after it
#   constructs a promoted application; so a signal arriving during the logout
#   to bootstrap transition ends in exit.  The serving application installs
#   its own handlers while it runs and restores these on return.  Handlers
#   only store attributes, so they take no lock.
# - ``owns_process_exit`` is passed through to the serving application
#   unchanged.  The CLI passes ``exit_unless_logout=True`` instead: a forced
#   exit at the first logout would defeat the same-PID loop, but a loop that
#   never forces the exit would leave a final stop at the mercy of a lingering
#   non-daemon thread.  The serving application therefore owns the exit unless
#   the only stop requested was ``logout``; see ``exit_independence``.
# - Z-6b: ``reopen`` removes this process's own ``daemon.json`` bootstrap
#   marker before the next wire is admitted.  Left in place it names a live
#   pid (this one), so the next admission refused with "previous process has
#   not exited" and a pre-initialize logout ended the daemon with status 1;
#   the GUI-hosted fallback of the request-http child used to hide that.
# - Z-5: a login the daemon drives (``auth.login.begin`` with ``until: serving``)
#   promotes without any client calling initialize.  The driver signals
#   ``promote`` only after it left the dispatcher lock (no driver is still inside
#   the accept), and the serving ``completed`` callback goes through the
#   first-login controller: the initialize receipt is completed only when the
#   device is registered and the run is then ``serving``.  Whatever ends the phase
#   (``stop``, ``reopen``, a failed construction, an unacknowledged initialize)
#   ends the runs ``stopped`` in ``host.close``.  An explicit
#   ``onboarding.initialize`` has no run, so its completion is the one it always
#   had.
# - Native actor loading is checked before bootstrap admission.  The loader is
#   injectable only for isolated tests; production uses the fail-closed native
#   backend loader.
# - W1E-2 (2026-11-21): process umask (0o077 on POSIX) is applied once at the
#   CLI/daemon OS process entry (shell/impl/cli/runtime/entry.py:main), which
#   covers every real ``hyprial daemon run`` child (detached start, launchd/
#   systemd service, foreground run, owned start, and post-logout lifecycle
#   re-entry) without repeating apply_private_umask() across lifecycle phases.
"""Ownerless daemon lifecycle: bootstrap wire, promotion, service, logout."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
import os
import signal
import threading
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.application.onboarding import CallContext
from hyprial.daemon.impl.application.onboarding.bootstrap.wire import (
    PROMOTE,
    REOPEN,
    STOP,
    UNACKNOWLEDGED,
    BootstrapWireHost,
)
from hyprial.daemon.impl.onboarding.runtime import BootstrapRuntime

from hyprial.kernel import (
    atomic_json_write,
    load_native,
    process_birth_identity,
)

from .promoted import install_serving_port
from .stop_reason import STOP_LOGOUT

__all__ = ["BootstrapLaunchError", "launch_daemon_lifecycle", "check_native_engine"]


class BootstrapLaunchError(RuntimeError):
    """A refused ownerless daemon lifecycle with a stable public code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.data: dict[str, Any] = {}


def check_native_engine() -> None:
    """Load the shipped actor extension before admitting a daemon home."""

    load_native()


class _LoopSignals:
    """SIGINT/SIGTERM for the whole loop; handlers only store attributes."""

    def __init__(self) -> None:
        self.requested = False
        self._host: BootstrapWireHost | None = None
        self._previous: dict[int, Any] = {}

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)

    def restore(self) -> None:
        previous, self._previous = self._previous, {}
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    def attach(self, host: BootstrapWireHost | None) -> None:
        self._host = host
        if host is not None and self.requested:
            host.request_stop()

    def _handle(self, _signum: int, _frame: Any) -> None:
        self.requested = True
        host = self._host
        if host is not None:
            host.request_stop()


def launch_daemon_lifecycle(
    *,
    home: Path,
    state_dir: Path,
    socket_path: Path,
    application_factory: Callable[[Path, Path], Any],
    native_loader: Callable[[], Any] = check_native_engine,
    owns_process_exit: bool = False,
    exit_unless_logout: bool = False,
    launch_id: str | None = None,
    bind_epoch: Callable[[str], None] | None = None,
) -> None:
    """Run the daemon's ownerless-to-serving lifecycle until shutdown.

    The bootstrap client performs login and initialize over ``daemon.sock``.
    Promotion is consumed by this process after the successful response has
    been flushed, then the existing application run loop owns the same locks.
    A serving application that stops for ``logout`` returns here and the next
    iteration admits a fresh bootstrap wire; any other stop ends the loop.
    An owned ``daemon run`` enters this loop too, after its first pure logout.
    ``bind_epoch`` names the daemon instance of each phase for log stamps.
    """

    home, state_dir, socket_path = map(Path, (home, state_dir, socket_path))
    signals = _LoopSignals()
    signals.install()
    try:
        native_loader()
        while not signals.requested:
            if not _run_phase(
                home,
                state_dir,
                socket_path,
                application_factory,
                owns_process_exit,
                exit_unless_logout,
                launch_id,
                bind_epoch,
                signals,
            ):
                return
    finally:
        signals.restore()


def _run_phase(
    home: Path,
    state_dir: Path,
    socket_path: Path,
    application_factory: Callable[[Path, Path], Any],
    owns_process_exit: bool,
    exit_unless_logout: bool,
    launch_id: str | None,
    bind_epoch: Callable[[str], None] | None,
    signals: _LoopSignals,
) -> bool:
    host: BootstrapWireHost | None = None
    again = False
    try:
        host = BootstrapWireHost.open(home, state_dir=state_dir)
        signals.attach(host)
        if bind_epoch is not None:
            bind_epoch(host.dispatcher.epoch)
        _publish_bootstrap_ready(state_dir, socket_path, launch_id)
        outcome = host.serve()
        if outcome == PROMOTE:
            runtime = host.dispatcher.runtime
            reason = runtime.run_daemon(
                lambda: _construct_promoted_application(
                    runtime,
                    host,
                    application_factory,
                    bind_epoch,
                    signals,
                ),
                owns_process_exit=owns_process_exit,
                exit_unless_logout=exit_unless_logout,
            )
            again = reason == STOP_LOGOUT and not signals.requested
        elif outcome == REOPEN:
            again = not signals.requested
            _remove_bootstrap_ready(state_dir, launch_id)
        elif outcome == UNACKNOWLEDGED:
            raise BootstrapLaunchError(
                "BOOTSTRAP_INITIALIZE_UNACKNOWLEDGED",
                "initialize was accepted but its response was not acknowledged",
            )
        elif outcome != STOP:
            raise BootstrapLaunchError(
                "BOOTSTRAP_LIFECYCLE_FAILED",
                f"bootstrap wire ended with unknown outcome: {outcome}",
            )
    except BootstrapLaunchError:
        raise
    except Exception as error:
        code = getattr(error, "code", None)
        if not isinstance(code, str) or not code:
            code = "BOOTSTRAP_LIFECYCLE_FAILED"
        raise BootstrapLaunchError(code, str(error)) from error
    finally:
        signals.attach(None)
        if host is not None:
            host.close()
        if not again:
            _remove_bootstrap_ready(state_dir, launch_id)
    return again


def _publish_bootstrap_ready(
    state_dir: Path, socket_path: Path, launch_id: str | None
) -> None:
    marker: dict[str, Any] = {
        "schemaVersion": 1,
        "pid": os.getpid(),
        "socket": str(socket_path),
        "phase": "bootstrap",
        "birthIdentity": process_birth_identity(os.getpid()),
    }
    if launch_id:
        marker["launchId"] = launch_id
    atomic_json_write(Path(state_dir) / "daemon.json", marker)


def _remove_bootstrap_ready(state_dir: Path, launch_id: str | None) -> None:
    path = Path(state_dir) / "daemon.json"
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if (
        not isinstance(marker, dict)
        or marker.get("phase") != "bootstrap"
        or marker.get("pid") != os.getpid()
    ):
        return
    if launch_id is not None and marker.get("launchId") != launch_id:
        return
    path.unlink(missing_ok=True)


def _construct_promoted_application(
    runtime: BootstrapRuntime,
    host: BootstrapWireHost,
    application_factory: Callable[[Path, Path], Any],
    bind_epoch: Callable[[str], None] | None,
    signals: _LoopSignals,
) -> Any:
    application = application_factory(runtime.state_dir, runtime.socket_path)
    owner = runtime.verified_owner()
    if owner is None:
        raise BootstrapLaunchError(
            "BOOTSTRAP_IDENTITY_UNVERIFIED",
            "promotion has no verified owner",
        )
    surface = replace(
        host.dispatcher.context.surface,
        transport_generation=application.epoch,
    )
    context = CallContext(
        surface=surface,
        phase="serving",
        caller="local-gui",
        owner=owner,
    )
    if bind_epoch is not None:
        bind_epoch(application.epoch)
    if signals.requested:
        application._request_stop("signal", signal.SIGTERM)

    def complete() -> None:
        runtime.complete_initialize(surface=surface)

    controller = host.dispatcher.first_login

    def completed() -> None:
        if controller is None:
            complete()
            return
        controller.promoted(complete=complete)

    return install_serving_port(
        runtime,
        application,
        context=context,
        completed=completed,
    )
