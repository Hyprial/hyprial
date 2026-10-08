"""``hyprial start`` - launch an agent session (headless or interactive)."""

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

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject, _DAEMON_IPC_ROUNDTRIP_SECONDS, _resolved_agent_cwd, _user_proxy_launch_params
@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def start(
    ctx: typer.Context,
    harness_kind: str | None = typer.Argument(None, help="claude, pi, codex, dsh, jev, or user-proxy; optional with --tier."),
    name: str = typer.Option(..., "--name"),
    tier: str | None = typer.Option(None, "--tier", help="Select fast, strong, or super; it chooses harness/provider/model, so it cannot be combined with any of them."),
    nickname: str | None = typer.Option(None, "--nickname"),
    cwd: Path | None = typer.Option(None, "--cwd"),
    headless: bool = typer.Option(False, "--headless"),
    smolvm_spec: Path | None = typer.Option(None, "--smolvm-spec", help="Explicit local smolvm v1 spec for a P2 headless Codex worker; experimental support boundary."),
    tmux: bool = typer.Option(
        False,
        "--tmux",
        help=(
            "Run the interactive TUI inside a detached tmux session and "
            "return attach commands (interactive claude and pi only)."
        ),
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help=(
            "Resume an existing session by id: interactive claude, or headless "
            "claude, pi, and codex (refused, never a fresh session, when the "
            "session cannot be found or does not hold)."
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
                f"`hyprial start --tier {tier} --name ...` or an explicit harness "
                "with --provider/--model",
            )
        if tier is not None and not explicit_selection:
            # Resolve inside the daemon so the audit uses its own runtime
            # profile, not caller-supplied evidence.  Selection is static
            # configuration, so this is one ordinary IPC round-trip: the
            # request no longer waits on any liveness probe (2026-09-21).
            # The read-only `dispatch matrix --probe` never enters this path.
            resolved = services._daemon_request(
                "dispatch.matrix.resolve", {"tier": tier, "name": name},
                timeout=_DAEMON_IPC_ROUNDTRIP_SECONDS,
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
            if resume is not None or tmux:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "user-proxy does not support --resume or --tmux",
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
            if resume is not None or tmux:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "jev does not support --resume or --tmux",
                )
            headless = True
        try:
            validate_model_selection(
                harness_kind, model_provider, model,
                context=f"hyprial start --name {name}",
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
        if resume is not None:
            if not (
                (harness_kind == "claude" and not headless)
                or (headless and harness_kind in {"claude", "pi", "codex"})
            ):
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "--resume is supported for interactive claude and for headless "
                    "claude, pi, and codex",
                )
            if not resume.strip():
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT, "--resume requires a session id"
                )
        if tmux and headless:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT, "--tmux cannot be combined with --headless"
            )
        if tmux and harness_kind not in {"claude", "pi"}:
            raise services.CliError(
                "UNSUPPORTED_CAPABILITY",
                "--tmux is currently only supported for interactive claude "
                "and pi sessions",
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
                    runtime_args=runtime_args,
                    model_provider=model_provider,
                    model=model,
                    json_output=json_output,
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
        # A5: the agent exists before the connector does, on this path too.
        services._create_agent_for_start(
            name=name,
            harness=harness_kind,
            runtime="headless",
            cwd=resolved_cwd,
            provider=None if harness_kind in {"jev", "user-proxy"} else model_provider,
            model=None if harness_kind in {"jev", "user-proxy"} else model,
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
        if resume is not None:
            # Headless resume: the daemon refuses a session it cannot find
            # (RESUME_SESSION_NOT_FOUND, before anything starts) and one that
            # does not hold (STRICT_RESUME_FAILED, worker stopped).  Without
            # this key the daemon keeps today's default: a fresh session.
            params["sessionRef"] = resume.strip()
        # Harness readiness is bounded by the lifecycle manager's operation
        # deadline, and this wait must OUTLAST the daemon-side wait
        # (deadline + wait margin): a shorter budget abandoned a healthy
        # start with IPC_TIMEOUT while the daemon still owned and settled it
        # (2026-09-14 production).  Same derivation as `down`; the three
        # waits are composed from one deadline, never chosen independently.
        return services._daemon_request(
            "lifecycle.start-smolvm" if execution_runtime is not None else "lifecycle.start",
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

    services._execute(operation, json_output=json_output)
