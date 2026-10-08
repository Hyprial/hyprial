"""Interactive Claude session launch."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from pathlib import Path
from hyprial.kernel import child_state_environment
from hyprial.kernel import ipc_errors
import json
import os
import subprocess
import sys

from hyprial.shell.impl.cli.commands.agent.runtime.interactive import _announce_plugin_skips, _channel_registration_confirmed, _handover_prompt, _interactive_actor, _interactive_actor_name, _launch_detached_tui, _plugin_skip_warnings, _runtime_launch_custody, _terminate_process, _wait_foreground, _with_plugin_warnings, _write_launch_config
from hyprial.shell.impl.cli.commands.common.support import JsonObject
_CLAUDE_HARNESS_TOOLS = (
    "harness_ack",
    "harness_progress",
    "harness_read",
    "harness_reply",
    "harness_send",
    "harness_targets",
    "harness_whoami",
)


_CLAUDE_SESSION_IDENTITY_FLAGS = frozenset(
    {"--session-id", "--resume", "--continue", "-c", "--fork-session"}
)


def _reject_claude_session_identity_args(runtime_args: tuple[str, ...]) -> None:
    services = get_services()
    for arg in runtime_args:
        flag = arg.split("=", 1)[0]
        if flag in _CLAUDE_SESSION_IDENTITY_FLAGS:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"{flag} changes Claude session identity, which hyprial owns; "
                "resume a session with `hyprial start claude --resume <session-id>`",
            )


def _start_interactive_claude(
    *,
    name: str,
    nickname: str | None,
    cwd: Path,
    resume: str | None,
    runtime_args: tuple[str, ...],
    model_provider: str | None,
    model: str | None,
    json_output: bool,
    tmux: bool = False,
) -> JsonObject:
    """Launch a real CC TUI with one session-owned Harness Channel server."""
    services = get_services()

    # Argument validation stays ahead of every daemon call: a rejected launch
    # must not have touched the daemon at all.
    _reject_claude_session_identity_args(runtime_args)
    # The persisted identity is the operator's input (the bare name); the
    # daemon mints the canonical URI per call. Never freeze a minted
    # address into the config or argv that outlives this launch (#352).
    actor_name = _interactive_actor_name(name)
    # A5: the agent exists before the TUI does. This is also where a duplicate
    # name is refused -- before a Claude process has been spawned.
    from hyprial.kernel import HarnessLaunchSpec
    from hyprial.daemon import claude_provider_environment

    provider_spec = HarnessLaunchSpec(
        "claude",
        name,
        False,
        args=runtime_args,
        model_provider=model_provider,
        model=model,
    )
    native_model_args = (
        ("--model", model)
        if model is not None and model_provider in {None, "anthropic"}
        else ()
    )
    handover = _handover_prompt(
        services._create_agent_for_start(
            name=name,
            harness="claude",
            runtime="interactive",
            cwd=cwd,
            provider=model_provider,
            model=model,
        )
    )
    runtime_payload = services._runtime_context_environment(
        name=name, harness="claude", cwd=cwd, include_projection=True
    )
    runtime_projection = (
        runtime_payload
        if runtime_payload is not None
        and isinstance(runtime_payload.get("environment"), dict)
        else None
    )
    runtime_environment = (
        runtime_payload
        if runtime_projection is None
        else dict(runtime_projection["environment"])
    )
    from hyprial.identity import apply_runtime_environment_profile
    from hyprial.daemon import (
        CLAUDE_RUNTIME_ENVIRONMENT,
        ClaudeRuntimeError,
        validate_claude_auth_environment,
    )

    status = services._daemon_request("ps")
    actor = _interactive_actor(name, status)
    from hyprial.kernel import agent_uri_actor

    display_name = nickname or agent_uri_actor(actor) or actor
    # Single identity decision: a fresh session gets a new ref; a resume reuses
    # the target session as the ref. The Harness ref and the Claude session id
    # are therefore always the same value and can never collide.
    session_ref = resume if resume is not None else str(services.uuid4())
    identity_args = (
        ["--resume", session_ref]
        if resume is not None
        else ["--session-id", session_ref]
    )
    state_dir = services._state_dir()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The config file belongs to this launch, not to the session: a resumed
    # session may still have a stale file from a crashed earlier launch.
    config_path = state_dir / f"claude-channel-{session_ref}-{services.uuid4().hex[:8]}.json"
    recovery_path = config_path.with_suffix(".recover")
    turn_signal_dir = config_path.with_suffix(".turns")
    server_args = [
        "-m",
        "hyprial.cli",
        "mcp",
        "claude-channel",
        "--actor",
        actor_name,
        "--session-ref",
        session_ref,
        "--cwd",
        str(cwd),
        "--command",
        "claude",
        "--recovery-signal",
        str(recovery_path),
        "--turn-signal-dir",
        str(turn_signal_dir),
    ]
    tmux_session_name: str | None = None
    if tmux:
        from hyprial.daemon import session_name_for_actor

        # Detached mode: this launcher exits right after spawn, so it can
        # never be the channel's owner fence. The carrier receives the tmux
        # session name instead -- the daemon records it on the registration
        # and the carrier fences on the pane's top process, which lives
        # exactly as long as the tmux session.
        tmux_session_name = session_name_for_actor(actor)
        server_args.extend(["--tmux-session", tmux_session_name])
    else:
        # The launcher lives exactly as long as the Claude process it waits
        # for. Passing both PID and birth marker lets a slowly starting
        # channel detect owner death even after reparenting, without
        # mistaking PID reuse for life.
        from hyprial.daemon import read_process_identity as _read_process_identity

        owner_pid = os.getpid()
        owner_identity = _read_process_identity(owner_pid)
        if owner_identity is not None:
            server_args.extend(
                [
                    "--owner-pid",
                    str(owner_pid),
                    "--owner-identity",
                    owner_identity,
                ]
            )
    config = {
        "mcpServers": {
            "harness-bridge": {
                "type": "stdio",
                "command": sys.executable,
                "args": server_args,
                # Pin the stdio child to the same daemon even when the parent
                # shell carries an ambient production socket override.
                "env": child_state_environment(services._hyprial_home(), state_dir),
            }
        }
    }
    from hyprial.shell.impl.plugins import (
        PluginManifestError,
        claude_plan,
        load_manifest,
        materialize_claude_skill_plugin,
    )

    try:
        plugin_plan = claude_plan(load_manifest(services._hyprial_home()))
    except PluginManifestError as error:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    plugin_warnings = _plugin_skip_warnings(plugin_plan.skipped)
    _announce_plugin_skips(plugin_warnings)
    # HYPRIAL_HOME-declared MCP servers ride the same per-launch --mcp-config file
    # as the channel server, so a session never depends on the launch
    # directory's per-project registration in ~/.claude.json.
    config["mcpServers"].update(plugin_plan.mcp_servers)
    # Named after this launch's config file so a crashed earlier launch can
    # never hand its stale skill payload to the next one.
    skill_plugin_dir = materialize_claude_skill_plugin(
        plugin_plan.skill_dirs,
        state_dir / f"{config_path.stem}-skills",
    )
    plugin_dir_args: list[str] = []
    for plugin_dir in (skill_plugin_dir, *plugin_plan.plugin_dirs):
        plugin_dir_args.extend(["--plugin-dir", str(plugin_dir)])
    settings = {
        "permissions": {
            "allow": [f"mcp__harness-bridge__{tool}" for tool in _CLAUDE_HARNESS_TOOLS]
        },
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": sys.executable,
                            "args": [
                                "-m",
                                "hyprial.cli",
                                "mcp",
                                "claude-channel-recover",
                                "--signal-path",
                                str(recovery_path),
                            ],
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": sys.executable,
                            # A stdlib-only module: importing hyprial.cli
                            # takes ~1 s on a loaded host, long enough for
                            # the hook timeout to drop the pulse.
                            "args": [
                                "-m",
                                "hyprial.biz.impl.turn_pulse",
                                "--signal-dir",
                                str(turn_signal_dir),
                            ],
                            "timeout": 5,
                        }
                    ],
                }
            ],
            "StopFailure": [
                {
                    "matcher": (
                        "rate_limit|overloaded|authentication_failed|"
                        "oauth_org_not_allowed|billing_error|invalid_request|"
                        "model_not_found|server_error|max_output_tokens|unknown"
                    ),
                    "hooks": [
                        {
                            "type": "command",
                            "command": sys.executable,
                            "args": [
                                "-m",
                                "hyprial.cli",
                                "mcp",
                                "claude-channel-recover",
                                "--signal-path",
                                str(recovery_path),
                            ],
                        }
                    ],
                }
            ],
        },
    }
    argv = [
        os.environ.get("HARNESS_CLAUDE_BIN", "claude"),
        *native_model_args,
        *runtime_args,
        *identity_args,
        "--name",
        display_name,
        "--mcp-config",
        str(config_path),
        *(
            ("--setting-sources", "user,project,local")
            if runtime_environment is not None
            else ()
        ),
        # Project MCP files remain discoverable inputs, but cannot add or
        # replace servers for this managed session.  The per-launch config
        # contains the daemon-bound Harness server plus explicitly approved
        # plugin-manifest servers.
        "--strict-mcp-config",
        *plugin_dir_args,
        "--settings",
        json.dumps(settings, separators=(",", ":")),
        # Channels are opt-in even when an MCP server declares the preview
        # capability. The local-development confirmation remains CC-owned.
        "--dangerously-load-development-channels",
        "server:harness-bridge",
        "--append-system-prompt",
        (
            "Harness Network is connected through the harness-bridge MCP server. "
            "When a Harness channel notification arrives, immediately call "
            "harness_read and handle every pending message in FIFO order. Reply to "
            "requests with harness_reply; use harness_ack only when no reply is "
            "required. Never treat the channel notification itself as message body."
            # A9: a harness swap starts the conversation from zero. Say so up
            # front, before the first turn, together with the previous harness
            # and session id -- the handover is allowed to cost context, it is
            # not allowed to happen silently.
            + (f"\n\n{handover}" if handover else "")
        ),
    ]
    process: subprocess.Popen[Any] | None = None
    with _runtime_launch_custody(runtime_projection) as secret_environment:
        profile_base = apply_runtime_environment_profile(
            os.environ, runtime_environment, secret_environment
        )
        provider_environment = claude_provider_environment(
            provider_spec,
            os.environ if runtime_environment is None else profile_base,
            allow_legacy_home_fallback=runtime_environment is None,
        )
        launch_environment = apply_runtime_environment_profile(
            os.environ,
            runtime_environment,
            secret_environment,
            provider_environment,
            CLAUDE_RUNTIME_ENVIRONMENT if runtime_environment is not None else {},
        )
        if runtime_environment is not None:
            native_root = runtime_environment.get("CLAUDE_CONFIG_DIR")
            if not native_root:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "Claude P2 runtime context is missing CLAUDE_CONFIG_DIR",
                )
            try:
                validate_claude_auth_environment(
                    Path(native_root), launch_environment
                )
            except ClaudeRuntimeError as error:
                raise services.CliError(
                    error.code or ipc_errors.INVALID_ARGUMENT, str(error)
                ) from error
        # M2: the attached TUI (and every shell it runs) carries the carrier's
        # daemon-bound identity. Build a child-only map; never mutate os.environ.
        launch_environment.update(
            {
                "HYPRIAL_WORKER_ACTOR": actor,
                "HYPRIAL_WORKER_SESSION_REF": session_ref,
                "HYPRIAL_MANAGED_WORKER": "1",
            }
        )
        if tmux_session_name is not None:
            return _launch_detached_tui(
                argv=argv,
                env=launch_environment,
                cwd=cwd,
                actor=actor,
                session_ref=session_ref,
                session_name=tmux_session_name,
                harness="claude",
                registration_deadline_seconds=60.0,
                is_session_registered=lambda status: _channel_registration_confirmed(
                    actor, session_ref, status
                ),
                registration_failure=(
                    f"Claude exited or timed out before Channel registration for {actor}"
                ),
                config=config,
                config_path=config_path,
                recovery_path=recovery_path,
                warnings=plugin_warnings,
            )
        try:
            _write_launch_config(config_path, config)
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=launch_environment,
                stdout=sys.stderr if json_output else None,
            )
        except BaseException:
            if process is not None and process.poll() is None:
                _terminate_process(process)
            from hyprial.daemon import cleanup_launch_resources

            cleanup_launch_resources(config_path, recovery_path)
            raise
    try:
        registered = False
        deadline = services.time.monotonic() + 60.0
        while services.time.monotonic() < deadline:
            status = services._daemon_request("ps")
            sessions = status.get("interactiveSessions", [])
            registered = isinstance(sessions, list) and any(
                isinstance(item, dict)
                and item.get("actor") == actor
                and item.get("sessionRef") == session_ref
                and item.get("channelConfirmed") is True
                for item in sessions
            )
            if registered or process.poll() is not None:
                break
            services.time.sleep(0.1)
        if not registered:
            if process.poll() is None:
                _terminate_process(process)
            else:
                process.wait()
            raise services.CliError(
                "CHANNEL_REGISTRATION_FAILED",
                f"Claude exited or timed out before Channel registration for {actor}",
            )
        returncode = _wait_foreground(process)
    finally:
        if process is not None and process.poll() is None:
            _terminate_process(process)
        from hyprial.daemon import cleanup_launch_resources

        cleanup_launch_resources(config_path, recovery_path)
    return _with_plugin_warnings(
        {
            "ok": returncode == 0,
            "actor": actor,
            "provider": "claude",
            "runtime": "interactive",
            "sessionRef": session_ref,
            "runtimeExitCode": returncode,
        },
        plugin_warnings,
    )
