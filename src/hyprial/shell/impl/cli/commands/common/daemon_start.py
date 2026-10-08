"""Daemon process lifecycle: launch, startup evidence, stop/teardown receipts."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import notice

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # deferred annotations only
    from hyprial.identity import IdentityTransactionLock

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import DAEMON_STARTUP_PHASES
from hyprial.kernel import DaemonLaunchResult
from pathlib import Path
from datetime import UTC, datetime
from hyprial.kernel import ipc_errors
import json
from hyprial.kernel import lock_exclusive
import os
from hyprial.kernel import resolve_node_id
import subprocess
import sys

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


_CUSTODY_STARTUP_ERROR_SHAPES: dict[str, tuple[tuple[str, type], ...]] = {
    ipc_errors.OWNER_MIGRATION_HOSTED_CONFLICT: (
        ("old", str),
        ("new", str),
        ("actor", str),
        ("hostedOwner", str),
    ),
    ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT: (
        ("old", str),
        ("new", str),
        ("grants", int),
        ("homes", int),
    ),
    ipc_errors.OWNER_MIGRATION_CUSTODY_UNREADABLE: (
        ("database", str),
        ("table", str),
        ("errorType", str),
    ),
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
        if entry.get("code") == ipc_errors.HYPRIAL_HOME_IN_USE:
            data = entry.get("data")
            if (
                isinstance(data, dict)
                and isinstance(data.get("path"), str)
                and isinstance(data.get("pid"), int)
                and data["pid"] > 0
            ):
                startup_error = {
                    "code": ipc_errors.HYPRIAL_HOME_IN_USE,
                    "data": {"path": data["path"], "pid": data["pid"]},
                }
        # The startup owner-migration custody gate refuses with a named
        # code (#513): the daemon child's ``--json`` failure line carries
        # it, and the launcher / login orchestration branch on it to report
        # the named switch outcome.  Only the bounded, daemon-minted fields
        # named in the shape table are copied — never free text from the log.
        shape = _CUSTODY_STARTUP_ERROR_SHAPES.get(entry.get("code"))
        if shape is not None:
            data = entry.get("data")
            if isinstance(data, dict) and all(
                isinstance(data.get(name), kind) and not isinstance(
                    data.get(name), bool
                )
                for name, kind in shape
            ):
                startup_error = {
                    "code": entry["code"],
                    "data": {name: data[name] for name, _kind in shape},
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


def _daemon_launch_capture(state_dir: Path) -> tuple[Any, Path, bytes, int]:
    """Create a per-launch inode and atomically expose its compatibility path."""
    services = get_services()

    nonce = services.uuid4().hex
    capture_path = state_dir / f".daemon-launch.{nonce}.log"
    compatibility_path = state_dir / "daemon-launch.log"
    temporary_link = state_dir / f".daemon-launch-link.{nonce}"
    stream = capture_path.open("x+b", buffering=0)
    os.chmod(capture_path, 0o600)
    marker = f"hyprial-launch-boundary:{nonce}\n".encode()
    try:
        stream.write(marker)
        offset = stream.tell()
        os.link(capture_path, temporary_link)
        os.replace(temporary_link, compatibility_path)
    except BaseException:
        stream.close()
        temporary_link.unlink(missing_ok=True)
        capture_path.unlink(missing_ok=True)
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


def _launch_daemon_process(
    *,
    ready_timeout: float,
    listen: str | None = None,
    connect: str | None = None,
    identity_transaction: IdentityTransactionLock | None = None,
) -> DaemonLaunchResult:
    """Probe and spawn under the home identity transaction OS lock.

    The delegated locked body preserves the one-launch endpoint overrides:
    ``environment["HYPRIAL_ZENOH_LISTEN"] = listen`` and
    ``environment["HYPRIAL_ZENOH_CONNECT"] = connect``.  It also derives the
    child variables with ``daemon_forwarding_environment(...)`` and then
    ``environment.update(forwarding_environment)`` before spawn.
    """
    from hyprial.identity import IdentityTransactionBusy, IdentityTransactionLock
    services = get_services()

    if identity_transaction is not None:
        return services._launch_daemon_process_locked(
            ready_timeout=ready_timeout,
            listen=listen,
            connect=connect,
            identity_transaction=identity_transaction,
        )
    try:
        transaction = IdentityTransactionLock.acquire(services._hyprial_home())
    except IdentityTransactionBusy as error:
        raise services.CliError(error.code, str(error)) from error
    with transaction:
        return services._launch_daemon_process_locked(
            ready_timeout=ready_timeout,
            listen=listen,
            connect=connect,
            identity_transaction=transaction,
        )


def _launch_daemon_process_locked(
    *,
    ready_timeout: float,
    listen: str | None,
    connect: str | None,
    identity_transaction: IdentityTransactionLock,
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
    from hyprial.identity import IDENTITY_TRANSACTION_FD_ENV
    services = get_services()

    state_dir = services._state_dir()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = state_dir / "daemon-launch.log"
    lock_path = state_dir / "daemon-launch.lock"
    with lock_path.open("a+b") as launch_lock:
        os.chmod(lock_path, 0o600)
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
            environment[IDENTITY_TRANSACTION_FD_ENV] = str(
                identity_transaction.fileno
            )
            spawned_at = datetime.now(UTC)
            started_monotonic = services.time.monotonic()
            process = subprocess.Popen(
                [sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"],
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                close_fds=True,
                pass_fds=(identity_transaction.fileno,),
                start_new_session=True,
                env=environment,
            )
            try:
                status = services._wait_for_daemon(
                    timeout=ready_timeout,
                    process=process,
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
                    ipc_errors.DAEMON_START_FAILED,
                    ipc_errors.DAEMON_START_TIMEOUT,
                }:
                    raise
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
                    and startup_error.get("code") == ipc_errors.HYPRIAL_HOME_IN_USE
                ):
                    data = startup_error.get("data")
                    if isinstance(data, dict):
                        path = data.get("path")
                        pid = data.get("pid")
                        if isinstance(path, str) and isinstance(pid, int):
                            launch_error = services.CliError(
                                ipc_errors.HYPRIAL_HOME_IN_USE,
                                f"HYPRIAL home {path} is being used by another "
                                f"daemon (pid {pid})",
                                data,
                            )
                            raise _launch_process_error(
                                launch_error, process, process_identity
                            ) from error
                if (
                    isinstance(startup_error, dict)
                    and startup_error.get("code")
                    in _CUSTODY_STARTUP_ERROR_SHAPES
                ):
                    # #513: the daemon refused to start at the owner-migration
                    # custody gate.  The named code crosses the boundary so the
                    # login orchestration reports the named switch outcome; the
                    # operator-facing full way out stays where the daemon wrote
                    # it — daemon-launch.log, which outlives the capture.
                    custody_code = str(startup_error["code"])
                    custody_data = startup_error.get("data")
                    bounded = (
                        dict(custody_data) if isinstance(custody_data, dict) else {}
                    )
                    if custody_code == ipc_errors.OWNER_MIGRATION_HOSTED_CONFLICT:
                        from hyprial.daemon import OwnerMigrationHostedConflict

                        refusal = OwnerMigrationHostedConflict(
                            old=str(bounded["old"]), new=str(bounded["new"]),
                            actor=str(bounded["actor"]),
                            hosted_owner=str(bounded["hostedOwner"]),
                        )
                        launch_error = services.CliError(
                            custody_code, str(refusal),
                            {
                                "phase": "startup",
                                "errorType": "hostedOwnerConflict",
                                "exitCode": process.returncode,
                                "logPath": str(log_path),
                                **bounded,
                                **diagnostics,
                            },
                        )
                    elif custody_code == ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT:
                        launch_error = services.CliError(
                            custody_code,
                            "daemon refused to start: owner migration custody "
                            "conflict — state under "
                            f"{bounded.get('old')!r} holds "
                            f"{bounded.get('grants')} live secret grant(s); the "
                            "daemon's refusal message in daemon-launch.log "
                            "names the full way out",
                            {
                                "phase": "startup",
                                "errorType": "custodyRefusal",
                                "exitCode": process.returncode,
                                "logPath": str(log_path),
                                **bounded,
                                **diagnostics,
                            },
                        )
                    else:
                        launch_error = services.CliError(
                            custody_code,
                            "daemon refused to start: owner-migration custody "
                            "state unreadable "
                            f"({bounded.get('table')} in {bounded.get('database')}: "
                            f"{bounded.get('errorType')}); the daemon's refusal "
                            "message in daemon-launch.log names the way out",
                            {
                                "phase": "startup",
                                "errorType": "custodyRefusal",
                                "exitCode": process.returncode,
                                "logPath": str(log_path),
                                **bounded,
                                **diagnostics,
                            },
                        )
                    raise _launch_process_error(
                        launch_error, process, process_identity
                    ) from error
                if (
                    error.code == ipc_errors.DAEMON_START_TIMEOUT
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
            capture_path.unlink(missing_ok=True)

        # The readiness answer (``status``) is the daemon's flat ping
        # payload; the old code spread it by hand and carried a `zenoh`
        # override that was provably dead (the variable was only ever
        # None here) -- the type builds the same flat JSON form without
        # either.
        return DaemonLaunchResult.launched(status)
