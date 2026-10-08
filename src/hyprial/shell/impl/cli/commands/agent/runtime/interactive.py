"""Shared interactive-session launch runtime (context, custody, TUI)."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import warn

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from collections.abc import Callable, Sequence
from pathlib import Path
from contextlib import contextmanager
from hyprial.kernel import ipc_errors, resolve_node_id
import json
import os
import re
import signal
import subprocess

from hyprial.shell.impl.cli.commands.common.support import JsonObject, _append_warning
def _plugin_skip_warnings(skipped: Sequence[Any]) -> list[JsonObject]:
    return [
        {
            "code": "PLUGIN_SKIPPED",
            "message": f"plugin {item.name!r} was not loaded: {item.reason}",
            "data": {"plugin": item.name, "reason": item.reason},
        }
        for item in skipped
    ]


def _announce_plugin_skips(warnings: Sequence[JsonObject]) -> None:
    for warning in warnings:
        warn(f"hyprial: warning: {warning['message']}", json_output=False)


def _with_plugin_warnings(
    result: JsonObject, warnings: Sequence[JsonObject]
) -> JsonObject:
    for warning in warnings:
        _append_warning(result, warning)
    return result


def _wait_foreground(process: subprocess.Popen[Any]) -> int:
    """Wait for the interactive TUI while leaving Ctrl-C to the child.

    The child shares this process's terminal and process group, so SIGINT
    already reaches the TUI directly. Ignoring SIGINT here keeps a turn
    interrupt inside Claude from tearing the whole session down through this
    supervisor's error path.
    """

    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return process.wait()
    finally:
        signal.signal(signal.SIGINT, previous)


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _interactive_actor(name: str, status: JsonObject) -> str:
    """Mint the canonical ``agent:<owner>:<machine>:<actor>`` for ``--name``.

    Pins, harnesses, and delivery addressing all speak the four-segment
    URI, and launch-time code that must *compare* against daemon truth
    (registration polling, tmux session names, result payloads) needs the
    minted spelling. The daemon's ``ps`` response is the source of truth for
    owner and node id, with the daemon's own fallbacks
    (``HYPRIAL_OWNER``/login, ``HYPRIAL_NODE_ID``/hostname) mirrored for older
    daemons that do not report them yet. A name that already is a
    four-segment URI passes through; any other colon form is rejected
    loudly instead of registering a third spelling.

    ⛔ Never persist this value: a minted address is derived state, and
    freezing it into an mcp-config or argv is what stranded still-running
    channels on a dead owner segment after the #352 owner switch. Anything
    that outlives the launch must carry the bare name
    (:func:`_interactive_actor_name`) and let the daemon mint per call.
    """
    services = get_services()

    if ":" not in name:
        from hyprial.daemon import resolve_node_owner
        from hyprial.kernel import canonical_agent_uri

        daemon = status.get("daemon") if isinstance(status, dict) else None
        node_id = ""
        owner = ""
        if isinstance(daemon, dict):
            node_id = str(daemon.get("nodeId") or "").strip()
            owner = str(daemon.get("owner") or "").strip()
        if not node_id:
            node_id = resolve_node_id()
        if not owner:
            owner = resolve_node_owner()
        return canonical_agent_uri(owner, node_id, name)
    from hyprial.kernel import agent_uri_actor

    if agent_uri_actor(name) is not None:
        return name
    raise services.CliError(
        ipc_errors.INVALID_ARGUMENT,
        f"--name {name!r} is neither a bare actor name nor a canonical "
        "agent:<owner>:<machine>:<actor> URI",
    )


def _interactive_actor_name(name: str) -> str:
    """Reduce ``--name`` to the bare actor name; validation only, never mint.

    The value that outlives a launch -- the mcp-config JSON and the channel
    argv -- is the operator's input, not a derived address: the daemon's
    boundary (``_canonical_interactive_actor``) mints the canonical
    four-segment URI per call from its own current owner/node, so an owner
    change never strands a still-running channel on a stale spelling. A
    four-segment URI still passes validation and reduces to its short name.
    """
    services = get_services()

    if ":" not in name:
        return name
    from hyprial.kernel import agent_uri_actor

    short = agent_uri_actor(name)
    if short is not None:
        return short
    raise services.CliError(
        ipc_errors.INVALID_ARGUMENT,
        f"--name {name!r} is neither a bare actor name nor a canonical "
        "agent:<owner>:<machine>:<actor> URI",
    )


def _handover_prompt(result: JsonObject) -> str | None:
    """The A9 notice to put in front of the agent's first turn, if any."""

    handover = result.get("harnessHandover")
    if not isinstance(handover, dict):
        return None
    notice = handover.get("notice")
    return notice if isinstance(notice, str) and notice else None


def _runtime_context_environment(
    *, name: str, harness: str, cwd: Path, include_projection: bool = False
) -> JsonObject | None:
    projection = _runtime_context_projection(name=name, harness=harness, cwd=cwd)
    if projection is None:
        return None
    return (
        dict(projection)
        if include_projection
        else dict(projection["environment"])
    )


def _runtime_context_projection(
    *, name: str, harness: str, cwd: Path
) -> JsonObject | None:
    """Ask the daemon for one non-secret P2 root/profile projection.

    ``None`` is the explicit legacy mode.  The response is intentionally a
    string map: entity tokens, grants, credential values, and receipts do not
    cross in this projection. Credential values cross only in the later
    acquire reply, and only for the one child protected by its spawn lease.
    An opaque launch token authorizes the final incarnation and grant checks.
    """
    services = get_services()

    result = services._daemon_request(
        "agent.runtime-context",
        {"name": name, "harness": harness, "cwd": str(cwd)},
    )
    if result.get("mode") == "legacy":
        return None
    if result.get("mode") != "agent-home-p2":
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "daemon returned an unsupported agent runtime context mode",
        )
    environment = result.get("environment")
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "daemon returned an invalid agent runtime environment",
        )
    for field in ("projectionRoot", "nativeRoot", "sessionRoot"):
        value = result.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"daemon returned an invalid agent runtime {field}",
            )
    shared_credential = result.get("sharedCredential")
    if shared_credential is not None:
        if not isinstance(shared_credential, dict) or shared_credential.get(
            "authMode"
        ) != "native-shared-link":
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "daemon returned an invalid shared credential binding",
            )
        for field in ("nativePath", "targetPath"):
            value = shared_credential.get(field)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"daemon returned an invalid shared credential {field}",
                )
    return result


@contextmanager
def _runtime_launch_custody(projection: JsonObject | None):
    services = get_services()
    if projection is None or projection.get("authorityPrepared") is not True:
        yield {}
        return
    launch_token = projection.get("launchToken")
    if not isinstance(launch_token, str) or not launch_token:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "authority-prepared runtime context is missing launchToken",
        )
    operation_id = f"cli:runtime-launch:{services.uuid4().hex}"
    acquired = services._daemon_request(
        "agent.runtime-launch.acquire",
        {"launchToken": launch_token, "operationId": operation_id},
    )
    if acquired.get("operationId") != operation_id:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "daemon returned a mismatched Agent runtime launch operation",
        )
    lease_token = acquired.get("leaseToken")
    if not isinstance(lease_token, str) or not lease_token:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "daemon returned an invalid Agent runtime launch lease",
        )
    body_failed = False
    try:
        raw_secret_environment = acquired.get("secretEnvironment", {})
        if not isinstance(raw_secret_environment, dict) or any(
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
            or not isinstance(value, str)
            for name, value in raw_secret_environment.items()
        ):
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "daemon returned an invalid Agent launch secret environment",
            )
        secret_environment = dict(raw_secret_environment)
        yield secret_environment
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            services._daemon_request(
                "agent.runtime-launch.release",
                {"leaseToken": lease_token, "operationId": operation_id},
            )
        except Exception:
            if not body_failed:
                raise


def _channel_registration_confirmed(
    actor: str, session_ref: str, status: JsonObject
) -> bool:
    """Readiness for the Claude Channel carrier: registered AND confirmed."""

    sessions = status.get("interactiveSessions", [])
    return isinstance(sessions, list) and any(
        isinstance(item, dict)
        and item.get("actor") == actor
        and item.get("sessionRef") == session_ref
        and item.get("channelConfirmed") is True
        for item in sessions
    )


def _pi_attach_registration(actor: str, session_ref: str, status: JsonObject) -> bool:
    """Readiness for the pi attach carrier: its session.register has landed."""

    sessions = status.get("interactiveSessions", [])
    return isinstance(sessions, list) and any(
        isinstance(item, dict)
        and item.get("actor") == actor
        and item.get("sessionRef") == session_ref
        and item.get("source") == "pi-extension"
        for item in sessions
    )


def _write_launch_config(config_path: Path, config: JsonObject) -> None:
    """Write the launch-scoped mcp config, mode 0600, refusing overwrites."""

    descriptor = os.open(
        config_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _launch_detached_tui(
    *,
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    actor: str,
    session_ref: str,
    session_name: str,
    harness: str,
    registration_deadline_seconds: float,
    is_session_registered: Callable[[JsonObject], bool],
    registration_failure: str,
    config: JsonObject | None = None,
    config_path: Path | None = None,
    recovery_path: Path | None = None,
    warnings: Sequence[JsonObject] = (),
) -> JsonObject:
    """Spawn an interactive TUI inside a detached tmux session, then return.

    Unlike the foreground path this launcher does NOT wait for the TUI: the
    tmux server owns the pane, attach/detach never signals it, and an
    optional launch-scoped mcp config stays on disk so an in-session MCP
    reconnect can still read it after this process exits. Registration is
    still the readiness gate: a session that never registers is killed again,
    exactly like the foreground spawn-failure path. ``harness`` decides the
    registration predicate (Claude: channelConfirmed; pi: the extension's
    session.register) and the deadline (pi's trust prompt stalls longer).
    """
    services = get_services()

    from hyprial.daemon import harnesses_tmux as tmux_mod

    tmux_bin = tmux_mod.find_tmux()
    if tmux_bin is None:
        if config_path is not None:
            from hyprial.daemon import cleanup_launch_resources

            cleanup_launch_resources(config_path, recovery_path)
        raise services.CliError(
            "TMUX_UNAVAILABLE",
            "--tmux requires the tmux binary on PATH",
        )
    if tmux_mod.has_session(tmux_bin, session_name):
        if config_path is not None:
            from hyprial.daemon import cleanup_launch_resources

            cleanup_launch_resources(config_path, recovery_path)
        hints = tmux_mod.attach_hints(session_name)
        raise services.CliError(
            "TMUX_SESSION_EXISTS",
            f"tmux session {session_name} already exists; attach to it with "
            f"`{hints['wsl-linux-terminal']}` (WSL/Linux) or "
            f"`{hints['macOS-iTerm2']}` (macOS iTerm2), or kill it first",
        )
    if config is not None:
        if config_path is None:
            raise ValueError("config_path is required when config is given")
        try:
            _write_launch_config(config_path, config)
        except BaseException:
            from hyprial.daemon import cleanup_launch_resources

            cleanup_launch_resources(config_path, recovery_path)
            raise
    try:
        cleanup = (
            tmux_mod.LaunchCleanupResources(config_path, recovery_path)
            if config_path is not None
            else None
        )
        tmux_mod.new_detached_session(
            tmux_bin,
            session_name,
            argv,
            cwd=cwd,
            env=env,
            cleanup=cleanup,
        )
    except subprocess.CalledProcessError as error:
        if config_path is not None:
            from hyprial.daemon import cleanup_launch_resources

            cleanup_launch_resources(config_path, recovery_path)
        detail = (error.stderr or "").strip()
        raise services.CliError(
            "TMUX_SPAWN_FAILED",
            f"tmux failed to start detached session {session_name}"
            + (f": {detail}" if detail else ""),
        ) from error
    registered = False
    deadline = services.time.monotonic() + registration_deadline_seconds
    next_pane_read = services.time.monotonic()
    while services.time.monotonic() < deadline:
        registered = is_session_registered(services._daemon_request("ps"))
        # A dead tmux session is this path's process.poll(): the TUI exited
        # before its carrier ever registered.
        if registered or not tmux_mod.has_session(tmux_bin, session_name):
            break
        if services.time.monotonic() >= next_pane_read:
            next_pane_read = services.time.monotonic() + 1.0
            prompt = tmux_mod.claude_confirmation_prompt(
                tmux_mod.pane_text(tmux_bin, session_name)
            )
            if prompt is not None:
                # A Claude Code start-up confirmation (folder trust, or the
                # development-channels warning).  Both are CC-owned on purpose
                # and cannot be pre-accepted, so nobody in a detached pane will
                # ever answer.  Waiting out the deadline and killing the pane
                # (the old path) reported the wrong failure and destroyed the
                # one pane a person could confirm in.  Keep it (and its launch
                # config) and say exactly what to do.
                hints = tmux_mod.attach_hints(session_name)
                question = {
                    "folder-trust": "whether to trust the working folder",
                    "development-channels": "its 'Loading development channels' warning",
                }[prompt]
                raise services.CliError(
                    "CLAUDE_CONFIRMATION_REQUIRED",
                    f"{harness} in tmux session {session_name} is waiting for a "
                    f"person to confirm {question}; attach with "
                    f"`{hints['wsl-linux-terminal']}` (iTerm2: "
                    f"`{hints['macOS-iTerm2']}`), confirm, then detach -- the "
                    "session registers with the daemon on its own",
                    {
                        "prompt": prompt,
                        "actor": actor,
                        "sessionRef": session_ref,
                        "tmuxSession": session_name,
                        "attach": hints,
                    },
                )
        services.time.sleep(0.1)
    if not registered:
        tmux_mod.kill_session(tmux_bin, session_name)
        if config_path is not None:
            from hyprial.daemon import cleanup_launch_resources

            cleanup_launch_resources(config_path, recovery_path)
        raise services.CliError("CHANNEL_REGISTRATION_FAILED", registration_failure)
    return _with_plugin_warnings(
        {
            "ok": True,
            "actor": actor,
            # The IPC key stays "provider" so this CLI can talk to a daemon (and
            # reports) running an older build.
            "provider": harness,
            "runtime": "interactive",
            "sessionRef": session_ref,
            "detached": True,
            "tmuxSession": session_name,
            "attach": tmux_mod.attach_hints(session_name),
        },
        warnings,
    )
