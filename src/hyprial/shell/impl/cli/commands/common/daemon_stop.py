"""Daemon stop, teardown receipt, and process-exit proof helpers."""

from __future__ import annotations

from pathlib import Path
import json
import subprocess
from typing import TYPE_CHECKING, Any

from hyprial.kernel import lock_exclusive, probe_process, unlock

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import CliError, JsonObject

if TYPE_CHECKING:  # deferred annotations only
    from hyprial.daemon import RestartProcessObservation


def old_pid_from(error: CliError) -> int | None:
    """Pull the surviving pid out of the error's payload, never from its prose."""

    data = error.data
    if isinstance(data, dict) and isinstance(data.get("oldPid"), int):
        return data["oldPid"]
    return None


def _describe_survivor(pid: int | None) -> str:
    """Say what is still running. Best effort -- never raises, never blocks.

    ⚠️ When the count cannot be taken, it says so rather than printing 0. A
    zero that means "could not look" reads exactly like a zero that means
    "nothing left", and the whole point of this line is to tell someone whether
    there is a fleet to go and kill.
    """

    if pid is None:
        return "surviving pid: unknown (the stop reported no pid)"
    try:
        completed = subprocess.run(
            ["ps", "-axo", "ppid="],
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
    except Exception:  # noqa: BLE001 -- diagnostics must not raise here
        return f"surviving pid: {pid}; descendant count: could not be taken"
    children = sum(1 for line in completed.stdout.split() if line.strip() == str(pid))
    return f"surviving pid: {pid}; direct children still running: {children}"


def _stop_daemon_for_operator() -> JsonObject:
    """`hyprial daemon stop` -- answers as soon as teardown is done, and says what it saw.

    It asks "did it stop", not "is it gone so I may start another", so it does
    not spend the upgrade path's budget waiting for an exit nobody here will act
    on -- callers give this command about twenty seconds and teardown alone can
    use most of that.

    ⚠️ It does not therefore claim the process is gone. If the pid is still
    around when teardown finishes, that goes back as `survivingPid`: a fact for
    the caller, not a failure. Failing here was tried and measured wrong -- the
    process is normally still exiting at that moment, so failing would report a
    survivor on nearly every healthy stop.

    ⛔ Known gap, written down rather than papered over: nothing currently reads
    `survivingPid` after a bare `hyprial daemon stop`. The upgrade path does act on
    the equivalent signal, and that is the 2026-08-30 scenario; a plain operator
    stop that leaves a survivor is at present unwatched.
    """
    services = get_services()

    return services._stop_daemon_gracefully(require_exit=False)


def _stop_daemon_gracefully(*, require_exit: bool = True) -> JsonObject:
    """Stop for operator and upgrade callers using historical compatibility."""

    return _stop_daemon_with_proof(require_exit=require_exit, require_known_pid=False)


def _stop_daemon_for_identity_switch() -> JsonObject:
    """Stop only when the old PID is known and its exit can be proven."""

    return _stop_daemon_with_proof(require_exit=True, require_known_pid=True)


def _stop_daemon_with_proof(
    *, require_exit: bool, require_known_pid: bool
) -> JsonObject:
    services = get_services()
    result = services._daemon_request("shutdown", timeout=2.0)
    old_pid = (
        result.get("pid")
        if isinstance(result, dict)
        and isinstance(result.get("pid"), int)
        and result["pid"] > 0
        else None
    )
    if require_known_pid and old_pid is None:
        raise services.CliError(
            "DAEMON_STOP_IDENTITY_UNKNOWN",
            "daemon shutdown did not identify the old pid; refusing to commit "
            "an identity change without an exit fence",
            {"oldPid": None, "teardownCompleted": False},
        )
    old_identity: str | None = None
    if old_pid is not None:
        from hyprial.daemon import read_process_identity as _read_process_identity

        old_identity = _read_process_identity(old_pid)
    # ⚠️ Derived, not chosen. This was `10.0`, a number smaller than the
    # daemon's own exit backstop -- so a shutdown that needed the backstop
    # could not satisfy this wait however healthy it was, and a teardown that
    # takes its full budget could not satisfy it at all. On 2026-08-31 that
    # ended an upgrade before it launched a replacement: 56 minutes down.
    from hyprial.kernel import TEARDOWN_BUDGETED_SECONDS

    deadline = services.time.monotonic() + TEARDOWN_BUDGETED_SECONDS
    observed = _wait_daemon_teardown_receipt(
        result if isinstance(result, dict) else {},
        deadline,
        old_pid=old_pid,
        old_identity=old_identity,
        require_exit=require_exit,
    )
    return {"ok": True, **(result if isinstance(result, dict) else {}), **observed}


def _wait_daemon_teardown_receipt(
    shutdown: JsonObject,
    deadline: float,
    *,
    old_pid: int | None,
    old_identity: str | None,
    require_exit: bool = True,
) -> JsonObject:
    """Wait until the old daemon is really gone, not merely finished tearing down.

    The control socket is unlinked before adapters, actor runtimes and their log
    writers finish draining.  ``daemon.lock`` is deliberately released at the
    very end of ``DaemonApplication._close``, so acquiring it — or seeing
    ``daemon.json`` name a different pid — proves the old holder **completed its
    teardown**.

    ⚠️ It does not prove the old holder **exited**, and on 2026-08-30 that gap
    took production down for nine hours.  ``autoupdate`` stopped the daemon at
    03:17:09; all fifty-three registrations closed, the lock came back and
    ``daemon.json`` moved to the replacement — every receipt below said "done" —
    while the process stayed alive until 12:47 the next day, holding one hundred
    and six descendants that then blocked the replacement's bootstrap.  The
    caller of this function is about to start or upgrade a daemon, and for that
    the question is not "did teardown finish" but "is the old one gone".

    So the two facts are kept apart, and which one may end the wait depends on
    whether we know **who** to watch:

    * ``old_pid`` known — only the process disappearing ends the wait.  A
      released lock or a replacement's pid is evidence that teardown ran, and
      is recorded in the timeout message, but it cannot stand in for exit.
    * ``old_pid`` unknown — the process cannot be watched at all, so the lock
      and the marker are the only receipts available and are honoured as before.

    🔑 This is why a replacement daemon does not shorten the wait when the pid is
    known: its arrival says nothing whatever about the old process.  What used to
    make that look safe is that ``_stopped_process`` silently returns ``False``
    for an unknown pid, so without those exits an unknown-pid stop would always
    run to the deadline — the exits were a fallback for "we don't know who to
    watch", not a faster path to success.
    """
    services = get_services()

    raw_lock_path = shutdown.get("lockPath")
    raw_state_dir = shutdown.get("stateDir")
    lock_path = (
        Path(raw_lock_path).expanduser().resolve()
        if isinstance(raw_lock_path, str) and raw_lock_path
        else None
    )
    state_dir = (
        Path(raw_state_dir).expanduser().resolve()
        if isinstance(raw_state_dir, str) and raw_state_dir
        else (lock_path.parent if lock_path is not None else None)
    )
    stream: Any | None = None
    if lock_path is not None:
        try:
            # Never create a path while proving teardown.  The target daemon
            # minted this lock before accepting the shutdown request.
            stream = lock_path.open("r+", encoding="utf-8")
        except FileNotFoundError:
            stream = None
    # ⭐ `require_exit` is the caller's context, moved out of prose and into the
    # signature. The docstring above said "the caller of this function is about
    # to start or upgrade a daemon" -- true of the upgrade path, false of
    # `hyprial daemon stop`, which starts nothing and only wants to know whether the
    # daemon stopped. A precondition that lives only in a docstring cannot stop
    # itself from being applied where it does not hold, and on 2026-08-31 that
    # cost four CI failures: `daemon stop` inherited a 50s wait for an exit its
    # caller had no need to see, and every caller giving it 20s timed out.
    #
    # ⚠️ Default True: a caller that forgets the flag gets the strict wait. The
    # other default would silently drop the exit requirement for some future
    # "about to start a replacement" path, and that is the nine-hour outage.
    # ⭐ `require_exit` is the caller's context, moved out of prose into the
    # signature. The docstring above says "the caller of this function is about
    # to start or upgrade a daemon" -- true of the upgrade path, false of
    # `hyprial daemon stop`, which starts nothing. A precondition that lives only in
    # a docstring cannot stop itself being applied where it does not hold, and
    # on 2026-08-31 that cost four CI failures.
    #
    # ⚠️ Default True: a caller that forgets the flag gets the strict wait. The
    # other default would silently drop the exit requirement for some future
    # "about to start a replacement" path -- the nine-hour outage.
    receipts_are_sufficient = old_pid is None or not require_exit
    teardown_done = False
    # Report how long we actually waited, not the nominal budget. The previous
    # message hardcoded "10s" and kept saying it after the budget became
    # TEARDOWN_BUDGETED_SECONDS -- an error message that quietly went stale
    # because nothing compares prose against the code beside it.
    started = services.time.monotonic()
    try:
        while True:
            lock_busy = False
            if stream is not None:
                try:
                    lock_exclusive(stream.fileno(), blocking=False)
                except BlockingIOError:
                    lock_busy = True
                else:
                    unlock(stream.fileno())
                    teardown_done = True
                    if receipts_are_sufficient:
                        # ⭐ Report a survivor as a **fact**, not as a verdict.
                        # Failing here was tried and measured wrong: a moment
                        # after the receipts land the process is almost always
                        # still there, because it is in the act of exiting. So
                        # a boolean must not decide -- "still running" 50ms
                        # after teardown and "still running" nine hours later
                        # are the same boolean and entirely different events.
                        if old_pid is None or services._stopped_process(old_pid, old_identity):
                            # ⚠️ The field is ABSENT when the process is gone,
                            # not null and not 0: a null reads the same as "we
                            # did not look", and saying whether anyone is still
                            # there is this field's only job.
                            return {"teardownCompleted": True}
                        return {
                            "teardownCompleted": True,
                            "survivingPid": old_pid,
                        }

            # A new daemon can legitimately acquire the same persistent lock
            # before this waiter does.  Its different PID proves the old holder
            # completed teardown -- and nothing more, so with a pid in hand we
            # keep waiting for that pid rather than following the replacement.
            if state_dir is not None and old_pid is not None:
                try:
                    marker = json.loads(
                        (state_dir / "daemon.json").read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    marker = None
                if (
                    isinstance(marker, dict)
                    and isinstance(marker.get("pid"), int)
                    and marker["pid"] != old_pid
                ):
                    teardown_done = True

            if services._stopped_process(old_pid, old_identity):
                return {"teardownCompleted": teardown_done}

            if services.time.monotonic() >= deadline:
                waited = services.time.monotonic() - started
                if teardown_done:
                    # ⭐ A distinct code, not just distinct prose. The caller has
                    # to branch on this, and matching on a sentence is a coupling
                    # that breaks silently the first time someone rewords it.
                    #
                    # The two timeouts mean opposite things about what to do
                    # next: the one below says "we stopped watching" -- the
                    # process may well be exiting. This one says "we watched,
                    # and it is definitely still there". Waiting longer will not
                    # help; someone has to go find the survivor.
                    raise services.CliError(
                        "DAEMON_STOP_SURVIVOR",
                        "daemon teardown completed but process "
                        f"{old_pid} did not exit within {waited:.1f}s; it is "
                        "still alive and may still own harness children. Check "
                        "for descendants before starting a replacement.",
                        {"oldPid": old_pid, "teardownCompleted": True},
                    )
                detail = (
                    "lock is still held" if lock_busy else "old process is still alive"
                )
                raise services.CliError(
                    "DAEMON_STOP_TIMEOUT",
                    "daemon socket closed but teardown did not finish within "
                    f"{waited:.1f}s ({detail})",
                    {"oldPid": old_pid, "teardownCompleted": False},
                )
            services.time.sleep(0.05)
    finally:
        if stream is not None:
            stream.close()


def _stopped_process(pid: int | None, identity: str | None) -> bool:
    if pid is None:
        return False
    if identity is not None:
        from hyprial.daemon import owner_process_status as _owner_process_status

        return _owner_process_status(pid, identity).value in {
            "pid-missing",
            "identity-mismatch",
        }
    try:
        probe_process(pid)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def _observe_restart_process(error: BaseException) -> RestartProcessObservation:
    """Observe the exact child named by a launch failure at notification time."""
    from hyprial.daemon import RestartProcessObservation, RestartProcessState

    pid = getattr(error, "_daemon_process_pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return RestartProcessObservation(None, RestartProcessState.UNKNOWN)
    if getattr(error, "_daemon_process_exited", False) is True:
        # Popen.wait/poll is an observation of this exact child, so later PID
        # reuse cannot turn its completed birth back into a running process.
        return RestartProcessObservation(pid, RestartProcessState.EXITED)
    identity = getattr(error, "_daemon_process_identity", None)
    if not isinstance(identity, str) or not identity:
        return RestartProcessObservation(pid, RestartProcessState.UNKNOWN)

    from hyprial.daemon import owner_process_status as _owner_process_status

    status = _owner_process_status(pid, identity).value
    if status == "alive":
        return RestartProcessObservation(pid, RestartProcessState.RUNNING)
    if status in {"pid-missing", "identity-mismatch"}:
        return RestartProcessObservation(pid, RestartProcessState.EXITED)
    return RestartProcessObservation(pid, RestartProcessState.UNKNOWN)
