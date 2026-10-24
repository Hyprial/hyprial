"""``hyprial agent start`` - launch an agent session (headless or interactive)."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import LIFECYCLE_IPC_MARGIN_SECONDS, LIFECYCLE_OPERATION_DEADLINE_SECONDS, LIFECYCLE_WAIT_MARGIN_SECONDS
from pathlib import Path
from hyprial.kernel import ipc_errors
import json
import os
import shutil
import sys
import typer

from hyprial.shell.impl.cli.commands.agent.admin import agent_app
from hyprial.shell.impl.cli.commands.common.support import JsonObject, _daemon_ipc_roundtrip_seconds, _resolved_agent_cwd, _resolved_interactive_resume_cwd, _user_proxy_launch_params


@agent_app.command(
    "start",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def start(
    ctx: typer.Context,
    harness_kind: str | None = typer.Argument(None, help="claude, pi, codex, dsh, jev, or user-proxy; optional with --tier."),
    name: str = typer.Option(..., "--name"),
    tier: str | None = typer.Option(None, "--tier", help="Select fast, strong, or super; it chooses harness/provider/model, so it cannot be combined with any of them."),
    nickname: str | None = typer.Option(None, "--nickname"),
    cwd: Path | None = typer.Option(None, "--cwd"),
    headless: bool = typer.Option(False, "--headless"),
    replace_interactive: bool = typer.Option(
        False,
        "--replace-interactive",
        help=(
            "With --headless, explicitly stop the same actor's interactive "
            "connector before starting, then keep its --resume session id. "
            "No process is killed by the caller."
        ),
    ),
    smolvm_spec: Path | None = typer.Option(None, "--smolvm-spec", help="Explicit local smolvm v1 spec for a P2 headless Codex worker; experimental support boundary."),
    tmux: bool = typer.Option(
        False,
        "--tmux",
        help=(
            "Run the interactive TUI inside a detached tmux session and "
            "return attach commands (interactive claude, pi, and codex). "
            "Detached Codex has no inbox carrier; for message delivery, run "
            "the foreground Codex start inside your own tmux window."
        ),
    ),
    agent_preset: str | None = typer.Option(
        None,
        "--agent-preset",
        help=(
            "Interactive dsh only: the dsh-tui agent preset id this launch "
            "boots (passed as the DSH_TUI_PRESET launch instruction); "
            "defaults to standard."
        ),
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help=(
            "Resume an existing session by id: interactive claude, pi, and "
            "codex, or headless claude, pi, and codex. Interactive Codex "
            "requires an existing Agent. DSH is refused because "
            "a fresh worker MCP identity cannot attach an existing DSH session; "
            "jev has no persistent session; user-proxy is a stateless relay. "
            "A missing session is refused, never replaced with a fresh one."
        ),
    ),
    model_provider: str | None = typer.Option(
        None,
        "--provider",
        help="Model vendor (for example deepseek); separate from the harness kind.",
    ),
    model: str | None = typer.Option(None, "--model", help="Model id."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Start a harness connector through the daemon."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        nonlocal harness_kind, model_provider, model, headless

        from hyprial.daemon import (
            ModelProviderError,
            validate_model_selection,
        )

        from hyprial.kernel import TIERS

        execution_runtime = None
        if smolvm_spec is not None:
            from hyprial.kernel import SmolvmRuntimeSpec
            from hyprial.daemon import smolvm_regular_read as _regular_read
            if harness_kind != "codex" or not headless or tmux or tier is not None:
                raise services.CliError(ipc_errors.INVALID_ARGUMENT, "--smolvm-spec requires explicit headless Codex")
            execution_runtime = SmolvmRuntimeSpec.from_json(json.loads(_regular_read(smolvm_spec.absolute())))
            capabilities = services._daemon_request("ps", {})
            if "smolvm-v1" not in capabilities.get("executionRuntimes", []):
                raise services.CliError(ipc_errors.INVALID_ARGUMENT, "Daemon does not support smolvm; no agent created")
        runtime_args = tuple(ctx.args)
        if tier is not None and tier not in TIERS:
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, "tier must be fast, strong, or super")
        explicit_selection = (
            harness_kind is not None or model_provider is not None or model is not None
            or any(arg in {"--provider", "--model"} or arg.startswith(("--provider=", "--model="))
                   for arg in runtime_args)
        )
        if tier is not None and explicit_selection:
            # --tier CHOOSES harness/provider/model.  Combined with an explicit
            # one it used to be dropped without a word, starting e.g. a pi with
            # no model vendor or model, silently running the harness's global default
            # (2026-09-21: `start pi --tier super` -> kimi-coding/k3 -> 403).
            # Refuse here, before any daemon call: no agent, no desired state.
            given = [
                label
                for label, present in (
                    (f"harness {harness_kind!r}", harness_kind is not None),
                    ("--provider", model_provider is not None),
                    ("--model", model is not None),
                    (
                        "--provider/--model after '--'",
                        any(
                            arg in {"--provider", "--model"}
                            or arg.startswith(("--provider=", "--model="))
                            for arg in runtime_args
                        ),
                    ),
                )
                if present
            ]
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"--tier {tier} chooses harness/provider/model itself; it "
                f"cannot be combined with {', '.join(given)}. Use either "
                f"`hyprial agent start --tier {tier} --name ...` or an explicit harness "
                "with --provider/--model",
            )
        if tier is not None and not explicit_selection:
            # Resolve inside the daemon so the audit uses its own runtime
            # profile, not caller-supplied evidence.  Selection is static
            # configuration, so this is one ordinary IPC round-trip: the
            # request no longer waits on any liveness probe (2026-09-21).
            # Availability recorded by `agent tiers probe` never enters this path.
            resolved = services._daemon_request(
                "dispatch.matrix.resolve", {"tier": tier, "name": name},
                timeout=_daemon_ipc_roundtrip_seconds(),
            )
            choice = resolved["selected"]
            harness_kind, model_provider, model = choice["harness"], choice["provider"], choice["model"]
        if harness_kind not in {"claude", "pi", "codex", "dsh", "jev", "user-proxy"}:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "harness kind must be claude, pi, codex, dsh, jev, or user-proxy (or use --tier without explicit model selection)",
            )
        proxy_route: str | None = None
        if harness_kind == "user-proxy":
            # One person's relay (docs/design-user-proxy-harness.md): the only
            # runtime argument is the person's DM route, which is where
            # everyone else's messages are forwarded to.
            if len(runtime_args) != 2 or runtime_args[0] != "--route":
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "user-proxy needs exactly: -- --route route:<adapter>:<route> "
                    "(the person's DM route)",
                )
            from hyprial.kernel import parse_route_uri

            proxy_route = runtime_args[1]
            if parse_route_uri(proxy_route) is None:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"--route must be route:<adapter>:<route>, got {proxy_route!r}",
                )
            if tier is not None or model_provider is not None or model is not None:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "user-proxy relays and has no model; --tier, --provider, and --model are not accepted",
                )
            if resume is not None:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "user-proxy is a stateless relay and has no persistent "
                    "session to resume",
                )
            if tmux:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT, "user-proxy does not support --tmux"
                )
            runtime_args = ()
            headless = True
        if harness_kind == "jev":
            if runtime_args:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "jev does not accept positional runtime arguments after '--'",
                )
            if tier is not None or model_provider is not None or model is not None:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "jev model selection belongs to each request; --tier, --provider, and --model are not accepted",
                )
            if resume is not None:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "jev has no persistent session; model selection is per request",
                )
            if tmux:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT, "jev does not support --tmux"
                )
            headless = True
        try:
            validate_model_selection(
                harness_kind, model_provider, model,
                context=f"hyprial agent start --name {name}",
            )
        except ModelProviderError as error:
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        for option, explicit in (("--provider", model_provider), ("--model", model)):
            if explicit is not None and any(
                value == option or value.startswith(f"{option}=")
                for value in runtime_args
            ):
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"{option} was supplied both as a hyprial option and after '--'",
                )
        resolved_cwd = _resolved_agent_cwd(name, cwd)
        resume_agent_before: JsonObject | None = None
        if resume is not None:
            if not (
                (harness_kind in {"claude", "pi", "codex", "dsh"} and not headless)
                or (headless and harness_kind in {"claude", "pi", "codex"})
            ):
                reason = (
                    "headless DSH cannot resume because a fresh worker MCP identity "
                    "cannot attach an existing DSH session"
                    if harness_kind == "dsh" and headless
                    else f"interactive {harness_kind} has no resumable native runtime"
                )
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    reason,
                )
            if not resume.strip():
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT, "--resume requires a session id"
                )
            if harness_kind in {"pi", "codex"} and not headless:
                resolved_cwd = _resolved_interactive_resume_cwd(name, cwd)
        if replace_interactive and not headless:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--replace-interactive is only valid with --headless",
            )
        if replace_interactive and resume is None:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--replace-interactive requires --resume <session-id>; a switch "
                "must preserve the conversation",
            )
        if tmux and headless:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT, "--tmux cannot be combined with --headless"
            )
        if tmux and harness_kind not in {"claude", "pi", "codex"}:
            raise services.CliError(
                "UNSUPPORTED_CAPABILITY",
                "--tmux is currently only supported for interactive claude, "
                "pi, and codex sessions",
            )
        if agent_preset is not None and (headless or harness_kind != "dsh"):
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--agent-preset is only supported for interactive dsh",
            )
        if not headless:
            from hyprial.daemon import (
                declare,
            )
            from hyprial.kernel import SupportLevel
            from hyprial.kernel import Capability

            attach = declare(harness_kind, headless=False).get(
                Capability.INTERACTIVE_ATTACH
            )
            if (
                attach is not None
                and attach.level is SupportLevel.NATIVE
                and attach.mechanism == "dsh_tui"
            ):
                return services._start_interactive_dsh(
                    name=name,
                    nickname=nickname,
                    cwd=resolved_cwd,
                    resume=resume,
                    runtime_args=runtime_args,
                    model_provider=model_provider,
                    model=model,
                    json_output=json_output,
                    tmux=tmux,
                    agent_preset=agent_preset,
                )
            if (
                attach is not None
                and attach.level is SupportLevel.NATIVE
                and attach.mechanism == "channel"
            ):
                return services._start_interactive_claude(
                    name=name,
                    nickname=nickname,
                    cwd=resolved_cwd,
                    resume=resume,
                    runtime_args=runtime_args,
                    model_provider=model_provider,
                    model=model,
                    json_output=json_output,
                    tmux=tmux,
                )
            if (
                attach is not None
                and attach.level is SupportLevel.NATIVE
                and attach.mechanism == "extension"
                and harness_kind == "pi"
            ):
                return services._start_interactive_pi(
                    name=name,
                    nickname=nickname,
                    cwd=resolved_cwd,
                    resume=resume,
                    runtime_args=runtime_args,
                    model_provider=model_provider,
                    model=model,
                    json_output=json_output,
                    tmux=tmux,
                )
            if (
                attach is not None
                and attach.level is SupportLevel.NATIVE
                and attach.mechanism == "app_server"
                and harness_kind == "codex"
            ):
                return services._start_interactive_codex(
                    name=name,
                    nickname=nickname,
                    cwd=resolved_cwd,
                    resume=resume,
                    runtime_args=runtime_args,
                    model_provider=model_provider,
                    model=model,
                    json_output=json_output,
                    tmux=tmux,
                )
            raise services.CliError(
                "UNSUPPORTED_CAPABILITY",
                f"{harness_kind} does not support interactive_attach; "
                "use --headless or start an interactive claude session instead",
            )
        if execution_runtime is None and harness_kind not in {"dsh", "jev", "user-proxy"}:
            # Pin the harness binary by absolute path at registration: the
            # daemon that later spawns it may run under a launchd/cron PATH
            # that lacks user bin dirs.  Older daemons ignore the unknown
            # key (tolerant desired-state reader), so this stays skew-safe.
            # abspath keeps vendor symlinks (e.g. ~/.local/bin/codex) intact
            # -- they are the upgrade-stable entry points.  Resolved BEFORE
            # agent creation so a refused start leaves no trace at all.
            pinned_binary = shutil.which(harness_kind)
            if pinned_binary is None:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"{harness_kind} binary not found on PATH; install it "
                    f"(or fix PATH) before starting a {harness_kind} connector",
                )
        params: JsonObject = {
            # The IPC key stays "provider" so this CLI can talk to a daemon
            # running an older build (and vice versa).
            "provider": harness_kind,
            "name": name,
            "headless": True if harness_kind in {"jev", "user-proxy"} else headless,
            "args": list(runtime_args),
            "cwd": str(resolved_cwd),
        }
        if execution_runtime is not None:
            params["executionRuntime"] = execution_runtime.to_json()
        elif harness_kind == "user-proxy":
            assert proxy_route is not None
            params["command"] = _user_proxy_launch_params(
                name, proxy_route, cwd=resolved_cwd
            )["command"]
        elif harness_kind == "jev":
            params["command"] = [
                os.path.abspath(sys.executable),
                "-m",
                "hyprial.daemon.impl.harnesses.python_worker._python_worker",
                "--kind",
                "jev",
            ]
        elif harness_kind != "dsh":
            params["command"] = [os.path.abspath(pinned_binary)]
        if nickname is not None:
            params["nickname"] = nickname
        if model_provider is not None and harness_kind not in {"jev", "user-proxy"}:
            params["modelProvider"] = model_provider
        if model is not None and harness_kind not in {"jev", "user-proxy"}:
            params["model"] = model
        if replace_interactive:
            params["replaceInteractive"] = True
        if resume is not None:
            # Headless resume: the daemon refuses a session it cannot find
            # (RESUME_SESSION_NOT_FOUND, before anything starts) and one that
            # does not hold (STRICT_RESUME_FAILED, worker stopped).  Without
            # this key the daemon keeps today's default: a fresh session.
            params["sessionRef"] = resume.strip()
            # Native transcript validation is read-only and must precede the
            # reusable Agent write.  A daemon without this dedicated method
            # fails closed with METHOD_NOT_FOUND before agent.create.
            preflight = services._daemon_request("lifecycle.resume.preflight", params)
            preflight_snapshot = preflight.get("agentSnapshot")
            if not isinstance(preflight_snapshot, dict):
                raise services.CliError(
                    "INVALID_RESPONSE",
                    "resume preflight did not return the complete Agent snapshot; "
                    "the daemon cannot safely restore a start-time refusal",
                )
        # A5: the Agent exists before the connector does. Resume adds the
        # stricter condition above: an invalid native target cannot rewrite it.
        create_result = services._create_agent_for_start(
            name=name,
            harness=harness_kind,
            runtime="headless",
            cwd=resolved_cwd,
            provider=None if harness_kind in {"jev", "user-proxy"} else model_provider,
            model=None if harness_kind in {"jev", "user-proxy"} else model,
            include_resume_snapshot=resume is not None,
        )
        if resume is not None:
            raw_before = create_result.get("resumePreviousSnapshot")
            if not isinstance(raw_before, dict):
                raise services.CliError(
                    ipc_errors.RESUME_AGENT_RESTORE_FAILED,
                    "agent.create did not replace an existing Agent after resume "
                    "preflight; no exact rollback record exists, so inspect the "
                    "new Agent before retrying",
                )
            resume_agent_before = raw_before
        resume_agent_after = create_result.get("resumeSnapshot")
        if resume_agent_before is not None and not isinstance(resume_agent_after, dict):
            raise services.CliError(
                "INVALID_RESPONSE",
                "agent.create did not return the post-write Agent snapshot; "
                "the daemon cannot safely restore a start-time refusal",
            )
        # Harness readiness is bounded by the lifecycle manager's operation
        # deadline, and this wait must OUTLAST the daemon-side wait
        # (deadline + wait margin): a shorter budget abandoned a healthy
        # start with IPC_TIMEOUT while the daemon still owned and settled it
        # (2026-09-14 production).  Same derivation as `down`; the three
        # waits are composed from one deadline, never chosen independently.
        try:
            started = services._daemon_request(
                (
                    "lifecycle.start-smolvm"
                    if execution_runtime is not None
                    else "lifecycle.start"
                ),
                params,
                timeout=(
                    LIFECYCLE_OPERATION_DEADLINE_SECONDS
                    + LIFECYCLE_WAIT_MARGIN_SECONDS
                    + LIFECYCLE_IPC_MARGIN_SECONDS
                    # A resume adds the daemon's readiness check (one margin) and,
                    # when it fails, one undo operation (a full operation wait).
                    + (
                        LIFECYCLE_WAIT_MARGIN_SECONDS
                        + LIFECYCLE_OPERATION_DEADLINE_SECONDS
                        + LIFECYCLE_WAIT_MARGIN_SECONDS
                        if "sessionRef" in params
                        else 0.0
                    )
                ),
            )
        except services.CliError as start_error:
            if (
                resume_agent_before is not None
                and isinstance(resume_agent_after, dict)
                and start_error.code
                in {
                    ipc_errors.RESUME_SESSION_NOT_FOUND,
                    ipc_errors.STRICT_RESUME_FAILED,
                }
            ):
                # The rollback is a compensating write, not a second start
                # attempt: when it fails its own error must NOT replace the
                # start refusal that made the rollback necessary.  Keep the
                # original code/message as the primary failure and attach the
                # rollback failure as extra context the operator can read.
                try:
                    services._daemon_request(
                        "agent.resume.restore",
                        {
                            "name": name,
                            "expected": resume_agent_after,
                            "replacement": resume_agent_before,
                        },
                    )
                except Exception as restore_error:  # noqa: BLE001 - reported, never mask
                    data = (
                        dict(start_error.data)
                        if isinstance(start_error.data, dict)
                        else {}
                    )
                    data["restoreError"] = {
                        "code": getattr(restore_error, "code", None),
                        "message": str(restore_error),
                    }
                    raise services.CliError(
                        start_error.code, str(start_error), data
                    ) from start_error
            raise
        if resume is not None:
            expected = resume.strip()
            started_session = (
                started.get("sessionRef") if isinstance(started, dict) else None
            )
            if started_session != expected:
                raise services.CliError(
                    ipc_errors.STRICT_RESUME_FAILED,
                    f"resume of {harness_kind} session {expected!r} did not hold: "
                    f"the worker established {started_session!r}",
                    {"harness": harness_kind, "name": name, "sessionRef": expected},
                )
        return started

    services._execute(operation, json_output=json_output)
