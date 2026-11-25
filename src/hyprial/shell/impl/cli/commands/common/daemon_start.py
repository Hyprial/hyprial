# Decision record (W2B-1, 2026-11-21):
# - Delegates private file hardening (capture and launch lock) and detached
#   daemon process spawning to hyprial.shell.impl.daemon_spawn (backed by
#   hyprial.kernel.{harden_private_path,spawn_detached_daemon}) so daemon launch
#   carries zero direct POSIX session/chmod assumptions or CLI->backend edges.
# - The launch state directory is created through
#   daemon_spawn.prepare_private_state_dir (create-then-harden), not a plain
#   ``mkdir(mode=0o700)``: on CPython 3.12.4+ (gh-118486) Windows that mkdir
#   applies an OWNER RIGHTS DACL which bootstrap admission rejects as ``unsafe-acl``.
"""Daemon process lifecycle: launch, startup evidence, stop/teardown receipts."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import notice

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.daemon_spawn import (
    harden_private_path,
    prepare_private_state_dir,
    spawn_detached_daemon,
)

from typing import Any
from hyprial.kernel import DAEMON_STARTUP_PHASES
from hyprial.kernel import DaemonLaunchResult
from pathlib import Path
from datetime import UTC, datetime
from hyprial.error import codes
from hyprial.kernel import ipc_errors
import json
from hyprial.kernel import lock_exclusive
import os
import shutil
from hyprial.kernel import resolve_node_id
import subprocess
import sys
import threading

from hyprial.shell.impl.cli.commands.common.support import JsonObject, CliError
_SAFE_DAEMON_STARTUP_EVENTS = frozenset(
    {
        # The two halves of startup recovery, and they travel together:
        # adapters are the Lark specs, harnesses are everything else. Listing
        # one without the other makes the launch summary report that adapters
        # came back while saying nothing about the connectors -- which is the
        # shape of the outage that made this event exist.
        "adapter.recovery.completed",
        "harness.recovery.completed",
        # ...and the failure names of the same story. This allowlist was born
        # success-only, so a daemon that died at the ipc-server step reported
        # `daemonEvents: []` -- byte-identical to a daemon that never logged.
        # The daemon now mirrors these two to stderr as JSON envelopes (see
        # `_mirror_startup_event_to_stderr`), and the summary's name-only
        # filter admits exactly these names: level alone never admits an
        # event, so the set stays closed.
        "daemon.start.failed",
        # A service-managed daemon that failed to start waits before exiting
        # so launchd/systemd cannot hot-loop it; the event names that wait
        # (attempt, backoffMs) next to the failure it follows.
        "daemon.start.backoff",
        # One persisted interactive session can conflict with an already-live
        # connector during actor-runtime recovery.  The daemon stays ready,
        # while this event names the isolated agent and the refusal reason.
        "agent.recovery.failed",
        "agent.recovery.cleaned",
        "harness.recovery.failed",
        "restore-policy-degraded",
        "daemon.ready",
        "daemon.stopping",
        "service.recovery.completed",
        # Candidate forwarding starts while `hyprial init` is still waiting for
        # daemon readiness.  These three events name that startup failure;
        # omitting them makes the launch summary indistinguishable from a
        # daemon that emitted no diagnosis at all.
        "zenoh.forwarding.exited",
        "zenoh.forwarding.failed",
        "zenoh.forwarding.start_failed",
        "zenoh.listen.derived_unavailable",
        "zenoh.endpoints.unset",
        "workflow.remote_unavailable",
        # Degraded workflow startup (#708): the daemon stays up with the
        # workflow/routine capabilities disabled, and these name why.  Without
        # them the launch summary shows a healthy start while dispatch is off.
        "workflow.cutover_failed",
        "workflow.recovery_unavailable",
        "workflow.pac_actor_unavailable",
        "pac.graph_authority_unavailable",
        "workflow.pac_gc_unavailable",
        "workflow.degrade_cleanup_failed",
        "routine.recovery_unavailable",
    }
)


_DAEMON_LOG_READ_BYTES = 64 * 1024


def _daemon_startup_phase_summary(
    state_dir: Path, *, spawned_at: datetime
) -> JsonObject:
    """Read only this launch's bounded startup phase names from daemon.jsonl."""

    result: JsonObject = {"lastStartupPhase": None, "phasesSeen": 0}
    path = state_dir / "logs" / "daemon.jsonl"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            start = max(0, end - _DAEMON_LOG_READ_BYTES)
            stream.seek(start)
            raw = stream.read(_DAEMON_LOG_READ_BYTES)
        if start:
            _, separator, raw = raw.partition(b"\n")
            if not separator:
                return result
    except OSError:
        return result

    phases_seen = 0
    last_phase: str | None = None
    for raw_line in raw.splitlines():
        try:
            entry = json.loads(raw_line)
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        if not isinstance(entry, dict) or entry.get("event") != "daemon.start.begin":
            continue
        timestamp = entry.get("ts")
        if not isinstance(timestamp, str):
            continue
        try:
            observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if observed_at.tzinfo is None or observed_at < spawned_at:
            continue
        # Saturation keeps even corrupted or adversarial logs finite while
        # preserving the useful distinction between none, some, and more than
        # the daemon's closed set of phases.
        phases_seen = min(phases_seen + 1, len(DAEMON_STARTUP_PHASES) + 1)
        phase = entry.get("phase")
        last_phase = (
            phase
            if isinstance(phase, str) and phase in DAEMON_STARTUP_PHASES
            else "unknown"
        )
    result["lastStartupPhase"] = last_phase
    result["phasesSeen"] = phases_seen
    return result


def _daemon_startup_failure_evidence(
    process: subprocess.Popen[bytes],
    *,
    state_dir: Path,
    spawned_at: datetime,
    started_monotonic: float,
) -> JsonObject:
    """Take one post-wait snapshot for a failed daemon launch."""
    services = get_services()

    status = process.poll()
    alive = status is None
    # One probe only.  process_cpu_seconds uses one /proc read on Linux or one
    # one-second-capped ps invocation on macOS; it never waits on or signals
    # the daemon child and returns None if the child vanishes or probing fails.
    cpu_seconds = services.process_cpu_seconds(process.pid) if alive else None
    phase = _daemon_startup_phase_summary(state_dir, spawned_at=spawned_at)
    return {
        **phase,
        "elapsedSeconds": round(max(0.0, services.time.monotonic() - started_monotonic), 6),
        "child": {
            "alive": alive,
            "exitCode": None if alive else status,
            "cpuSeconds": cpu_seconds,
        },
    }


def _daemon_launch_log_summary(
    stream: Any, *, marker: bytes, offset: int
) -> JsonObject:
    """Classify one launch log without returning attacker-controlled text."""

    unavailable: JsonObject = {
        "logAvailable": False,
        "daemonEvents": [],
        "recentLog": None,
    }

    try:
        stream.flush()
        end = os.fstat(stream.fileno()).st_size
        if end <= offset:
            return unavailable
        stream.seek(0)
        if stream.read(len(marker)) != marker:
            # Truncation or replacement invalidates the birth boundary. Never
            # surface bytes whose ownership can no longer be proven.
            return unavailable
        start = max(offset, end - 16 * 1024)
        stream.seek(start)
        raw = stream.read(end - start)
        if start > offset:
            # The inspection window starts inside an arbitrary record. Only
            # complete records can contribute an allow-listed category.
            _, separator, raw = raw.partition(b"\n")
            if not separator:
                raw = b""
    except (OSError, ValueError):
        return unavailable

    events: list[str] = []
    startup_error: JsonObject | None = None
    for raw_line in raw.splitlines():
        try:
            entry = json.loads(raw_line)
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        if not isinstance(entry, dict):
            continue
        event = entry.get("event")
        if (
            isinstance(event, str)
            and event in _SAFE_DAEMON_STARTUP_EVENTS
            and event not in events
        ):
            events.append(event)
        if entry.get("code") == codes.HYPRIAL_HOME_IN_USE:
            data = entry.get("data")
            if (
                isinstance(data, dict)
                and isinstance(data.get("path"), str)
                and isinstance(data.get("pid"), int)
                and data["pid"] > 0
            ):
                startup_error = {
                    "code": codes.HYPRIAL_HOME_IN_USE,
                    "data": {"path": data["path"], "pid": data["pid"]},
                }
    summary: JsonObject = {
        "logAvailable": True,
        "daemonEvents": events,
        # Additive compatibility field: arbitrary daemon output is never
        # copied into CLI JSON, even after attempted redaction.
        "recentLog": None,
    }
    if startup_error is not None:
        summary["startupError"] = startup_error
    return summary


def _discard_launch_capture(capture_path: Path) -> None:
    """Best-effort removal of a per-launch alias; never a launch failure.

    The daemon child keeps the capture open as its stdout.  An OS that cannot
    delete an open file (Windows: WinError 32) then refuses the unlink for that
    generation's lifetime, after the daemon is already serving.  The
    compatibility path stays a hard link to the same bytes either way, and the
    next locked launch sweeps the leftover alias.  Plain cross-platform code:
    no platform branch, the refusal itself is the signal.
    """
    try:
        capture_path.unlink(missing_ok=True)
    except OSError:
        pass


def _sweep_stale_launch_captures(state_dir: Path) -> None:
    """Drop aliases left by earlier launches (caller holds daemon-launch.lock).

    Only the serialized launch transaction may reclaim them: the capture
    helper itself has concurrent callers whose live aliases must survive.  An
    alias an older daemon still holds open is skipped and retried next time.
    """
    for stale in state_dir.glob(".daemon-launch.*.log"):
        _discard_launch_capture(stale)


def _daemon_launch_capture(state_dir: Path) -> tuple[Any, Path, bytes, int]:
    """Create a per-launch inode and atomically expose its compatibility path."""
    services = get_services()

    nonce = services.uuid4().hex
    capture_path = state_dir / f".daemon-launch.{nonce}.log"
    compatibility_path = state_dir / "daemon-launch.log"
    temporary_link = state_dir / f".daemon-launch-link.{nonce}"
    stream = capture_path.open("x+b", buffering=0)
    harden_private_path(capture_path, mode=0o600)
    marker = f"hyprial-launch-boundary:{nonce}\n".encode()
    try:
        stream.write(marker)
        offset = stream.tell()
        os.link(capture_path, temporary_link)
        os.replace(temporary_link, compatibility_path)
    except BaseException:
        stream.close()
        _discard_launch_capture(temporary_link)
        _discard_launch_capture(capture_path)
        raise
    return stream, capture_path, marker, offset


def _launch_process_error(
    error: CliError,
    process: subprocess.Popen[bytes],
    process_identity: str | None,
) -> CliError:
    """Keep the child birth handle off the public CLI error payload."""

    setattr(error, "_daemon_process_pid", process.pid)
    setattr(error, "_daemon_process_identity", process_identity)
    setattr(error, "_daemon_process_exited", process.poll() is not None)
    return error


def _daemon_autostart_disabled() -> bool:
    return os.environ.get("HYPRIAL_DAEMON_AUTOSTART") == "0"


def _service_autoinstall_env_allowed() -> bool:
    return os.environ.get("HYPRIAL_SERVICE_AUTOINSTALL") != "0"


def _home_matches_account_home() -> bool:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return False
    try:
        import pwd
    except ImportError:
        return False
    return Path.home().resolve() == Path(pwd.getpwuid(getuid()).pw_dir).resolve()


def _program_path_install_safe() -> bool:
    candidate = shutil.which("hyprial") or sys.argv[0]
    resolved = str(Path(candidate).expanduser().resolve())
    return "/AppTranslocation/" not in resolved and "/AppTranslocation/" not in str(candidate)


_AF_UNIX_PROBE_DIRECTORY = "/hyprial-af-unix-limit-probe-nonexistent/"


def _af_unix_path_limit() -> int | None:
    """The longest AF_UNIX path (bytes) this platform accepts, from the seam.

    The bind-probe itself lives in the kernel local IPC seam; this name stays
    as the CLI's own budget vocabulary (and test seam). ``None`` means the
    transport reports no AF_UNIX path limit.
    """
    from hyprial.kernel import local_ipc_path_limit

    return local_ipc_path_limit()


def _require_socket_path_fits(services: Any) -> None:
    socket_path = services._socket_path()
    length = len(os.fsencode(str(socket_path)))
    if length <= len(_AF_UNIX_PROBE_DIRECTORY) + 1:
        return
    limit = _af_unix_path_limit()
    if limit is None or length <= limit:
        return
    raise services.CliError(
        codes.DAEMON_START_FAILED,
        f"the daemon socket path is {length} bytes, over this platform's AF_UNIX "
        f"limit of {limit}: {socket_path}. Use a shorter HYPRIAL_HOME / "
        "HARNESS_STATE_DIR.",
        {"socketPath": str(socket_path), "length": length, "limit": limit},
    )


def _attempt_service_autoinstall(
    services: Any,
) -> tuple[tuple[Any, Any], Any, Any] | None:
    """Install the per-user unit once through the manager, then re-derive it.

    Z-8c (2026-11-15, workflow brief + OD-Z2): every manager failure is
    non-fatal by design. Ownership refusal, manager command failure, a
    missing manager command and an unsupported platform all surface as
    ``RuntimeError`` shapes from the service backend, so exactly one stderr
    notice names the failure and the manual retry, and the caller falls back
    to the detached spawn so the daemon still starts. The suite reaches this
    path only through fake runners/managers (no test relies on the backend's
    HOME guard as its only protection), and the test fixtures pin both
    opt-out variables to "0" so ambient exports cannot turn auto-install on
    for a test run. Product code never writes either variable.

    Z-8 launchd acceptance fix D1 (2026-11-16): the unit state before the
    install is returned with the supervisor so the caller can revert a
    first supervised start that fails (``_revert_autoinstall``); a unit that
    cannot start must not stay loaded and crash-loop behind the fallback.
    """
    from hyprial.shell.impl.cli.commands.daemon.service import (
        _service_manager,
        installed_supervisor,
    )

    try:
        manager = _service_manager()
        state = manager.capture_unit_state()
        status = manager.install()
    except RuntimeError as error:
        reason = (
            getattr(error, "reason", None)
            or getattr(error, "platform", None)
            or type(error).__name__
        )
        notice(
            f"daemon service auto-install failed ({reason}): {error}. "
            "The daemon starts detached; register the service explicitly "
            "with `hyprial daemon service install`."
        )
        return None
    notice(
        f"installed the per-user daemon service ({status.unit}); remove it "
        "with `hyprial daemon service uninstall`."
    )
    supervisor = installed_supervisor()
    if supervisor is None:
        return None
    return supervisor, manager, state


def _revert_autoinstall(
    services: Any, manager: Any, state: Any, cause: BaseException
) -> None:
    """Undo an auto-install whose first supervised start failed, then fall back."""
    try:
        reverted = manager.revert_install(state)
    except RuntimeError as error:
        raise services.CliError(
            "SERVICE_SUPERVISION_LOST",
            f"the auto-installed daemon service did not start ({cause}) and "
            f"could not be reverted ({error}); run "
            "`hyprial daemon service uninstall`",
            {"install": {"supervision": "lost", "reverted": "failed"}},
        ) from error
    notice(
        f"the auto-installed daemon service did not start ({cause}); "
        f"reverted ({reverted}). The daemon starts detached."
    )


def _launch_daemon_process(
    *,
    ready_timeout: float,
    listen: str | None = None,
    connect: str | None = None,
    expected_identity_generation: int | None = None,
    implicit: bool = False,
    autoinstall: bool = True,
) -> DaemonLaunchResult:
    """Start the daemon once: through the installed supervisor, else spawn it.

    No identity lock is held or handed over here.  The daemon acquires the
    home identity-transaction lock itself before reading identity; a login
    passes ``expected_identity_generation`` so a daemon that lost the gap to a
    later login exits stale instead of serving the superseded owner.

    With an attributable per-user service installed (launchd/systemd) the
    supervisor is the only launcher: a CLI spawn next to ``KeepAlive`` /
    ``Restart=always`` would race it for the locks.  The delegated locked body
    preserves the one-launch endpoint overrides:
    ``environment["HYPRIAL_ZENOH_LISTEN"] = listen`` and
    ``environment["HYPRIAL_ZENOH_CONNECT"] = connect``.  It also derives the
    child variables with ``daemon_forwarding_environment(...)`` and then
    ``environment.update(forwarding_environment)`` before spawn.

    Z-8c start order (design §3.8, workflow brief 2026-11-15):
    ``HYPRIAL_DAEMON_AUTOSTART=0`` gates every manager-mediated start. With
    the opt-out an implicit caller is refused with
    ``DAEMON_AUTOSTART_DISABLED`` and an explicit caller bypasses the manager
    entirely -- installed supervisor included -- and takes the detached
    spawn, so pinning the variable suite-wide can neither install units nor
    turn ``daemon start`` into a silent no-op. Without the opt-out an
    attributable installed unit is the launcher as before (installing is
    never involved there; the unit was explicitly installed). Otherwise,
    when ``autoinstall=True`` (a caller whose launch must not change
    supervision state -- ``pending_restart`` -- passes ``autoinstall=False``),
    no endpoints are requested (they keep their CLI-spawn-only meaning),
    ``HYPRIAL_SERVICE_AUTOINSTALL!=0``, the operating HOME is the OS account
    home, and the program path is stable (not AppTranslocated), the primitive
    installs the unit once through the manager and starts supervised; any
    install failure is non-fatal (one notice, detached fallback), and so is a
    first supervised start that fails: the install is reverted through the
    manager (unit unloaded and removed, or the previous unit restored) before
    the detached fallback, so no crash-looping unit is left behind. Product
    code never writes either opt-out variable.

    Z-8 launchd acceptance fix F4 (2026-11-16): the socket path is checked
    against the platform's AF_UNIX limit before anything is installed or
    spawned, so an over-long state directory is named instead of surfacing as
    a generic ``DAEMON_START_FAILED`` from the IPC server stage. The code is
    the existing one; only the message and data are new.
    """
    services = get_services()
    _require_socket_path_fits(services)
    from hyprial.shell.impl.cli.commands.daemon.service import (
        installed_supervisor,
        start_supervised_daemon,
    )

    autostart_disabled = _daemon_autostart_disabled()
    supervisor = None if autostart_disabled else installed_supervisor()
    if supervisor is not None:
        return start_supervised_daemon(
            supervisor,
            idle_budget=ready_timeout,
            endpoints_requested=listen is not None or connect is not None,
            startup_progress=_daemon_startup_phase_summary,
        )
    if implicit and autostart_disabled:
        raise services.CliError(
            "DAEMON_AUTOSTART_DISABLED",
            "implicit daemon autostart is disabled (HYPRIAL_DAEMON_AUTOSTART=0); "
            "start the daemon explicitly with `hyprial daemon start`",
            {"env": "HYPRIAL_DAEMON_AUTOSTART"},
        )
    if (
        autoinstall
        and not autostart_disabled
        and listen is None
        and connect is None
        and _service_autoinstall_env_allowed()
        and _home_matches_account_home()
        and _program_path_install_safe()
    ):
        attempt = _attempt_service_autoinstall(services)
        if attempt is not None:
            supervisor, manager, state = attempt
            try:
                return start_supervised_daemon(
                    supervisor,
                    idle_budget=ready_timeout,
                    endpoints_requested=False,
                    startup_progress=_daemon_startup_phase_summary,
                )
            except CliError as error:
                _revert_autoinstall(services, manager, state, error)
    return services._launch_daemon_process_locked(
        ready_timeout=ready_timeout,
        listen=listen,
        connect=connect,
        expected_identity_generation=expected_identity_generation,
    )


def _launch_daemon_process_locked(
    *,
    ready_timeout: float,
    listen: str | None,
    connect: str | None,
    expected_identity_generation: int | None = None,
) -> DaemonLaunchResult:
    """Start ``daemon run`` detached from the invoking command's pipes.

    The child writes only to a daemon-owned regular file and starts a new
    session.  Keeping this as the one launch primitive lets ``init`` and the
    post-upgrade restart share the macOS-safe path instead of spawning a
    captured ``hyprial init`` whose child could inherit a short-lived pipe.

    Returns the typed ``DaemonLaunchResult`` (F2): ``init``, ``upgrade`` and
    the e2e runner all read that one type; the flat JSON form it carries is
    what ``hyprial init --json`` emits.
    """
    from hyprial.daemon import ForwardingConfigurationError, daemon_forwarding_environment
    from hyprial.shell.impl.login.generation import (
        IDENTITY_GENERATION_STALE_EXIT_CODE,
        set_launch_generation,
        set_launch_id,
    )
    services = get_services()

    state_dir = services._state_dir()
    prepare_private_state_dir(state_dir)
    log_path = state_dir / "daemon-launch.log"
    lock_path = state_dir / "daemon-launch.lock"
    with lock_path.open("a+b") as launch_lock:
        harden_private_path(lock_path, mode=0o600)
        lock_exclusive(launch_lock.fileno())
        # Two launchers can both miss an optimistic readiness probe.  Recheck
        # under the lock so only one daemon generation is created.
        try:
            status = services._daemon_probe(timeout=0.5)
        except ipc_errors.DaemonUnavailableError:
            pass
        else:
            if listen is not None or connect is not None:
                raise services.CliError(
                    "DAEMON_ALREADY_RUNNING",
                    "stop the running daemon before changing zenoh endpoints",
                )
            return DaemonLaunchResult.existing(status)

        # Endpoints are NOT persisted (Allen 2026-08-28). A machine's own
        # address is not hyprial's to remember: written to desired-state it
        # survives the machine changing address, silently, which is the
        # constant we just deleted from the onboarding document relocated
        # into every node's own state file.
        #
        # Durable configuration belongs in whatever launches the daemon --
        # launchd unit, systemd, shell -- via HYPRIAL_ZENOH_LISTEN /
        # HYPRIAL_ZENOH_CONNECT, which are re-read every start and therefore
        # cannot go stale.
        if listen is not None or connect is not None:
            notice(
                "note: --listen/--connect apply to THIS launch only and are "
                "no longer persisted. To make them durable, set "
                "HYPRIAL_ZENOH_LISTEN / HYPRIAL_ZENOH_CONNECT in the environment that "
                "starts the daemon (launchd/systemd/shell) -- that is where "
                "machine-specific configuration belongs, and it is re-read "
                "on every start rather than remembered and going stale."
            )

        # Only this locked launch transaction may reclaim earlier aliases.
        _sweep_stale_launch_captures(state_dir)
        log_stream, capture_path, marker, launch_log_offset = _daemon_launch_capture(
            state_dir
        )
        try:
            environment = dict(os.environ)
            # --listen/--connect are sugar for a one-launch environment
            # override, which is the whole of their meaning now that nothing
            # is persisted: the daemon child inherits this environment, and
            # `_zenoh_endpoints` reads exactly these two variables.
            #
            # Without this the flags would be silently inert -- desired-state
            # used to be their only route to the daemon, so removing
            # persistence alone turns them into a no-op that still prints a
            # reassuring note.
            if listen is not None:
                environment["HYPRIAL_ZENOH_LISTEN"] = listen
            if connect is not None:
                environment["HYPRIAL_ZENOH_CONNECT"] = connect
            try:
                forwarding_environment = daemon_forwarding_environment(
                    services._hyprial_home(),
                    environment,
                    node_id=resolve_node_id(environment),
                )
            except ForwardingConfigurationError as error:
                raise services.CliError(error.code, str(error)) from error
            environment.update(forwarding_environment)
            # These markers select the scheduler child path only.  A restarted
            # daemon inherits every operator variable (especially PATH), but
            # must not mistake itself for the one-shot updater child.
            from hyprial.daemon import (
                AUTOUPDATE_CHILD_ENV,
                AUTOUPDATE_TRIGGER_ENV,
            )

            environment.pop(AUTOUPDATE_CHILD_ENV, None)
            environment.pop(AUTOUPDATE_TRIGGER_ENV, None)
            set_launch_generation(environment, expected_identity_generation)
            # This launch's identity for the readiness fence, on every
            # platform: ``Popen.pid`` is not the daemon's pid behind a venv /
            # uv ``python.exe`` redirector (Windows), so the child records the
            # nonce in daemon.json instead.  Never forward an inherited one.
            launch_id = services.uuid4().hex
            set_launch_id(environment, launch_id)
            spawned_at = datetime.now(UTC)
            started_monotonic = services.time.monotonic()
            process = spawn_detached_daemon(
                [sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"],
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                close_fds=True,
                env=environment,
            )
            _reap_when_it_exits(process)
            try:
                status = services._wait_for_daemon(
                    timeout=ready_timeout,
                    process=process,
                    launch_id=launch_id,
                    startup_progress=lambda: _daemon_startup_phase_summary(
                        state_dir, spawned_at=spawned_at
                    ),
                )
            except services.CliError as error:
                # Capture the birth fence only after the readiness wait ends.
                # Reading it before the wait could add a bounded ps probe in
                # front of the idle budget and consume silence headroom.
                from hyprial.daemon import read_process_identity as _read_process_identity

                process_identity = _read_process_identity(process.pid)
                if error.code not in {
                    codes.DAEMON_START_FAILED,
                    codes.DAEMON_START_TIMEOUT,
                }:
                    raise
                stale_exit = getattr(process, "returncode", None)
                if stale_exit == IDENTITY_GENERATION_STALE_EXIT_CODE:
                    raise _launch_process_error(
                        services.CliError(
                            "IDENTITY_SUPERSEDED",
                            "the launched daemon found a newer identity "
                            "generation than the one this command committed "
                            f"({expected_identity_generation}) and exited; a "
                            "later login superseded it",
                            {
                                "phase": "startup",
                                "errorType": "identitySuperseded",
                                "exitCode": stale_exit,
                                "expectedIdentityGeneration": (
                                    expected_identity_generation
                                ),
                                "logPath": str(log_path),
                            },
                        ),
                        process,
                        process_identity,
                    ) from error
                diagnostics = _daemon_launch_log_summary(
                    log_stream,
                    marker=marker,
                    offset=launch_log_offset,
                )
                startup_evidence = _daemon_startup_failure_evidence(
                    process,
                    state_dir=state_dir,
                    spawned_at=spawned_at,
                    started_monotonic=started_monotonic,
                )
                startup_error = diagnostics.pop("startupError", None)
                if (
                    isinstance(startup_error, dict)
                    and startup_error.get("code") == codes.HYPRIAL_HOME_IN_USE
                ):
                    data = startup_error.get("data")
                    if isinstance(data, dict):
                        path = data.get("path")
                        pid = data.get("pid")
                        if isinstance(path, str) and isinstance(pid, int):
                            launch_error = services.CliError(
                                codes.HYPRIAL_HOME_IN_USE,
                                f"HYPRIAL home {path} is being used by another "
                                f"daemon (pid {pid})",
                                data,
                            )
                            raise _launch_process_error(
                                launch_error, process, process_identity
                            ) from error
                if (
                    error.code == codes.DAEMON_START_TIMEOUT
                    and startup_evidence["child"]["alive"] is True
                ):
                    # ① never arrived: the process is alive but did not bind
                    # and answer within the budget.  That is a startup
                    # failure, not a slow restore -- restore is outside this
                    # wait by construction.  The process is still left alone:
                    # killing what we cannot explain is how survivors are
                    # made, so the pid goes into the report instead.
                    launch_error = services.CliError(
                        error.code,
                        f"{error}; the daemon process (pid {process.pid}) "
                        "was still running when the wait ended",
                        {
                            "phase": "startup",
                            "errorType": "servingTimeout",
                            "pid": process.pid,
                            "logPath": str(log_path),
                            **diagnostics,
                            **startup_evidence,
                        },
                    )
                    raise _launch_process_error(
                        launch_error, process, process_identity
                    ) from error
                launch_error = services.CliError(
                    error.code,
                    str(error),
                    {
                        "phase": "startup",
                        "errorType": "processExit",
                        "exitCode": process.returncode,
                        "logPath": str(log_path),
                        **diagnostics,
                        **startup_evidence,
                    },
                )
                raise _launch_process_error(
                    launch_error, process, process_identity
                ) from error
        finally:
            log_stream.close()
            _discard_launch_capture(capture_path)

        # The readiness answer (``status``) is the daemon's flat ping
        # payload; the old code spread it by hand and carried a `zenoh`
        # override that was provably dead (the variable was only ever
        # None here) -- the type builds the same flat JSON form without
        # either.
        return DaemonLaunchResult.launched(status)


def _reap_when_it_exits(process: subprocess.Popen[bytes]) -> None:
    """Wait for the daemon this process started, so its exit leaves no zombie.

    Decision: daemon-first-z6c-spawner-reaps-20261118.  The daemon is started
    detached (its own session) and outlives the command that started it.  When
    that command is long lived (``hyprial gui``) nothing ever waited for the
    child, so after a stop the exited daemon stayed a zombie of the launcher, its
    pid and birth identity still read, and a stop or logout that proves the exit
    ended in ``DAEMON_STOP_SURVIVOR``.  A daemon thread blocked in ``wait()``
    reaps it on every platform; it never signals or stops the daemon, never keeps
    the launcher alive at exit (a short-lived command simply exits and init
    reaps), and ``subprocess`` serializes it with the readiness wait's ``poll``.
    """

    def wait() -> None:
        try:
            process.wait()
        except Exception:
            return

    threading.Thread(target=wait, name="daemon-reaper", daemon=True).start()
