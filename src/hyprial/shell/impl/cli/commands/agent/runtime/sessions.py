"""Interactive pi and codex session launches."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import warn

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import Logger, agent_uri_actor
from pathlib import Path
from hyprial.kernel import child_state_environment
from hyprial.kernel import ipc_errors
import os
import shutil
import subprocess
import sys
import tempfile
import time

from hyprial.shell.impl.cli.commands.agent.runtime.interactive import _announce_plugin_skips, _handover_prompt, _interactive_actor, _launch_detached_tui, _pi_attach_registration, _plugin_skip_warnings, _runtime_context_projection, _runtime_launch_custody, _terminate_process, _wait_foreground, _with_plugin_warnings
from hyprial.shell.impl.cli.commands.common.support import JsonObject
_PI_SESSION_IDENTITY_FLAGS = frozenset(
    {
        "--session-id",
        "--session",
        "--fork",
        "--continue",
        "-c",
        "--resume",
        "-r",
        "--mode",
        "--print",
        "-p",
        "--no-session",
    }
)


def _reject_pi_session_identity_args(runtime_args: tuple[str, ...]) -> None:
    services = get_services()
    for arg in runtime_args:
        flag = arg.split("=", 1)[0]
        if flag in _PI_SESSION_IDENTITY_FLAGS:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"{flag} changes pi session identity or mode, which hyprial owns "
                "on an attached launch",
            )


def _registration_clock() -> float:
    """Monotonic clock for the interactive attach-registration waits.

    A seam local to these waits: tests patch it here instead of the global
    ``time.monotonic``, which every thread in the process shares (a leaked
    background thread once consumed a test's first tick and the wait never
    ended, hanging a CI shard for 30 minutes).
    """

    return time.monotonic()


def _start_interactive_pi(
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
    """Launch a real pi TUI with the hyprial attach carrier extension loaded.

    Same five phases as the Claude interactive launcher (design
    docs/notes/design-tui-launcher.md): prepare (validate + agent.create +
    identity), spawn_tui (foreground, terminal inherited; or a detached
    tmux session with --tmux), attach_and_register (poll ps until the
    carrier's session.register lands), wait_foreground, teardown (terminate
    stragglers; the carrier's own session_shutdown unregister is the normal
    exit path and the daemon TTL reclaims anything else).

    Detached mode needs no claude-style owner-fence rewiring: the pi carrier
    is an IN-PROCESS extension, so its lifetime already equals the TUI
    process's -- which under tmux is the pane's. kill-session SIGHUPs the
    pane, pi dies, the carrier dies with it (session_shutdown unregisters
    best-effort, the daemon TTL reclaims the rest). The launcher only
    teaches the carrier the session name (HYPRIAL_WORKER_TMUX_SESSION) so the
    daemon can record it on the registration.
    """
    services = get_services()

    from hyprial.kernel import HarnessLaunchSpec
    from hyprial.daemon import pi_model_args
    from hyprial.kernel import agent_uri_actor
    from hyprial.daemon import PI_HARNESS_ATTACH_EXTENSION
    from hyprial.daemon import pi_session_id
    from hyprial.daemon import SessionFileError, resolve_legacy_pi_resume_session

    # Argument validation stays ahead of every daemon call: a rejected launch
    # must not have touched the daemon at all.
    _reject_pi_session_identity_args(runtime_args)
    provider_spec = HarnessLaunchSpec(
        "pi",
        name,
        False,
        args=runtime_args,
        cwd=str(cwd),
        model_provider=model_provider,
        model=model,
    )
    selected_args = pi_model_args(provider_spec)
    if resume is None:
        handover = _handover_prompt(
            services._create_agent_for_start(
                name=name,
                harness="pi",
                runtime="interactive",
                cwd=cwd,
                provider=model_provider,
                model=model,
            )
        )
    else:
        preflight = services._daemon_request(
            "agent.start.preflight",
            {"name": name, "harness": "pi", "runtime": "interactive"},
        )
        if type(preflight.get("resumeCommitGuardVersion")) is not int or int(
            preflight["resumeCommitGuardVersion"]
        ) < 1:
            raise services.CliError(
                "PI_RESUME_GUARD_UNSUPPORTED",
                "daemon does not confirm atomic interactive Pi resume support",
            )
        handover = None
    runtime_projection = services._runtime_context_environment(
        name=name, harness="pi", cwd=cwd, include_projection=True
    )
    runtime_environment = (
        None
        if runtime_projection is None
        else {
            key: value
            for key, value in runtime_projection["environment"].items()
            if isinstance(key, str) and isinstance(value, str)
        }
    )
    status = services._daemon_request("ps")
    actor = _interactive_actor(name, status)
    display_name = nickname or agent_uri_actor(actor) or actor
    # Single identity decision: the Harness ref and the pi session id are
    # the same value, translated once at pi's --session-id charset boundary
    # (#192; the raw ref remains the daemon identity key).
    session_ref = resume or str(services.uuid4())
    state_dir = services._state_dir()
    append_system_prompt = (
        "Harness Network is connected through the hyprial pi harness-bridge "
        "extension. Harness messages arrive as user messages prefixed "
        "with [Harness Network ...]; each carries only a summary; read a body with harness_read_body, "
        "and answer each with harness_reply or harness_ack — nothing is sent on your behalf. "
        "Use the harness_send/harness_read/harness_progress/harness_reply/harness_ack/"
        "harness_targets/harness_whoami tools for proactive Harness "
        "Network access." + (f"\n\n{handover}" if handover else "")
        # A9: a harness swap starts the conversation from zero. Say so up
        # front, before the first turn.
    )
    legacy_resume_path: Path | None = None
    if resume is not None and runtime_projection is None:
        agent_dir = Path(
            os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent"))
        ).expanduser()
        try:
            legacy_resume_path = resolve_legacy_pi_resume_session(
                agent_dir, str(cwd), session_ref
            )
        except SessionFileError as error:
            reason = getattr(error, "reason", "unusable")
            code = {
                "not-found": "PI_RESUME_SESSION_UNKNOWN",
                "ambiguous": "PI_RESUME_SESSION_AMBIGUOUS",
                "outside-cwd": "PI_RESUME_SESSION_OUTSIDE_CWD",
            }.get(reason, "PI_RESUME_SESSION_UNRESOLVABLE")
            guidance = (
                "; use controlled-loader (P2) mode for SDK-owned exact lookup"
                if reason == "unusable"
                else ""
            )
            raise services.CliError(code, f"{error}{guidance}") from error
    native_session_args = (
        ("--session", str(legacy_resume_path))
        if legacy_resume_path is not None
        else (
            ("--session", session_ref)
            if resume is not None
            else ("--session-id", pi_session_id(session_ref))
        )
    )
    argv = [
        os.environ.get("HARNESS_PI_BIN", "pi"),
        *selected_args,
        *runtime_args,
        *native_session_args,
        "--name",
        display_name,
        "--extension",
        str(PI_HARNESS_ATTACH_EXTENSION),
        "--append-system-prompt",
        append_system_prompt,
    ]
    from hyprial.shell.impl.plugins import PluginManifestError, load_manifest, pi_plan

    try:
        plugin_plan = pi_plan(load_manifest(services._hyprial_home()))
    except PluginManifestError as error:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    plugin_warnings = _plugin_skip_warnings(plugin_plan.skipped)
    _announce_plugin_skips(plugin_warnings)
    # HYPRIAL_HOME-declared payloads ride pi's native session-scoped flags, so a
    # session never depends on launch-directory-local discovery.
    if runtime_projection is None:
        for skill_dir in plugin_plan.skill_dirs:
            argv.extend(["--skill", str(skill_dir)])
        for extension in plugin_plan.extensions:
            argv.extend(["--extension", str(extension)])
    from hyprial.identity import apply_runtime_environment_profile

    process: subprocess.Popen[Any] | None = None
    with _runtime_launch_custody(runtime_projection) as secret_environment:
        environment = apply_runtime_environment_profile(
            os.environ,
            runtime_environment,
            secret_environment,
            {
                # The carrier's canonical identity is child-only and pinned
                # to this daemon's socket (never an ambient production daemon).
                "HYPRIAL_WORKER_ACTOR": actor,
                "HYPRIAL_WORKER_SESSION_REF": session_ref,
                "HYPRIAL_MANAGED_WORKER": "1",
                **(
                    {"HYPRIAL_RESUME_GUARDED": "1"}
                    if resume is not None
                    else {}
                ),
                **child_state_environment(services._hyprial_home(), state_dir),
            },
        )
        from hyprial.daemon import pi_provider_environment

        # Pi reads DEEPSEEK_API_KEY from its own environment; unlike claude and
        # codex it had no model-vendor step here, so a P2 launch without a
        # granted secret started secretless and failed every turn (2026-10-16).
        # The source stays the per-agent authorization; a P2 launch with no
        # grant now refuses here instead of running without a credential.
        environment = {
            **environment,
            **pi_provider_environment(
                provider_spec,
                environment,
                native_root=(
                    None
                    if runtime_projection is None
                    else Path(str(runtime_projection["nativeRoot"]))
                ),
                settings_root=(
                    None
                    if runtime_projection is None
                    else Path(str(runtime_projection["projectionRoot"]))
                ),
                allow_legacy_home_fallback=runtime_projection is None,
            ),
        }
        if runtime_projection is not None:
            from hyprial.daemon import (
                find_pi_package_root,
                pi_sdk_launch_from_public_projection,
                resolve_approved_pi_project,
            )

            pi_command = (os.environ.get("HARNESS_PI_BIN", "pi"),)
            try:
                trust = resolve_approved_pi_project(
                    cwd=str(cwd), runtime_args=runtime_args
                )
                sdk_launch = pi_sdk_launch_from_public_projection(
                    runtime_projection,
                    trust=trust,
                    mode="tui",
                    session_id=pi_session_id(session_ref),
                    require_existing_session=resume is not None,
                    pi_package_root=find_pi_package_root(pi_command, environment),
                    model_provider=model_provider,
                    model=model,
                    additional_extension_paths=(
                        PI_HARNESS_ATTACH_EXTENSION,
                        *plugin_plan.extensions,
                    ),
                    additional_skill_paths=plugin_plan.skill_dirs,
                    append_system_prompt=(append_system_prompt,),
                    session_name=display_name,
                )
            except ValueError as error:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT, str(error)
                ) from error
            argv = list(sdk_launch.argv)
        if tmux:
            from hyprial.daemon import session_name_for_actor

            session_name = session_name_for_actor(actor)
            environment["HYPRIAL_WORKER_TMUX_SESSION"] = session_name
            return _launch_detached_tui(
                argv=argv,
                env=environment,
                cwd=cwd,
                actor=actor,
                session_ref=session_ref,
                session_name=session_name,
                harness="pi",
                # An attended launch may stall on pi's project-trust prompt
                # before the extension ever runs: the deadline is generous, and
                # the error names that exact cause (research finding E8).
                registration_deadline_seconds=180.0,
                is_session_registered=lambda status: _pi_attach_registration(
                    actor, session_ref, status
                ),
                registration_failure=(
                    f"Pi could not resume exact session {session_ref!r}"
                    if resume is not None
                    else "pi exited or timed out before attach registration for "
                    f"{actor}; if the project-trust prompt was showing, trust "
                    "the project and retry"
                ),
                warnings=plugin_warnings,
            )
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdout=sys.stderr if json_output else None,
        )
    try:
        registered = False
        # An attended launch may stall on pi's project-trust prompt before
        # the extension ever runs: the deadline is generous, and the error
        # names that exact cause (research finding E8).
        deadline = _registration_clock() + 180.0
        while _registration_clock() < deadline:
            status = services._daemon_request("ps")
            sessions = status.get("interactiveSessions", [])
            registered = isinstance(sessions, list) and any(
                isinstance(item, dict)
                and item.get("actor") == actor
                and item.get("sessionRef") == session_ref
                and item.get("source") == "pi-extension"
                for item in sessions
            )
            if registered or process.poll() is not None:
                break
            services.time.sleep(0.1)
        if not registered:
            exited = process.poll() is not None
            if not exited:
                _terminate_process(process)
            else:
                process.wait()
            if resume is not None and exited:
                raise services.CliError(
                    "PI_RESUME_SESSION_UNKNOWN",
                    f"Pi did not find exact session {session_ref!r}; no fresh "
                    "session was created",
                )
            raise services.CliError(
                "PI_RESUME_TUI_ATTACH_FAILED"
                if resume is not None
                else "CHANNEL_REGISTRATION_FAILED",
                f"pi exited or timed out before attach registration for {actor}; "
                "if the project-trust prompt was showing, trust the project "
                "and retry",
            )
        returncode = _wait_foreground(process)
    finally:
        if process is not None and process.poll() is None:
            _terminate_process(process)
    return _with_plugin_warnings(
        {
            "ok": returncode == 0,
            "actor": actor,
            "provider": "pi",
            "runtime": "interactive",
            "sessionRef": session_ref,
            "runtimeExitCode": returncode,
        },
        plugin_warnings,
    )


_CODEX_SESSION_FLAGS = frozenset({"--remote", "--remote-auth-token-env"})


def _reject_codex_session_args(runtime_args: tuple[str, ...]) -> None:
    services = get_services()
    for arg in runtime_args:
        flag = arg.split("=", 1)[0]
        if flag in _CODEX_SESSION_FLAGS:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"{flag} changes the Codex app-server session transport, which "
                "hyprial owns on an attached launch",
            )


_CODEX_APPROVAL_OPTIONS = ("--ask-for-approval", "-a")
_CODEX_NO_APPROVAL = "never"
_CODEX_BYPASS_FLAGS = frozenset({"--dangerously-bypass-approvals-and-sandbox", "--yolo"})


def _interactive_codex_runtime_args(runtime_args: tuple[str, ...]) -> tuple[str, ...]:
    """Pin ``on-request`` approvals: under ``never`` codex refuses every MCP call.

    The harness-bridge tools are MCP tools, so an interactive launch that
    disables approvals would mount them and then have each call refused.
    There is no operator flag that legitimately opts out, hence rejection.
    """

    services = get_services()
    approvals: list[str] = []
    for index, arg in enumerate(runtime_args):
        name, eq, inline = arg.partition("=")
        if name in _CODEX_APPROVAL_OPTIONS:
            approvals.append(
                inline if eq else (runtime_args[index + 1] if index + 1 < len(runtime_args) else "")
            )
        elif arg.startswith("-a") and not arg.startswith("--"):
            approvals.append(arg[2:])
    if (
        _CODEX_NO_APPROVAL in approvals
        or _CODEX_BYPASS_FLAGS.intersection(runtime_args)
    ):
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "an interactive Codex session needs on-request approvals for its "
            "harness-bridge tools; '-a never', '--yolo' and "
            "'--dangerously-bypass-approvals-and-sandbox' make Codex refuse every "
            "MCP tool call (use them only with --headless)",
        )
    if approvals or "--approve-for-me" in runtime_args:
        return runtime_args
    return ("--ask-for-approval", "on-request", *runtime_args)


def _codex_tmux_module() -> Any:
    """The daemon's tmux boundary, imported lazily like the pi/claude paths."""

    from hyprial.daemon import harnesses_tmux

    return harnesses_tmux


def _codex_tmux_session_name(actor: str) -> str:
    from hyprial.daemon import session_name_for_actor

    return session_name_for_actor(actor)


def _codex_attach_registration(
    actor: str, session_ref: str, status: JsonObject
) -> bool:
    """Readiness for the Codex carrier: its ``session.register`` has landed."""

    sessions = status.get("interactiveSessions", [])
    return isinstance(sessions, list) and any(
        isinstance(item, dict)
        and item.get("actor") == actor
        and item.get("sessionRef") == session_ref
        and item.get("source") == "codex-app-server"
        for item in sessions
    )


def _start_interactive_codex(
    *,
    name: str,
    nickname: str | None,
    cwd: Path,
    resume: str | None = None,
    runtime_args: tuple[str, ...],
    model_provider: str | None,
    model: str | None,
    json_output: bool,
    tmux: bool = False,
    _attach_timeout_seconds: float = 180.0,
) -> JsonObject:
    """Launch Codex with a hyprial-owned app-server and remote TUI.

    PR1 owns launch-time discovery, registration, foreground TUI lifetime, and
    clean detach.  The socket carrier's turn/inbox/FIFO behavior lands in PR2.
    """
    services = get_services()

    from hyprial.kernel import HarnessLaunchSpec
    from hyprial.identity import SharedCredentialBinding
    from hyprial.daemon import (
        CodexAppServerRpcError,
        CodexInteractiveAppServer,
        CodexInteractiveCarrier,
        CodexInteractiveResumeError,
        CodexInteractiveResumePending,
        interactive_channel_config_args,
    )
    from hyprial.daemon import codex_provider_configuration as codex_model_provider_configuration

    from hyprial.shell.impl.plugins import PluginManifestError, codex_plan, load_manifest

    _reject_codex_session_args(runtime_args)
    launch_args = _interactive_codex_runtime_args(runtime_args)
    provider_spec = HarnessLaunchSpec(
        "codex",
        name,
        False,
        args=runtime_args,
        model_provider=model_provider,
        model=model,
    )
    try:
        plugin_plan = codex_plan(load_manifest(services._hyprial_home()))
    except PluginManifestError as error:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    plugin_warnings = _plugin_skip_warnings(plugin_plan.skipped)
    _announce_plugin_skips(plugin_warnings)
    if resume is None:
        services._create_agent_for_start(
            name=name,
            harness="codex",
            runtime="interactive",
            cwd=cwd,
            provider=model_provider,
            model=model,
        )
    else:
        preflight = services._daemon_request(
            "agent.start.preflight",
            {"name": name, "harness": "codex", "runtime": "interactive"},
        )
        if type(preflight.get("resumeCommitGuardVersion")) is not int or int(
            preflight["resumeCommitGuardVersion"]
        ) < 1:
            raise services.CliError(
                "CODEX_RESUME_GUARD_UNSUPPORTED",
                "daemon does not confirm atomic interactive Codex resume support",
            )
    runtime_projection = _runtime_context_projection(
        name=name, harness="codex", cwd=cwd
    )
    runtime_environment = (
        None
        if runtime_projection is None
        else dict(runtime_projection["environment"])
    )
    from hyprial.identity import apply_runtime_environment_profile

    status = services._daemon_request("ps")
    actor = _interactive_actor(name, status)
    codex_bin = os.environ.get("HARNESS_CODEX_BIN", "codex")
    # A detached pane outlives this CLI, so its socket directory must too: the
    # app-server keeps listening there and the pane TUI keeps reading it.  The
    # foreground path keeps the throwaway /tmp root it always had.
    if tmux:
        tmux_module = _codex_tmux_module()
        tmux_bin = tmux_module.find_tmux()
        if tmux_bin is None:
            raise services.CliError(
                "TMUX_UNAVAILABLE", "--tmux requires the tmux binary on PATH"
            )
        session_name = _codex_tmux_session_name(actor)
        if tmux_module.has_session(tmux_bin, session_name):
            hints = tmux_module.attach_hints(session_name)
            raise services.CliError(
                "TMUX_SESSION_EXISTS",
                f"tmux session {session_name} already exists; attach with "
                f"{hints['wsl-linux-terminal']} or kill it first",
            )
        root = (
            Path(services._state_dir())
            / "codex-tmux"
            / (agent_uri_actor(actor) or actor)
        )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    else:
        tmux_module = None
        tmux_bin = None
        session_name = None
        root = Path(tempfile.mkdtemp(prefix="hyprial-codex-attach-", dir="/tmp"))
    socket_path = root / "app.sock"
    session_ref_file = root / "session-ref"
    session_ref: str | None = None
    registered = False
    raw_shared_credential = (
        None
        if runtime_projection is None
        else runtime_projection.get("sharedCredential")
    )
    shared_credential = (
        None
        if not isinstance(raw_shared_credential, dict)
        else SharedCredentialBinding(
            actor=actor,
            harness="codex",
            native_path=Path(str(raw_shared_credential["nativePath"])),
            target_path=Path(str(raw_shared_credential["targetPath"])),
            agent_cwd=cwd,
        )
    )
    server: CodexInteractiveAppServer | None = None
    process: subprocess.Popen[Any] | None = None
    carrier: CodexInteractiveCarrier | None = None
    detached_ready = False
    returncode = 1
    model_args = ("--model", model) if model is not None else ()
    argv: list[str] = []
    try:
        with _runtime_launch_custody(runtime_projection) as secret_environment:
            profile_base = apply_runtime_environment_profile(
                os.environ, runtime_environment, secret_environment
            )
            provider_args, provider_environment = codex_model_provider_configuration(
                provider_spec,
                os.environ if runtime_environment is None else profile_base,
                allow_legacy_home_fallback=runtime_projection is None,
            )
            environment = apply_runtime_environment_profile(
                os.environ,
                runtime_environment,
                secret_environment,
                child_state_environment(
                    services._hyprial_home(), services._state_dir()
                ),
                provider_environment,
            )
            server = CodexInteractiveAppServer(
                socket_path,
                cwd=cwd,
                command=(codex_bin, *provider_args),
                env=environment,
                # Plugin MCP servers run in the app server, not the remote TUI;
                # so does harness-bridge, whose sessionRef is published below.
                config_args=(
                    *plugin_plan.config_args,
                    *interactive_channel_config_args(
                        actor=actor,
                        session_ref_file=session_ref_file,
                        hyprial_home=services._hyprial_home(),
                        state_dir=services._state_dir(),
                    ),
                ),
                model_provider=model_provider,
                projection_root=(
                    None
                    if runtime_projection is None
                    else Path(str(runtime_projection["projectionRoot"]))
                ),
                native_root=(
                    None
                    if runtime_projection is None
                    else Path(str(runtime_projection["nativeRoot"]))
                ),
                session_root=(
                    None
                    if runtime_projection is None
                    else Path(str(runtime_projection["sessionRoot"]))
                ),
                shared_credential=shared_credential,
                authority_prepared=(
                    runtime_projection is not None
                    and runtime_projection.get("authorityPrepared") is True
                ),
            )
            argv = [codex_bin, *provider_args]
            if resume is not None:
                argv.extend(("resume", resume))
            argv.extend(
                [
                    "--no-alt-screen",
                    *model_args,
                    *launch_args,
                    "--remote",
                    f"unix://{socket_path}",
                ]
            )
            server.start()
            if resume is not None:
                try:
                    server.resume_thread(
                        resume, runtime_args=launch_args, model=model
                    )
                except CodexInteractiveResumeError as error:
                    raise services.CliError(error.code, str(error)) from error
            if tmux_module is not None:
                assert session_name is not None and tmux_bin is not None
                tmux_module.new_detached_session(
                    tmux_bin, session_name, argv, cwd=cwd, env=environment
                )
                process = None
            else:
                process = subprocess.Popen(argv, cwd=cwd, env=environment)
        assert server is not None
        deadline = _registration_clock() + 180.0
        while _registration_clock() < deadline:
            if tmux_module is not None:
                assert session_name is not None and tmux_bin is not None
                if not tmux_module.has_session(tmux_bin, session_name):
                    raise services.CliError(
                        "CODEX_ATTACH_REGISTRATION_FAILED",
                        "Codex tmux pane exited before interactive thread "
                        f"discovery for {actor}",
                    )
            elif process is not None and process.poll() is not None:
                if resume is not None:
                    raise services.CliError(
                        "CODEX_RESUME_TUI_ATTACH_FAILED",
                        f"Codex TUI exited before attaching requested thread {resume!r}",
                    )
                raise services.CliError(
                    "CODEX_ATTACH_REGISTRATION_FAILED",
                    f"Codex exited before interactive thread discovery for {actor}",
                )
            try:
                if resume is None:
                    discovered, discovery_mode = server.discover_thread()
                else:
                    discovered, discovery_mode = server.confirm_resumed_thread(resume)
            except CodexInteractiveResumePending:
                services.time.sleep(
                    min(0.2, max(0.0, deadline - services.time.monotonic()))
                )
                continue
            except CodexInteractiveResumeError as error:
                raise services.CliError(error.code, str(error)) from error
            except (ConnectionError, CodexAppServerRpcError):
                services.time.sleep(
                    min(0.2, max(0.0, deadline - services.time.monotonic()))
                )
                continue
            session_ref = discovered
            from hyprial.daemon import read_process_identity as _read_process_identity

            process_pid = server.pid
            process_identity = (
                _read_process_identity(process_pid)
                if process_pid is not None
                else None
            )
            process_fence = (
                {
                    "processPid": process_pid,
                    "processIdentity": process_identity,
                }
                if process_pid is not None and process_identity is not None
                else {}
            )
            registration_method = (
                "session.register_guarded" if resume is not None else "session.register"
            )
            try:
                registration = services._daemon_request(
                    registration_method,
                    {
                    "actor": actor,
                    "cwd": str(cwd),
                    "command": argv,
                    "source": "codex-app-server",
                    "runtime": "codex_interactive",
                    "sessionRef": session_ref,
                    **process_fence,
                    **({"tmuxSession": session_name} if session_name else {}),
                    },
                )
            except services.CliError as error:
                if resume is not None and error.code == ipc_errors.METHOD_NOT_FOUND:
                    raise services.CliError(
                        "CODEX_RESUME_GUARD_UNSUPPORTED",
                        "daemon does not implement guarded interactive Codex resume",
                    ) from error
                raise
            registered = True
            if (
                resume is not None
                and registration.get("liveIncumbentGuardApplied") is not True
            ):
                raise services.CliError(
                    "CODEX_RESUME_GUARD_UNCONFIRMED",
                    "daemon did not confirm that the live-incumbent guard was applied",
                )
            pending_ref = session_ref_file.with_name("session-ref.tmp")
            pending_ref.write_text(session_ref, encoding="utf-8")
            pending_ref.replace(session_ref_file)
            if tmux_module is not None:
                assert session_name is not None
                # Re-verify the CLI's own predicate against the daemon's live
                # ps, so a daemon that accepted the call but never reflected
                # the session cannot be reported as detached-and-ready.
                if not _codex_attach_registration(
                    actor, session_ref, services._daemon_request("ps")
                ):
                    raise services.CliError(
                        "CODEX_ATTACH_REGISTRATION_FAILED",
                        f"daemon did not reflect the detached Codex session for {actor}",
                    )
                detached_ready = True
                if not json_output:
                    warn(
                        f"Codex detached actor={actor} sessionRef={session_ref} "
                        f"tmux={session_name}; the pane TUI drives the app-server, "
                        "but the in-process inbox carrier is not started (transfer "
                        "attach/continue only)",
                        json_output=False,
                    )
                return _with_plugin_warnings(
                    {
                        "ok": True,
                        "actor": actor,
                        "provider": "codex",
                        "runtime": "interactive",
                        "sessionRef": session_ref,
                        "detached": True,
                        "tmuxSession": session_name,
                        "attach": tmux_module.attach_hints(session_name),
                    },
                    plugin_warnings,
                )
            carrier = CodexInteractiveCarrier(
                server,
                actor=actor,
                session_ref=session_ref,
                cwd=cwd,
                command=argv,
                daemon_request=lambda method, params: services._daemon_request(method, params),
                logger=Logger.worker(services._state_dir(), runtime="codex", name=actor),
                state_path=services._state_dir() / "codex-interactive-carrier.sqlite3",
                process_pid=process_pid if process_identity is not None else None,
                process_identity=process_identity,
            )
            carrier.start()
            if not json_output:
                warn(
                    f"Codex attach registered actor={actor} sessionRef={session_ref} "
                    f"discovery={discovery_mode}",
                    json_output=False,
                )
            break
        else:
            raise services.CliError(
                "CODEX_RESUME_TUI_ATTACH_FAILED"
                if resume is not None
                else "CODEX_ATTACH_REGISTRATION_FAILED",
                f"timed out attaching Codex TUI to requested thread {resume!r}"
                if resume is not None
                else f"timed out discovering a Codex TUI thread for {actor}",
            )
        returncode = _wait_foreground(process)
    finally:
        if not detached_ready:
            # On a failed detached launch the pane (if any) and app-server are
            # ours to tear down; on success the pane TUI and its app-server are
            # the owners: this CLI must not stop the server, unregister the
            # session, or delete the socket directory it handed them.
            if (
                tmux_module is not None
                and session_name is not None
                and tmux_bin is not None
            ):
                tmux_module.kill_session(tmux_bin, session_name)
            if carrier is not None:
                carrier.stop()
            if registered and session_ref is not None:
                try:
                    services._daemon_request(
                        "session.unregister",
                        {"actor": actor, "sessionRef": session_ref},
                        restore_wait=0.0,
                    )
                except Exception:  # noqa: BLE001, S110 - cleanup must not mask the TUI exit
                    pass
            if process is not None and process.poll() is None:
                _terminate_process(process)
            if server is not None:
                server.stop()
            shutil.rmtree(root, ignore_errors=True)
    return _with_plugin_warnings(
        {
            "ok": returncode == 0,
            "actor": actor,
            "provider": "codex",
            "runtime": "interactive",
            "sessionRef": session_ref,
            "runtimeExitCode": returncode,
        },
        plugin_warnings,
    )
